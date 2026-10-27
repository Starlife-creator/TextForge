"""#3/#6/#9 顺手小项回归：regen 次数进报告摘要与历史、错误归类映射表、对照稿拒绝符号链接。"""

import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from routes import history
from routes.status import _list_comparisons, _read_comparison
from utils.errors import classify_exception, raise_internal_error


# ---------- #9 符号链接拒绝 ----------

def test_comparison_read_rejects_symlink_and_lists_only_real_files(tmp_path):
    comp = tmp_path / "00_comparison"
    comp.mkdir()
    real = comp / "chapter_1.comparison.md"
    real.write_text("正常对照稿", encoding="utf-8")
    outside = tmp_path / "secret.md"
    outside.write_text("目录外部文件", encoding="utf-8")
    link = comp / "chapter_9.comparison.md"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError) as e:
        # Windows 非开发者模式下 os.symlink 抛 OSError 1314（缺符号链接特权）；
        # 这种环境测不了符号链接拒绝，直接 skip 而不是让整套测试红。
        pytest.skip(f"当前环境不支持创建符号链接：{e}")

    ok = _read_comparison(comp, "chapter_1.comparison.md")
    assert ok == [{"name": "chapter_1.comparison.md", "text": "正常对照稿"}]
    assert _read_comparison(comp, "chapter_9.comparison.md") == []

    listed = {x["name"] for x in _list_comparisons(comp)}
    assert "chapter_1.comparison.md" in listed
    assert "chapter_9.comparison.md" not in listed


# ---------- #6 异常归类映射表 ----------

def test_classify_exception_maps_known_types():
    assert classify_exception(FileNotFoundError("x"))["code"] == "file_missing"
    assert classify_exception(PermissionError("x"))["code"] == "permission_denied"
    assert classify_exception(TimeoutError("x"))["code"] == "upstream_timeout"
    assert classify_exception(ConnectionError("x"))["code"] == "upstream_unreachable"
    assert classify_exception(ValueError("x"))["code"] == "invalid_input"
    assert classify_exception(RuntimeError("x"))["code"] == "internal_error"


def test_internal_error_still_hides_detail_after_classify():
    exc = FileNotFoundError("/home/secret/progress.json 不存在")
    http = raise_internal_error(exc, "内部错误，详情请查看后端日志", "regen_chapter")
    assert http.status_code == 500
    assert "/home/secret" not in str(http.detail)


# ---------- #3 regen 次数进历史元数据 ----------

def test_meta_from_progress_records_regen_total(monkeypatch, tmp_path):
    monkeypatch.setenv("TEXTFORGE_DATA_DIR", str(tmp_path))
    progress = {
        "regen_count": {"1": 3, "2": 1},
        "chapters": [], "batches": [], "error_logs": [],
    }
    meta = history._meta_from_progress(progress)
    assert meta["regen_total"] == 4


# ---------- #3 regen 次数进质量报告摘要 ----------

def _req(**kw):
    base = dict(forbidden_canon=[], name_map={}, refactor_mode="full_rewrite",
                model="mock", context_window=8000, input_mode="single_file",
                input_format="txt")
    base.update(kw)
    return SimpleNamespace(**base)


def test_quality_summary_exposes_regen_stats():
    from core.quality import build_quality_report
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        (out / "02_workspace/split").mkdir(parents=True)
        ch = {"id": "1", "title": "第一章",
              "split_file": "02_workspace/split/1.txt", "is_empty": False}
        (out / ch["split_file"]).write_text("原文。" * 300, encoding="utf-8")
        report = build_quality_report(
            chapters=[ch], texts=["重构。" * 280],
            progress={"regen_count": {"1": 2, "3": 1}},
            req=_req(), output_path=out,
        )
    assert report["summary"]["regen_total"] == 3
    assert report["summary"]["regen_chapters"] == 2
