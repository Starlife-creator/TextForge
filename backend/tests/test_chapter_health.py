"""#13 章节健康分最小闭环：评分规则、档位与质量报告聚合回归。"""

import tempfile
from pathlib import Path
from types import SimpleNamespace

from core.quality import (
    build_quality_report,
    chapter_health_score,
    health_grade,
)


def _req(**kw):
    base = dict(forbidden_canon=[], name_map={}, refactor_mode="full_rewrite",
                model="mock", context_window=8000, input_mode="single_file",
                input_format="txt")
    base.update(kw)
    return SimpleNamespace(**base)


def _chapter(i: int) -> dict:
    return {"id": str(i), "title": f"第{i}章",
            "split_file": f"02_workspace/split/{i}.txt", "is_empty": False}


def _report(tmp: str, originals: list[str], recons: list[str], *, req=None,
            progress=None) -> dict:
    out = Path(tmp)
    (out / "02_workspace/split").mkdir(parents=True, exist_ok=True)
    chapters = []
    for i, (orig, _recon) in enumerate(zip(originals, recons, strict=False), start=1):
        ch = _chapter(i)
        (out / ch["split_file"]).write_text(orig, encoding="utf-8")
        chapters.append(ch)
    return build_quality_report(
        chapters=chapters, texts=recons, progress=progress or {},
        req=req or _req(), output_path=out,
    )


def test_health_grade_bands():
    assert health_grade(100) == "good"
    assert health_grade(85) == "good"
    assert health_grade(84) == "warn"
    assert health_grade(60) == "warn"
    assert health_grade(59) == "bad"
    assert health_grade(0) == "bad"


def test_clean_chapter_scores_full_marks():
    row = {"status": "ok", "ratio": 0.95}
    assert chapter_health_score(row, []) == 100
    assert health_grade(100) == "good"


def test_skipped_chapter_scores_zero():
    row = {"status": "skipped", "ratio": None}
    assert chapter_health_score(row, []) == 0
    assert health_grade(0) == "bad"


def test_named_bad_code_penalizes_more_than_generic_warn():
    # placeholder 专属扣 30；通用 BAD 扣 40；通用 WARN 扣 10。
    row = {"status": "bad", "ratio": 1.0}
    ph = {"severity": "bad", "code": "placeholder"}
    generic_bad = {"severity": "bad", "code": "some_other_bad"}
    generic_warn = {"severity": "warn", "code": "length_off"}
    assert chapter_health_score(row, [ph]) == 70
    assert chapter_health_score(row, [generic_bad]) == 60
    assert chapter_health_score(row, [generic_warn]) == 90


def test_ratio_off_extra_penalty():
    row = {"status": "ok", "ratio": 0.4}
    assert chapter_health_score(row, []) == 92


def test_report_attaches_health_fields_and_aggregates():
    originals = ["第一章开篇主线推进。" * 120, "第二章新人物登场交锋。" * 120]
    recons = ["第一章重构后主线继续推进。" * 115, "第二章重构后新人物登场交锋。" * 110]
    with tempfile.TemporaryDirectory() as tmp:
        report = _report(tmp, originals, recons)

    for row in report["chapters"]:
        assert "health_score" in row and "health_grade" in row
        assert 0 <= row["health_score"] <= 100
    s = report["summary"]
    assert s["avg_health"] == 100.0
    assert s["health_trend"] == [100, 100]
    assert s["health_good"] == 2 and s["health_warn"] == 0 and s["health_bad"] == 0
    # 指标快照：全量计数不受 issues 列表截断影响
    assert s["issues_total"] == len(report["issues"])
    assert isinstance(s["issue_code_counts"], dict)


def test_report_flags_collapse_chapter_as_health_bad():
    originals = [("原文。" * 800), ("原文。" * 300)]
    recons = ["只剩一句话。", ("重构。" * 270)]
    with tempfile.TemporaryDirectory() as tmp:
        report = _report(tmp, originals, recons)

    assert report["status"] == "bad"
    assert report["summary"]["health_bad"] >= 1
    collapse_row = next(r for r in report["chapters"] if r["id"] == "1")
    assert collapse_row["health_grade"] == "bad"
    assert collapse_row["health_score"] < 60


def test_empty_chapter_report_row_scores_zero():
    originals = [("原文。" * 300)]
    with tempfile.TemporaryDirectory() as tmp:
        report = _report(tmp, originals, [""])
    row = report["chapters"][0]
    assert row["health_score"] == 0
    assert row["health_grade"] == "bad"
    assert report["summary"]["health_bad"] == 1
