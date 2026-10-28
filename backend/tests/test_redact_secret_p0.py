"""P0-1 回归：分阶段模型表 api_key/token/password 不得落盘、进状态回传或历史注册表。

覆盖链路：
  * redact 递归脱敏（models 嵌套 dict / list / 字符串旧契约）；
  * pipeline_runner._cfg_brief 差异文案截断前先脱敏；
  * pipeline_runner.init_progress 持久化 models 时脱敏；
  * history._meta_from_progress / history.upsert 写入 runs.json 前脱敏兜底。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import routes.history as history
from routes import pipeline_runner as runner
from utils.redact import redact

SECRET = "sk-live-secret-1234567890"
SECOND_SECRET = "tok_abcdefghijklmnopqrstuvwxyz"


def _models_with_secrets() -> dict:
    return {
        "phase2": {"model": "gpt-x", "api_url": "https://host/v1", "api_key": SECRET},
        "phase3": "gemini-y",  # 旧契约：仅模型名字符串，无密钥
        "fallback": {"token": SECOND_SECRET, "password": "pw-987"},
    }


def test_redact_recursively_masks_secret_keys() -> None:
    out = redact(_models_with_secrets())
    flat = json.dumps(out, ensure_ascii=False)
    assert SECRET not in flat
    assert SECOND_SECRET not in flat
    assert out["phase2"]["api_key"] == "***"
    assert out["fallback"]["token"] == "***"
    assert out["fallback"]["password"] == "***"
    # 非敏感字段原样保留
    assert out["phase2"]["model"] == "gpt-x"
    assert out["phase3"] == "gemini-y"


def test_cfg_brief_never_emits_plaintext_key() -> None:
    brief = runner._cfg_brief(_models_with_secrets())
    assert SECRET not in brief
    assert SECOND_SECRET not in brief
    assert "gpt-x" in brief  # 非敏感模型名仍可读


def test_init_progress_masks_models_on_persist() -> None:
    req = SimpleNamespace(
        novel_name="测试书", input_path="/tmp/in.txt", input_mode="single_file",
        input_format="txt", output_path="/tmp/out", api_url="https://host/v1",
        model="gpt-x", context_window=8000, rich_text=False,
        models=_models_with_secrets(),
    )
    progress = runner.init_progress(req, "run-1")
    blob = json.dumps(progress["models"], ensure_ascii=False)
    assert SECRET not in blob
    assert SECOND_SECRET not in blob
    assert progress["models"]["phase2"]["api_key"] == "***"
    # 全局 api_config 只存 url/模型名/窗口，不存顶层 api_key
    assert "api_key" not in progress["api_config"]


def test_meta_from_progress_masks_drift() -> None:
    progress = {"config_drift": [f"模型：gpt-x → gpt-y（api_key={SECRET}）"]}
    meta = history._meta_from_progress(progress)
    assert SECRET not in json.dumps(meta["config_drift"], ensure_ascii=False)


def test_upsert_never_writes_plaintext_key(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(history, "_registry_path", lambda: tmp_path / "runs.json")
    history.upsert("/tmp/out", {
        "config_drift": [f"api_key={SECRET}"], "novel_name": "测试书",
    })
    blob = (tmp_path / "runs.json").read_text(encoding="utf-8")
    assert SECRET not in blob
    assert "***" in blob


def test_redact_normalizes_key_separators_and_camelcase() -> None:
    """#6 review：apiKey / API-KEY / access_token 等变体必须命中脱敏。"""
    payload = {
        "apiKey": SECRET,
        "API-KEY": SECRET,
        "access_token": SECOND_SECRET,
        "openai_api_key": SECRET,
        "nested": {"APIKey": SECRET},
        "keyword": "keyword_should_survive",  # 不应误伤普通词
    }
    out = redact(payload)
    assert out["apiKey"] == "***"
    assert out["API-KEY"] == "***"
    assert out["access_token"] == "***"
    assert out["openai_api_key"] == "***"
    assert out["nested"]["APIKey"] == "***"
    assert out["keyword"] == "keyword_should_survive"
