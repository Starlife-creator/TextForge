"""#18 诊断包单测：成员齐全、密钥不落包、日志尾部截断、无任务目录也能打包。"""

import io
import json
import zipfile
from pathlib import Path


from utils.diagnostics import (
    build_diagnostic_bundle,
    _system_snapshot,
)


def _make_output(tmp_path: Path) -> Path:
    out = tmp_path / "book"
    diag = out / "_diagnostics"
    diag.mkdir(parents=True)
    (out / "progress.json").write_text(json.dumps({
        "novel_name": "测试书",
        "current_phase": "phase3",
        "input_path": "/tmp/novel.txt",
        "input_mode": "single_file",
        "chapters": [{"id": "1"}, {"id": "2"}],
        "batches": [{"status": "phase3_done"}],
        "usage": {"prompt_tokens": 100},
        "quality_report": {"status": "passed"},
        "error_logs": [{"message": f"错误 {i}"} for i in range(5)],
        "models": {"default": {"api_key": "sk-secret-zzz-123456",
                               "api_url": "http://127.0.0.1:1234/v1"}},
        "config_drift": [{"field": "context_window", "old": "8192", "new": "16384"}],
    }, ensure_ascii=False), encoding="utf-8")
    (diag / "quality_report.json").write_text(json.dumps({
        "status": "passed", "headline": "测试报告", "chapters": []},
        ensure_ascii=False), encoding="utf-8")
    return out


def _read_members(blob: bytes) -> dict[str, str]:
    out: dict[str, str] = {}
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        for name in zf.namelist():
            out[name] = zf.read(name).decode("utf-8")
    return out


def test_bundle_contains_expected_members(tmp_path: Path):
    out = _make_output(tmp_path)
    log = tmp_path / "backend.log"
    log.write_text("[info] 正常日志一行\n", encoding="utf-8")

    blob = build_diagnostic_bundle(out, log_path=log)
    members = _read_members(blob)

    expected = {"manifest.json", "system_info.json", "backend.log",
                "progress_summary.json", "quality_report.json", "README.md"}
    assert expected == set(members)

    manifest = json.loads(members["manifest.json"])
    assert set(manifest["members"]) == expected
    assert manifest["source_output_dir"] == str(out)

    summary = json.loads(members["progress_summary.json"])
    assert summary["current_phase"] == "phase3"
    assert summary["total_chapters"] == 2
    assert summary["quality_report_status"] == "passed"
    assert len(summary["error_logs"]) == 5


def test_bundle_never_contains_planted_secret(tmp_path: Path):
    out = _make_output(tmp_path)
    log = tmp_path / "backend.log"
    log.write_text("[info] 启动\n", encoding="utf-8")

    blob = build_diagnostic_bundle(out, log_path=log)
    members = _read_members(blob)
    for name, text in members.items():
        assert "sk-secret-zzz-123456" not in text, f"密钥泄露在 {name}"


def test_bundle_without_output_dir_still_builds(tmp_path: Path):
    log = tmp_path / "backend.log"
    log.write_text("日志内容\n", encoding="utf-8")
    blob = build_diagnostic_bundle(None, log_path=log)
    members = _read_members(blob)
    assert "progress_summary.json" not in members
    assert "quality_report.json" not in members
    assert "system_info.json" in members
    assert "backend.log" in members


def test_bundle_truncates_log_tail(tmp_path: Path):
    out = _make_output(tmp_path)
    log = tmp_path / "backend.log"
    log.write_text("\n".join(f"第 {i} 行日志" for i in range(5000)), encoding="utf-8")

    blob = build_diagnostic_bundle(out, log_path=log, max_log_lines=100)
    members = _read_members(blob)
    body = members["backend.log"].splitlines()
    # 头部说明行 + 最后 100 行正文
    assert len(body) == 101
    assert "仅附最后 100 行" in body[0]
    assert "第 4999 行日志" in body[-1]


def test_system_snapshot_fields_present():
    info = _system_snapshot()
    for key in ("version", "python", "platform", "machine", "cwd", "app_dir"):
        assert key in info


def test_quality_report_missing_is_skipped(tmp_path: Path):
    out = _make_output(tmp_path)
    (out / "_diagnostics" / "quality_report.json").unlink()
    log = tmp_path / "backend.log"
    log.write_text("x\n", encoding="utf-8")
    blob = build_diagnostic_bundle(out, log_path=log)
    members = _read_members(blob)
    assert "quality_report.json" not in members
