"""第二轮加固回归：#3 重写上限常量、#6 错误出口不泄露细节、#9 对照稿路径白名单、
#12 日志脱敏覆盖 URL 查询参数与异常栈文本。"""
from __future__ import annotations

import logging

import pytest

from core.refactor import AUTO_REWRITE_MAX_PER_RUN, REGEN_MAX_PER_CHAPTER
from routes.status import _COMPARISON_NAME_RE
from utils.errors import raise_internal_error
from utils.redact import SensitiveFilter, redact


# ---------- #12 日志脱敏 ----------

def test_redact_url_query_secret_masked():
    text = "GET https://api.example/v1/chat?api_key=sk-abc123&token=tok-xyz987"
    out = redact(text)
    assert "sk-abc123" not in out and "tok-xyz987" not in out
    assert "***" in out


def test_redact_nested_dict_keeps_structure():
    data = {"models": {"refactor": {"api_key": "sk-live", "model": "gpt-x"}}, "ok": True}
    out = redact(data)
    assert out["models"]["refactor"]["api_key"] == "***"
    assert out["models"]["refactor"]["model"] == "gpt-x"
    assert out["ok"] is True


def test_sensitive_filter_masks_exc_text():
    record = logging.LogRecord(
        name="httpx", level=logging.ERROR, pathname=__file__, lineno=1,
        msg="request failed", args=(), exc_info=None,
    )
    record.exc_text = 'Traceback ... Authorization: Bearer tok-secret-zzz at line 3'
    SensitiveFilter().filter(record)
    assert "tok-secret-zzz" not in (record.exc_text or "")


# ---------- #9 对照稿路径白名单 ----------

@pytest.mark.parametrize("name,allowed", [
    ("chapter_3.comparison.md", True),
    ("chapter_12_二期.comparison.md", True),
    ("../chapter_3.comparison.md", False),
    ("sub/chapter_3.comparison.md", False),
    ("chapter_3.md", False),
    ("chapter_3\\comparison.md", False),
    ("chapter_3.comparison.json", False),
])
def test_comparison_name_whitelist(name, allowed):
    assert bool(_COMPARISON_NAME_RE.match(name)) is allowed


# ---------- #3 重写上限 ----------

def test_regen_limit_aligned_with_rewrite_guard():
    # 与主路径 AUTO_REWRITE_MAX_PER_RUN=3 对齐，防止单章无限驳回计费。
    assert REGEN_MAX_PER_CHAPTER == 3
    assert AUTO_REWRITE_MAX_PER_RUN == REGEN_MAX_PER_CHAPTER


# ---------- #6 错误出口不泄露内部细节 ----------

def test_internal_error_hides_exception_detail():
    exc = ValueError("/home/user/secret/output/progress.json 不存在")
    http = raise_internal_error(exc, "单章重生成失败，详情请查看后端日志", "regen_chapter")
    assert http.status_code == 500
    assert http.detail == "单章重生成失败，详情请查看后端日志"
    assert "/home/user" not in str(http.detail)
