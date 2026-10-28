"""B3（review A4.1）：只换 models 里的 api_key，config_drift 必须报「分阶段模型」漂移。

旧实现用 _cfg_brief（先 redact 成 ***）做比较，两侧密钥都变成 ***，漂移漏报。
新实现用 _fingerprint（敏感键取 sha256 前 8 位）做比较，密钥一变哈希就变。
把 _fingerprint 退回 _cfg_brief，本测试必须变红（返回 []）。
"""
from __future__ import annotations

from routes.pipeline_runner import _config_drift


class _Req:
    def __init__(self, **kw):
        self.__dict__.update(kw)


_MODELS_OLD = {"phase1": {"model": "gpt-x", "api_key": "sk-old-aaaa1111"},
               "phase2": {"model": "gpt-x", "api_key": "sk-old-bbbb2222"}}
_MODELS_NEW = {"phase1": {"model": "gpt-x", "api_key": "sk-new-cccc3333"},
               "phase2": {"model": "gpt-x", "api_key": "sk-new-dddd4444"}}


def _base(models) -> dict:
    return {
        "api_config": {"model": "m1", "api_url": "http://a/v1/chat/completions",
                       "context_window": 8000},
        "input_path": "/in/a.txt",
        "models": models,
    }


def test_config_drift_detects_api_key_rotation() -> None:
    req = _Req(model="m1", api_url="http://a/v1/chat/completions", context_window=8000,
               input_path="/in/a.txt", models=_MODELS_NEW)
    diffs = _config_drift(_base(_MODELS_OLD), req)
    assert diffs, "只换 api_key 却未报漂移（指纹未生效？）"
    assert any("分阶段模型" in d for d in diffs), f"漂移里没有分阶段模型：{diffs!r}"


def test_config_drift_no_false_positive_when_identical() -> None:
    req = _Req(model="m1", api_url="http://a/v1/chat/completions", context_window=8000,
               input_path="/in/a.txt", models=_MODELS_OLD)
    assert _config_drift(_base(_MODELS_OLD), req) == []
