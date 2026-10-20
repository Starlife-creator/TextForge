"""phase2 分层蓝图的纯逻辑测试（不发网络请求）。"""

import asyncio
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from core.blueprint import (
    CLIP_MARK,
    build_blueprint_context,
    compress_summaries,
    read_gap_digest,
    summary_files,
)


def test_summary_files_sorted_numerically():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "01_summaries"
        d.mkdir(parents=True)
        for n in (1, 2, 10, 100):
            (d / f"batch_{n:02d}.txt").write_text(f"摘要{n}", encoding="utf-8")
        assert [p.stem for p in summary_files(tmp)] == [
            "batch_01",
            "batch_02",
            "batch_10",
            "batch_100",
        ]


def test_compress_within_budget_makes_no_calls():
    calls = []

    async def map_call(prompt, target):
        calls.append(prompt)
        return "压缩"

    with tempfile.TemporaryDirectory() as tmp:
        items, meta = asyncio.run(
            compress_summaries(
                ["甲" * 100, "乙" * 100],
                budget=1000,
                map_call=map_call,
                output_path=tmp,
                model="m",
                prompt_version="v",
            )
        )
    assert calls == []
    assert meta["map_calls"] == 0
    assert meta["sampled"] is False
    assert items == ["甲" * 100, "乙" * 100]


def test_compress_maps_all_chunks_and_keeps_ends():
    prompts = []

    async def map_call(prompt, target):
        prompts.append(prompt)
        return "压" * 80

    items = [f"MARK{i:02d}" + "甲" * 600 for i in range(10)]
    with tempfile.TemporaryDirectory() as tmp:
        out, meta = asyncio.run(
            compress_summaries(
                items,
                budget=2400,
                map_call=map_call,
                output_path=tmp,
                model="m",
                prompt_version="v",
            )
        )

    assert meta["sampled"] is False
    assert meta["map_calls"] == len(prompts) > 0
    assert "MARK00" in prompts[0]
    assert "MARK09" in prompts[-1]
    assert len(out) < len(items)


def test_compress_cache_avoids_second_run():
    calls = []

    async def map_call(prompt, target):
        calls.append(prompt)
        return "压" * 80

    items = [f"MARK{i:02d}" + "甲" * 600 for i in range(10)]
    with tempfile.TemporaryDirectory() as tmp:
        _, first = asyncio.run(
            compress_summaries(
                items, budget=2400, map_call=map_call, output_path=tmp,
                model="m", prompt_version="v",
            )
        )
        _, second = asyncio.run(
            compress_summaries(
                items, budget=2400, map_call=map_call, output_path=tmp,
                model="m", prompt_version="v",
            )
        )

    assert first["map_calls"] == second["cache_hits"] > 0
    assert second["map_calls"] == 0
    assert len(calls) == first["map_calls"]


def test_single_huge_summary_is_clipped_head_and_tail():
    async def map_call(prompt, target):  # pragma: no cover - 不应被调用
        raise AssertionError("单条摘要无法再分段，不应调用 map")

    item = "HEAD" + "甲" * 3000 + "TAIL"
    with tempfile.TemporaryDirectory() as tmp:
        out, meta = asyncio.run(
            compress_summaries(
                [item], budget=500, map_call=map_call, output_path=tmp,
                model="m", prompt_version="v",
            )
        )

    text = out[0]
    assert meta["sampled"] is True
    assert text.startswith("HEAD") and text.endswith("TAIL")
    assert CLIP_MARK in text
    assert len(text) <= 500


def test_gap_digest_prioritizes_high_and_caps():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "diagnose_01.json").write_text(
            json.dumps(
                {
                    "batch_id": 1,
                    "gaps": [
                        {"severity": "low", "location": "第3章", "description": "低风险"},
                        {"severity": "high", "location": "第1章", "description": "高风险"},
                        {"severity": "medium", "location": "第2章", "description": "中风险"},
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (d / "diagnose_02.json").write_text(
            json.dumps(
                {"batch_id": 2, "gaps": [{"severity": "high", "description": "第二处高风险"}]},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        full = read_gap_digest(str(d), max_chars=2000)
        capped = read_gap_digest(str(d), max_chars=25)

    assert full["total"] == 4 and full["included"] == 4
    assert full["text"].index("高风险") < full["text"].index("低风险")
    assert capped["included"] == 1
    assert "高风险" in capped["text"]
    assert "低风险" not in capped["text"]


def test_build_blueprint_context_reaches_last_batch():
    prompts = []

    async def map_call(prompt, target):
        prompts.append(prompt)
        return "压" * 100

    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "01_summaries"
        d.mkdir(parents=True)
        for i in range(10):
            (d / f"batch_{i:02d}.txt").write_text(
                f"BATCH{i:02d}" + "内容" * 500, encoding="utf-8"
            )
        req = SimpleNamespace(context_window=8000, diagnose_json=None, model="m")
        progress = {}
        events = []
        context, meta = asyncio.run(
            build_blueprint_context(
                output_path=tmp,
                req=req,
                progress=progress,
                map_call=map_call,
                prompt_version="v",
                model="m",
                emit=lambda ev, data: events.append((ev, data)),
            )
        )

    assert meta["summaries"] == 10
    assert meta["last_summary"] == "batch_09"
    assert meta["map_calls"] == len(prompts) > 0
    assert "BATCH00" in prompts[0]
    assert "BATCH09" in prompts[-1]
    assert progress["blueprint_context"] is meta
    assert not [e for e in events if e[0] == "error"]
