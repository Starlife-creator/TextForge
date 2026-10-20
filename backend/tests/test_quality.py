"""成品质量报告的结构性检查测试。"""

import tempfile
from pathlib import Path
from types import SimpleNamespace

from core.quality import (
    build_quality_report,
    find_duplicate_pairs,
    read_quality_report,
    write_quality_report,
)


def _req(**kw):
    base = dict(
        forbidden_canon=[],
        name_map={},
        refactor_mode="full_rewrite",
        model="mock-model",
        context_window=8000,
        input_mode="single_file",
        input_format="txt",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _chapter(i: int, is_empty: bool = False) -> dict:
    return {
        "id": str(i),
        "title": f"第{i}章",
        "split_file": f"02_workspace/split/{i}.txt",
        "is_empty": is_empty,
    }


def _report(tmp: str, originals: list[str], recons: list[str], *, req=None, progress=None):
    out = Path(tmp)
    (out / "02_workspace/split").mkdir(parents=True, exist_ok=True)
    chapters = []
    for i, (orig, _recon) in enumerate(zip(originals, recons, strict=False), start=1):
        ch = _chapter(i)
        (out / ch["split_file"]).write_text(orig, encoding="utf-8")
        chapters.append(ch)
    return build_quality_report(
        chapters=chapters,
        texts=recons,
        progress=progress or {},
        req=req or _req(),
        output_path=out,
    )


def test_quality_good_report():
    originals = [("原文第%d章。" % i) * 150 for i in range(1, 4)]
    recons = [("重构第%d章故事继续。" % i) * 130 for i in range(1, 4)]
    with tempfile.TemporaryDirectory() as tmp:
        report = _report(tmp, originals, recons)

    assert report["status"] == "good"
    assert report["summary"]["empty_chapters"] == 0
    assert report["summary"]["duplicate_pairs"] == 0
    assert all(row["status"] == "ok" for row in report["chapters"])
    assert report["summary"]["length_ratio"] > 0


def test_quality_detects_empty_residual_and_placeholder():
    originals = [("原文。" * 300) for _ in range(3)]
    recons = [
        "",
        "正文 ===CHAPTER_BREAK=== 残留",
        "这里 TODO 待补，还没写完。",
    ]
    with tempfile.TemporaryDirectory() as tmp:
        report = _report(tmp, originals, recons)

    codes = {i["code"] for i in report["issues"]}
    assert report["status"] == "bad"
    assert {"empty_chapter", "residual_marker", "placeholder"} <= codes
    assert report["summary"]["empty_chapters"] == 1
    assert report["summary"]["residual_markers"] == 1
    assert report["summary"]["placeholder_hits"] == 1


def test_quality_detects_cross_chapter_duplicate():
    originals = [("原文甲。" * 300), ("原文乙。" * 300)]
    same = "这是一段会被完整复制到另一章的正文。" * 40
    with tempfile.TemporaryDirectory() as tmp:
        report = _report(tmp, originals, [same, same])

    assert report["status"] == "bad"
    assert report["summary"]["duplicate_pairs"] == 1
    assert report["duplicates"][0]["containment"] > 0.9
    assert {i["code"] for i in report["issues"]} >= {"cross_chapter_duplicate"}


def test_quality_detects_forbidden_and_old_name_in_reskin():
    originals = [("原文。" * 300)]
    recon = "苏晚看见林婉儿走来，她用了禁术。" * 30
    req = _req(
        refactor_mode="reskin",
        name_map={"林婉儿": "苏晚"},
        forbidden_canon=["禁术"],
    )
    with tempfile.TemporaryDirectory() as tmp:
        report = _report(tmp, originals, [recon], req=req)

    codes = {i["code"] for i in report["issues"]}
    assert report["status"] == "bad"
    assert "forbidden_canon" in codes
    assert "old_name_residue" in codes
    assert report["summary"]["forbidden_hits"] == 1
    assert report["summary"]["old_name_hits"] == 1


def test_quality_detects_content_collapse():
    originals = [("原文。" * 800)]
    with tempfile.TemporaryDirectory() as tmp:
        report = _report(tmp, originals, ["只剩一句话。"])

    assert report["status"] == "bad"
    assert {i["code"] for i in report["issues"]} >= {"content_collapse"}


def test_duplicate_needs_more_than_one_shared_shingle():
    common = "共同片段" * 20  # 80 字：至少产生两个 60 字重叠片段
    a = common + "甲" * 500
    b = common + "乙" * 500
    assert len(find_duplicate_pairs({"1": a, "2": b})) == 1

    short = "共同片段" * 15  # 60 字：只共享一个片段，不应误报
    c = short + "甲" * 500
    d = short + "乙" * 500
    assert find_duplicate_pairs({"1": c, "2": d}) == []


def test_quality_report_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        report = _report(tmp, ["原文。" * 200], ["重构。" * 200])
        path = write_quality_report(tmp, report)
        loaded = read_quality_report(tmp)

    assert path.name == "quality_report.json"
    assert loaded is not None
    assert loaded["status"] == report["status"]
    assert loaded["summary"]["chapters"] == 1
