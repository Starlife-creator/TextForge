"""B2（review A2）：runs.json 读-改-写在并发下不能丢条目。

record_progress（流水线线程）与 history_forget（用户点按钮）并发时，无锁会让
后写者整份覆盖前写者。两个线程对不同 output_path 各 upsert 20 次，结束后两条
entry 都必须在、字段完整。去掉 _REGISTRY_LOCK 本测试必须变红。
"""
from __future__ import annotations

import threading

import routes.history as history


def test_history_upsert_concurrent_two_paths(tmp_path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(history, "ensure_data_dir", lambda: data_dir)
    # 重置注册表到空
    (data_dir / "runs.json").write_text('{"entries": {}}', encoding="utf-8")

    def upsert_a() -> None:
        for i in range(200):
            history.upsert(str(tmp_path / "book_a"),
                           {"novel_name": "甲", "current_phase": f"phase{i % 3}"})

    def upsert_b() -> None:
        for i in range(200):
            history.upsert(str(tmp_path / "book_b"),
                           {"novel_name": "乙", "current_phase": f"phase{i % 3}"})

    ta = threading.Thread(target=upsert_a)
    tb = threading.Thread(target=upsert_b)
    ta.start(); tb.start()
    ta.join(); tb.join()

    reg = history._load()
    entries = reg["entries"]
    assert len(entries) == 2, f"并发丢条目：{list(entries)!r}"
    paths = {e["output_path"] for e in entries.values()}
    assert str(tmp_path / "book_a") in paths
    assert str(tmp_path / "book_b") in paths
    for e in entries.values():
        assert e.get("novel_name"), f"字段不完整：{e!r}"
