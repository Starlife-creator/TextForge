"""§4-6：URL 内嵌口令（`scheme://user:pw@host`）必须脱敏。

REVIEW-LOG §4 第 6 条（R5a 的剩余缺口）原本写着「`https://user:pw@host` 这类 URL
内嵌口令仍不遮」，并给了回来条件：「一旦出现『把带凭据的 URL 存进 config/models 表』
的真实入口，立刻补」。那个入口一直都在，只是当初没查：

  * 前端 `App.vue` 的 apiUrl 是自由输入框，用户可填任意 URL；
  * `pipeline_runner.init_progress` 把 `req.api_url` 写进 `progress.json` 的 api_config；
  * 实测 `redact()` / `_mask_text()` 对该形态**原样返回**，`scrub_models` 也只洗
    `api_key` 而放过同 dict 里的 `api_url` ⇒ 口令随 progress.json / 诊断包 / 任务历史外发。

本文件的断言分三层，防止「只在单函数上绿」：
  1. `_mask_text` 层：正例遮掉、反例（普通 URL / 邮箱 / 裸 @ / 端口）不动；
  2. `redact` 层：字典里的 `api_config.api_url` 也遮（递归路径）；
  3. **落盘链路层**：`init_progress` 产出的 progress 里不再有明文口令，
     且 `_config_drift` 不因掩码而误报「地址已变更」——这是掩码引入的假漂移风险，
     也是本文件里唯一一条「改了会悄悄伤到用户」的断言。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from routes import pipeline_runner as runner
from utils.redact import _mask_text, _strip_url_credentials, redact

PW = "secretpw123"
URL_WITH_CRED = f"https://user:{PW}@host.example.com/v1/chat/completions"


# ---------------------------------------------------------------- 第 1 层：_mask_text

def test_url_embedded_credential_is_masked() -> None:
    out = _mask_text(URL_WITH_CRED)
    assert PW not in out
    assert out == "https://***:***@host.example.com/v1/chat/completions"


def test_empty_user_segment_is_masked() -> None:
    """`redis://:password@host` 是真实写法，用户段可以为空。"""
    out = _mask_text("redis://:hunter2@cache:6379/0")
    assert "hunter2" not in out
    assert out == "redis://***:***@cache:6379/0"


def test_credential_inside_url_does_not_leave_half_shape() -> None:
    """先遮 URL 再跑裸凭据兜底，避免留下 `u:` 这种半截形状。"""
    out = _mask_text("https://u:sk-live-ABCDEFGHIJ@h/v1")
    assert out == "https://***:***@h/v1"
    assert "u:" not in out


@pytest.mark.parametrize("text", [
    "https://api.example.com/v1/chat/completions",   # 普通 URL 不动
    "http://127.0.0.1:1234/v1/chat/completions",     # 本地端口不动
    "https://host:8080/v1",                          # 端口不是口令
    "a@b.com",                                       # 邮箱不动
    "user@host",                                     # 裸 @ 不动
    "详见 https://docs.example.com/x",                # 正文里的 URL 不动
    "gemini-y",                                      # 模型名不动
])
def test_non_credential_text_is_untouched(text: str) -> None:
    assert _mask_text(text) == text


# ---------------------------------------------------------------- 第 2 层：redact

def test_redact_masks_api_config_api_url() -> None:
    data = {"api_config": {"api_url": URL_WITH_CRED, "model": "gpt-x", "context_window": 8000}}
    out = redact(data)
    blob = json.dumps(out, ensure_ascii=False)
    assert PW not in blob
    assert out["api_config"]["api_url"].startswith("https://***:***@")
    # 非凭据字段照旧可读
    assert out["api_config"]["model"] == "gpt-x"


def test_redact_masks_models_phase_url() -> None:
    """分阶段模型的 api_url 也能带口令（App.vue 的 buildModels 支持独立端点）。"""
    data = {"models": {"diagnose": {"model": "m", "api_url": URL_WITH_CRED}}}
    out = redact(data)
    assert PW not in json.dumps(out, ensure_ascii=False)


# ------------------------------------------------- 第 3 层：落盘链路（防「单函数绿」）

def test_init_progress_does_not_persist_plaintext_url_credential() -> None:
    """这条是验收底线：遮要遮在落盘链路上，不只是 `_mask_text` 单函数绿。"""
    req = SimpleNamespace(
        novel_name="测试书", input_path="/tmp/in.txt", input_mode="single_file",
        input_format="txt", output_path="/tmp/out", api_url=URL_WITH_CRED,
        model="m", context_window=8000, rich_text=False, models={},
    )
    progress = runner.init_progress(req, "run-1")
    blob = json.dumps(progress, ensure_ascii=False)
    assert PW not in blob, "progress.json 里仍有明文口令"
    assert progress["api_config"]["api_url"] == \
        "https://***:***@host.example.com/v1/chat/completions"


def _req(url: str):
    return SimpleNamespace(
        model="m", api_url=url, context_window=8000, input_path="/tmp/i",
        input_mode="single_file", refactor_mode="full_rewrite", refactor_gates={},
        forbidden_canon=[], name_map={}, models={}, diagnose_json=None,
        max_concurrency=1,
    )


def test_config_drift_does_not_false_positive_from_masking() -> None:
    """掩码引入的假漂移：盘上是 `***:***@host`，req 是原始 `u:pw@host`。

    若直接 str 比较，两者永不相等 ⇒ 每次续跑都弹「API 地址已变更」，
    用户会以为配置被人改了。这条钉的就是这个副作用。
    """
    existing = {"api_config": {"api_url": "https://***:***@host.example.com/v1/chat/completions",
                               "model": "m", "context_window": 8000}}
    assert runner._config_drift(existing, _req(URL_WITH_CRED)) == []


def test_config_drift_still_reports_real_host_change_without_leaking() -> None:
    """真换了 host 仍要报，且差异文案里不能再出现明文口令。"""
    existing = {"api_config": {"api_url": "https://***:***@old-host/v1/chat",
                               "model": "m", "context_window": 8000}}
    diffs = runner._config_drift(existing, _req(f"https://user:{PW}@new-host/v1/chat"))
    assert len(diffs) == 1
    assert "new-host" in diffs[0]
    assert PW not in diffs[0]


def test_strip_url_credentials_aligns_both_sides() -> None:
    """`_strip_url_credentials` 自己的语义：整段删掉，不是替换成 `***`。"""
    assert _strip_url_credentials(URL_WITH_CRED) == \
        "https://host.example.com/v1/chat/completions"
    assert _strip_url_credentials("https://***:***@host.example.com/v1/chat/completions") == \
        "https://host.example.com/v1/chat/completions"
    # 无凭据的 URL 原样返回
    plain = "https://api.example.com/v1/x"
    assert _strip_url_credentials(plain) == plain
