"""R5（第九轮）：错误正文是密钥进入磁盘与响应的最后一条通道。

四面都钉：源头（log_error 写 progress.json）、注册表写侧（runs.json 字节）、
注册表读侧（修复前已落盘的残留）、响应侧（/api/status 与 /api/output/inspect）。
反向断言：去掉任一处 redact ⇒ 对应用例红；每条都标在用例 docstring 里。
"""
from __future__ import annotations

import asyncio
import json

import pytest

import routes.history as history
import routes.output as output
import routes.pipeline_runner as pipeline_runner
import routes.state as state
import routes.status as status_module

SECRET = "sk-live-ABCDEFGHIJKLMNOP1234"
PLAIN = "上游拒绝：api_key=" + SECRET


def _registry(tmp_path, monkeypatch):
    """把历史注册表指到 tmp，返回 (data_dir, runs.json 路径)。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(history, "ensure_data_dir", lambda: data_dir)
    (data_dir / "runs.json").write_text('{"entries": {}}', encoding="utf-8")
    return data_dir, data_dir / "runs.json"


def _entry(path):
    reg = json.loads(path.read_text(encoding="utf-8"))
    assert len(reg["entries"]) == 1
    return next(iter(reg["entries"].values()))


def test_log_error_masks_message_before_writing_progress(tmp_path):
    """去掉 log_error 里的 redact ⇒ 本用例红（明文进 progress.json）。"""
    pf = tmp_path / "progress.json"
    pf.write_text(json.dumps({"chapters": [], "batches": [], "error_logs": []}),
                  encoding="utf-8")

    async def main():
        state.progress_lock = asyncio.Lock()
        await pipeline_runner.log_error(pf, RuntimeError(PLAIN))

    asyncio.run(main())
    raw = pf.read_text(encoding="utf-8")
    assert SECRET not in raw
    assert "api_key=***" in raw
    assert "上游拒绝" in raw          # 原因本身必须留着


def test_upsert_masks_last_error_before_writing_runs_json(tmp_path, monkeypatch):
    """去掉 upsert 里的 redact ⇒ 本用例红。"""
    _, reg_path = _registry(tmp_path, monkeypatch)
    history.upsert(str(tmp_path / "book"), {"novel_name": "甲", "last_error": PLAIN})
    raw = reg_path.read_text(encoding="utf-8")
    assert SECRET not in raw
    assert "api_key=***" in raw
    assert "上游拒绝" in raw


def test_upsert_masks_list_of_error_dicts(tmp_path, monkeypatch):
    """错误正文也可能是结构化列表：键名命中也必须遮（去掉 redact ⇒ 本用例红）。"""
    _, reg_path = _registry(tmp_path, monkeypatch)
    history.upsert(str(tmp_path / "book"), {
        "last_error": [{"api_key": SECRET, "message": "上游 500"}],
    })
    raw = reg_path.read_text(encoding="utf-8")
    assert SECRET not in raw
    assert "***" in raw
    assert "上游 500" in raw


@pytest.mark.parametrize("lead", [189, 190])
def test_truncation_fragments_are_not_left_behind(tmp_path, monkeypatch, lead):
    """先截断会让 `api_key=sk-` 这种碎片停在 200 字符处：短于模式要求的 6 个
    ASCII，于是脱敏正则看不见它，密钥前缀就留在了 runs.json 里。
    把顺序换成 str(errs)[:_TRIM_CHARS] 再 redact ⇒ 本用例红。"""
    _, reg_path = _registry(tmp_path, monkeypatch)
    msg = "x" * lead + f"api_key={SECRET}"
    history.upsert(str(tmp_path / "book"), {"last_error": msg})
    entry = _entry(reg_path)
    assert SECRET not in json.dumps(entry, ensure_ascii=False)
    kept = entry["last_error"]
    assert "api_key=sk-" not in kept
    # 等号之后只能剩下星号：lead=190 时连星号都会被预算截掉，但绝不留密钥字符。
    assert kept.split("api_key=", 1)[1].strip("*") == ""
    assert kept.startswith("x" * lead)
    assert len(kept) <= history._TRIM_CHARS


def test_load_masks_residue_from_before_the_fix(tmp_path, monkeypatch):
    """修复前已经写在 runs.json 里的明文删不掉也没法回填 ⇒ 读侧必须兜底。
    去掉 _load 里的 last_error 那支 ⇒ 本用例红。"""
    _, reg_path = _registry(tmp_path, monkeypatch)
    reg_path.write_text(json.dumps({"entries": {
        "k1": {"output_path": "C:/x", "last_error": PLAIN,
               "config_drift": [f"api_key={SECRET}"]},
    }}), encoding="utf-8")
    loaded = history._load()
    e = loaded["entries"]["k1"]
    assert SECRET not in json.dumps(loaded, ensure_ascii=False)
    assert e["last_error"] == "上游拒绝：api_key=***"
    assert e["config_drift"] == ["api_key=***"]


def _write_progress(tmp_path):
    (tmp_path / "progress.json").write_text(json.dumps({
        "novel_name": "甲", "current_phase": "phase2", "chapters": [],
        "batches": [{"status": "phase1_done"}],
        "error_logs": [{"ts": "2026-10-29T00:00:00Z", "kind": "api_error",
                        "code": "400", "message": PLAIN}],
    }, ensure_ascii=False), encoding="utf-8")
    return tmp_path


def test_status_and_inspect_both_mask_error_logs(tmp_path):
    """两条读路径展示同一个字段，脱敏结果必须相等（去掉任一处 redact ⇒ 本用例红）。"""
    _write_progress(tmp_path)
    resp = asyncio.run(status_module.status(output_path=str(tmp_path)))
    assert SECRET not in json.dumps(resp, ensure_ascii=False)
    assert resp["error_logs"][0]["message"] == "上游拒绝：api_key=***"

    info = output._summarize(str(tmp_path))
    assert SECRET not in json.dumps(info, ensure_ascii=False)
    assert info["error_logs"] == resp["error_logs"]


def test_diagnostic_bundle_is_masked(tmp_path):
    """诊断包会外发给别人：去掉 _progress_summary 末尾那次 redact ⇒ 本用例红。"""
    from utils import diagnostics
    _write_progress(tmp_path)
    summary = diagnostics._progress_summary(tmp_path)
    assert SECRET not in json.dumps(summary, ensure_ascii=False)
    assert summary["error_logs"][0]["message"] == "上游拒绝：api_key=***"


def test_plain_error_text_survives_verbatim(tmp_path, monkeypatch):
    """不劣化：不含凭据形状的中文原因必须原样保留（既有断言「炸了」「最后一条错」靠它）。"""
    _, reg_path = _registry(tmp_path, monkeypatch)
    history.upsert(str(tmp_path / "book"), {"last_error": "炸了"})
    assert _entry(reg_path)["last_error"] == "炸了"
