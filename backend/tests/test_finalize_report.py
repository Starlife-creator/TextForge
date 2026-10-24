"""质量报告生成失败时必须留下原因：既不能拖垮成品，也不能只剩一行日志。

为什么专门测：v0.13.0 首次发布时，CI 上有一轮报告没生成。finalize 把异常收成
`logger.warning` 就结束了 —— 界面只显示「尚无报告」，端到端只拿到一个 404，
而日志在打包版里用户看不到、在 CI 里也取不到。事后靠重跑才确认是偶发。
现在失败原因写进 progress.json，并由 `/api/output/quality` 的 404 详情带出来。
"""

import json
from types import SimpleNamespace

from core.finalize import write_quality_report_safely
from routes.output import _missing_report_detail


def _req():
    return SimpleNamespace(input_format="txt", input_mode="single_file",
                           refactor_gates={}, author_style="")


def test_success_records_report_metadata(monkeypatch, tmp_path):
    from core import quality

    monkeypatch.setattr(quality, "build_quality_report", lambda **kw: {
        "status": "warn",
        "generated_at": "2026-10-24T00:00:00",
        "summary": {"issues_by_severity": {"bad": 0, "warn": 2, "info": 1}},
    })
    monkeypatch.setattr(quality, "write_quality_report",
                        lambda out, rep: tmp_path / "quality_report.json")
    progress: dict = {}
    write_quality_report_safely(tmp_path, progress, chapters=[], texts=[], req=_req())
    qr = progress["quality_report"]
    assert qr["status"] == "warn"
    assert qr["issues_by_severity"] == {"bad": 0, "warn": 2, "info": 1}
    assert qr["path"].endswith("quality_report.json")
    assert "error" not in qr


def test_failure_is_recorded_with_reason(monkeypatch, tmp_path):
    """报告炸了：成品照常，但 progress 里必须能查到「为什么没有报告」。"""
    from core import quality

    def boom(**kwargs):
        raise RuntimeError("模拟渲染崩溃")

    monkeypatch.setattr(quality, "build_quality_report", boom)
    progress: dict = {}
    write_quality_report_safely(tmp_path, progress, chapters=[], texts=[], req=_req())
    qr = progress["quality_report"]
    assert qr["status"] == "failed"
    assert qr["error"].startswith("RuntimeError:")
    assert "模拟渲染崩溃" in qr["error"]


def test_missing_report_detail_carries_the_reason(tmp_path):
    """404 详情：有失败记录就把原因带上，没有就不要编造。"""
    assert "尚无质量报告" in _missing_report_detail(str(tmp_path))

    (tmp_path / "progress.json").write_text(json.dumps({
        "quality_report": {"status": "failed", "error": "RuntimeError: 磁盘已满"},
    }), encoding="utf-8")
    detail = _missing_report_detail(str(tmp_path))
    assert "RuntimeError: 磁盘已满" in detail

    (tmp_path / "progress.json").write_text(json.dumps({
        "quality_report": {"status": "ok"},
    }), encoding="utf-8")
    assert "生成失败" not in _missing_report_detail(str(tmp_path))
