"""#4 阻塞 I/O 线程化配套：progress 同步读写原语与线程安全缓存单测。

路由层把这些同步函数经 asyncio.to_thread 调用，事件循环不再直接做磁盘读取与
JSON 解析；这里校验原语本身的行为（往返、损坏报错、缓存失效、并发安全）。
"""

import json
import threading
from pathlib import Path

import pytest

from utils.progress_io import (
    ProgressUnreadable,
    clear_progress_cache,
    load_progress_dict,
    read_progress_cached,
    save_progress_dict,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_progress_cache()
    yield
    clear_progress_cache()


def test_save_and_load_round_trip(tmp_path: Path):
    p = tmp_path / "progress.json"
    data = {"current_phase": "phase3", "chapters": [{"id": "1"}], "usage": {"prompt_tokens": 12}}
    save_progress_dict(p, data)
    loaded = load_progress_dict(p)
    assert loaded == data
    assert loaded["chapters"][0]["id"] == "1"


def test_load_corrupted_raises_unreadable(tmp_path: Path):
    p = tmp_path / "progress.json"
    p.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(ProgressUnreadable):
        load_progress_dict(p)


def test_read_progress_cached_hits_cache_on_unchanged_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    p = tmp_path / "progress.json"
    p.write_text(json.dumps({"a": 1, "nested": {"x": [1, 2]}}), encoding="utf-8")
    clear_progress_cache()
    # 计数真实读盘：第二次调用必须命中缓存、不再走 read_progress。
    calls = {"n": 0}
    import utils.progress_io as m
    real = m.read_progress
    def counting(path):
        calls["n"] += 1
        return real(path)
    monkeypatch.setattr(m, "read_progress", counting)
    first = read_progress_cached(p)
    second = read_progress_cached(p)
    # 命中缓存但返回深拷贝：内容相等，调用方改 second 不影响 first（也不污染缓存本体）。
    assert first == second
    assert second["a"] == 1
    second["nested"]["x"].append(999)
    assert first["nested"]["x"] == [1, 2]
    assert calls["n"] == 1, f"缓存未命中：read_progress 被调用 {calls['n']} 次"


def test_read_progress_cached_reloads_after_file_change(tmp_path: Path):
    p = tmp_path / "progress.json"
    p.write_text(json.dumps({"a": 1}), encoding="utf-8")
    read_progress_cached(p)
    # 模拟流水线写回：换 mtime + size
    p.write_text(json.dumps({"a": 2, "b": "x"}), encoding="utf-8")
    reloaded = read_progress_cached(p)
    assert reloaded["a"] == 2
    assert reloaded["b"] == "x"


def test_read_progress_cached_thread_safe_concurrent_reads(tmp_path: Path):
    p = tmp_path / "progress.json"
    p.write_text(json.dumps({"chapters": list(range(50))}), encoding="utf-8")
    errors: list[Exception] = []
    barrier = threading.Barrier(8)

    def worker():
        try:
            barrier.wait()
            for _ in range(20):
                read_progress_cached(p)
        except Exception as e:  # pragma: no cover - 记录并发异常
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


def test_save_then_cached_read_returns_new_content(tmp_path: Path):
    p = tmp_path / "progress.json"
    save_progress_dict(p, {"v": 1})
    assert read_progress_cached(p)["v"] == 1
    save_progress_dict(p, {"v": 2})
    assert read_progress_cached(p)["v"] == 2
