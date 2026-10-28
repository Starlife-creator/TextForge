"""#19 阈值集中维护后的边界行为回归：常量口径不变，判定边界与基线一致。"""

import tempfile
from pathlib import Path
from types import SimpleNamespace

from core.quality import (
    COLLAPSE_MIN_ORIG_CHARS,
    COLLAPSE_RATIO,
    EXPANSION_RATIO,
    MAX_BATCH_QC_ISSUES,
    MAX_ISSUES_REPORT,
    MAX_PRE_STITCH_ISSUES,
    WARN_HIGH_RATIO,
    WARN_LOW_RATIO,
    assess_batch_texts,
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


def _run(tmp: str, orig: str, recon: str) -> list[dict]:
    out = Path(tmp)
    (out / "02_workspace/split").mkdir(parents=True)
    ch = _chapter(1)
    (out / ch["split_file"]).write_text(orig, encoding="utf-8")
    return assess_batch_texts(
        chapters=[ch], chapter_ids=["1"], texts={"1": recon},
        output_path=out, forbidden_canon=[], name_map={},
        refactor_mode="full_rewrite", gates={},
    )


def _codes(issues: list[dict]) -> set[str]:
    return {i["code"] for i in issues}


def test_threshold_constants_centralized_with_expected_values():
    # 与基线口径一致：塌缩 30%、膨胀 3 倍、提醒带 55%~180%、最小原文 500 字。
    assert COLLAPSE_MIN_ORIG_CHARS == 500
    assert COLLAPSE_RATIO == 0.30
    assert EXPANSION_RATIO == 3.0
    assert WARN_LOW_RATIO == 0.55
    assert WARN_HIGH_RATIO == 1.8
    assert MAX_ISSUES_REPORT == 200
    assert MAX_BATCH_QC_ISSUES == 50
    assert MAX_PRE_STITCH_ISSUES == 200


def test_short_original_does_not_trigger_collapse_bad():
    # 原文 200 字、重构 50 字（0.25）：低于最小原文门槛，只报篇幅偏离 WARN。
    with tempfile.TemporaryDirectory() as tmp:
        issues = _run(tmp, "原" * 200, "重" * 50)
    codes = _codes(issues)
    assert "content_collapse" not in codes
    assert "length_off" in codes
    assert all(i["severity"] != "bad" for i in issues)


def test_long_original_collapse_is_bad():
    with tempfile.TemporaryDirectory() as tmp:
        issues = _run(tmp, "原" * 600, "重" * 150)
    assert "content_collapse" in _codes(issues)
    assert any(i["severity"] == "bad" for i in issues)


def test_long_original_expansion_is_bad():
    with tempfile.TemporaryDirectory() as tmp:
        issues = _run(tmp, "原" * 600, "重" * 2000)
    assert "content_expansion" in _codes(issues)
    assert any(i["severity"] == "bad" for i in issues)


def test_mid_length_off_is_warn_only():
    # 原文 400、重构 750（1.875）：越过提醒带上限但原文不足 500，不报膨胀 BAD。
    with tempfile.TemporaryDirectory() as tmp:
        issues = _run(tmp, "原" * 400, "重" * 750)
    codes = _codes(issues)
    assert "length_off" in codes
    assert "content_expansion" not in codes
    assert all(i["severity"] != "bad" for i in issues)


def test_in_band_ratio_is_clean():
    # 原文 400、重构 380（0.95）：落在提醒带内，无篇幅问题。
    with tempfile.TemporaryDirectory() as tmp:
        issues = _run(tmp, "原" * 400, "重" * 380)
    assert "length_off" not in _codes(issues)
