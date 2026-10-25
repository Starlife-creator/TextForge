"""#22 蓝图压缩缓存 LRU：条目数/字节双口径淘汰、命中与未命中统计。"""

import asyncio
import os
import time
from pathlib import Path

import core.blueprint as bp


def _make_cache_dir(tmp: Path, n: int, *, size: int = 128) -> Path:
    d = tmp / "_diagnostics" / "blueprint_map"
    d.mkdir(parents=True)
    base = time.time_ns()
    for i in range(n):
        f = d / f"arc_{i:024d}.json"
        f.write_text("x" * size, encoding="utf-8")
        os.utime(f, ns=(base + i, base + i))
    return d


def test_evict_respects_entry_count_cap(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(bp, "CACHE_MAX_ENTRIES", 50)
    monkeypatch.setattr(bp, "CACHE_MAX_BYTES", 100 * 1024 * 1024)
    d = _make_cache_dir(tmp_path, 60)
    evicted = bp._evict_cache_lru(d)
    assert evicted == 10
    remaining = sorted(p.name for p in d.glob("arc_*.json"))
    # 最旧的 00~09 被淘汰，最新的 50 条保留
    expected = {f"arc_{i:024d}.json" for i in range(10, 60)}
    assert set(remaining) == expected
    assert len(remaining) == 50


def test_evict_respects_total_byte_cap(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(bp, "CACHE_MAX_ENTRIES", 100)
    monkeypatch.setattr(bp, "CACHE_MAX_BYTES", 256)
    d = _make_cache_dir(tmp_path, 10, size=100)
    evicted = bp._evict_cache_lru(d)
    assert evicted >= 8
    remaining = list(d.glob("arc_*.json"))
    total = sum(p.stat().st_size for p in remaining)
    assert total <= 256


def test_evict_missing_dir_returns_zero(tmp_path: Path):
    assert bp._evict_cache_lru(tmp_path / "nope") == 0


def test_cache_misses_counted_on_first_run_hits_on_second(tmp_path: Path):
    calls = []

    async def map_call(prompt, target):
        calls.append(prompt)
        return "压" * 80

    items = [f"MARK{i:02d}" + "甲" * 600 for i in range(10)]
    _, first = asyncio.run(bp.compress_summaries(
        items, budget=2400, map_call=map_call, output_path=tmp_path,
        model="m", prompt_version="v",
    ))
    _, second = asyncio.run(bp.compress_summaries(
        items, budget=2400, map_call=map_call, output_path=tmp_path,
        model="m", prompt_version="v",
    ))

    assert first["cache_misses"] == first["map_calls"] > 0
    assert first["cache_hits"] == 0
    assert second["cache_hits"] == first["map_calls"]
    assert second["cache_misses"] == 0
    assert second["map_calls"] == 0


def test_cache_scope_is_per_output_dir(tmp_path: Path):
    """两个输出目录的缓存物理隔离：A 目录的压缩结果不会出现在 B 目录。"""
    calls = []

    async def map_call(prompt, target):
        calls.append(prompt)
        return "压" * 80

    book_a = tmp_path / "book_a"
    book_b = tmp_path / "book_b"
    items = [f"MARK{i:02d}" + "甲" * 600 for i in range(10)]
    asyncio.run(bp.compress_summaries(
        items, budget=2400, map_call=map_call, output_path=book_a,
        model="m", prompt_version="v",
    ))
    assert (book_a / "_diagnostics" / "blueprint_map").is_dir()
    assert not (book_b / "_diagnostics").exists()
