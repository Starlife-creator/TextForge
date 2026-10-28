"""B2/N4（review）：runs.json 读-改-写在并发下不能丢条目。

用 barrier 在外层强制两线程同时进入 upsert：
- 有锁：两线程串行，A 写完 B 再读，两条都在。
- 无锁：两线程基于同一空快照读-改-写，后写者整份覆盖前写者，只剩一条。
不用靠 200 次循环赌交错概率（上一版去掉锁后 8 次里 4 次全绿，是抛硬币守卫）。
"""
from __future__ import annotations

import threading

import routes.history as history


def test_history_upsert_concurrent_two_paths(tmp_path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(history, "ensure_data_dir", lambda: data_dir)
    (data_dir / "runs.json").write_text('{"entries": {}}', encoding="utf-8")

    real_upsert = history.upsert
    barrier = threading.Barrier(2)

    def racing_upsert(path, meta):
        # 两线程同时放行，都进入真实 upsert 的读-改-写段。
        barrier.wait(timeout=5)
        return real_upsert(path, meta)

    monkeypatch.setattr(history, "upsert", racing_upsert)

    def upsert_a():
        real_upsert(str(tmp_path / "book_a"),
                    {"novel_name": "甲", "current_phase": "phase1"})

    def upsert_b():
        real_upsert(str(tmp_path / "book_b"),
                    {"novel_name": "乙", "current_phase": "phase2"})

    ta = threading.Thread(target=upsert_a)
    tb = threading.Thread(target=upsert_b)
    ta.start(); tb.start()
    ta.join(); tb.join()

    reg = history._load()
    entries = reg["entries"]
    assert len(entries) == 2, f"并发丢条目（去掉锁必红）：{list(entries)!r}"
    paths = {e["output_path"] for e in entries.values()}
    assert str(tmp_path / "book_a") in paths
    assert str(tmp_path / "book_b") in paths
    for e in entries.values():
        assert e.get("novel_name"), f"字段不完整：{e!r}"
