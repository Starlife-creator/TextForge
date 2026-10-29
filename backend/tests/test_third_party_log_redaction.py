"""B1.5：第三方库日志里的密钥只有挂在 handler 上才遮得住。

CPython 的 `Logger.callHandlers` 只执行 handler 自己的 filter；祖先 logger 上的
filter 对传播来的记录根本不参与。所以 `logging.getLogger().addFilter(...)` 这种
写法看着像兜底，实际一条也挡不住——第 3 个用例把这条现状钉成断言。
"""
from __future__ import annotations

import ast
import io
import logging
from contextlib import contextmanager
from pathlib import Path

from utils.logging_setup import THIRD_PARTY_LOGGERS, attach_redaction_to_handlers
from utils.redact import SensitiveFilter

SECRET = "sk-live-ABCDEFGHIJKLMNOP1234"
APP_PY = Path(__file__).resolve().parents[1] / "app.py"


@contextmanager
def isolated_logger(name: str):
    """临时接管一个第三方 logger（换成内存 handler），退出按原样还回去。"""
    lg = logging.getLogger(name)
    saved = (list(lg.handlers), lg.propagate, lg.level)
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(message)s"))
    lg.handlers = [handler]
    lg.propagate = False
    lg.setLevel(logging.WARNING)
    try:
        yield lg, buf
    finally:
        lg.handlers, lg.propagate, lg.level = saved


def test_attach_masks_records_written_by_uvicorn_handler():
    """不给 handler 挂过滤器 ⇒ 本用例红（这就是改前的现状）。"""
    with isolated_logger("uvicorn.error") as (lg, buf):
        assert attach_redaction_to_handlers(("uvicorn.error",)) == 1
        lg.warning("上游请求失败 api_key=%s", SECRET)
        out = buf.getvalue()
    assert SECRET not in out
    assert "api_key=***" in out
    assert "上游请求失败" in out


def test_attach_is_idempotent():
    """重复调用不叠过滤器：叠上去每条日志都要多跑一遍正则。"""
    with isolated_logger("httpx") as (lg, buf):
        attach_redaction_to_handlers(("httpx",))
        assert attach_redaction_to_handlers(("httpx",)) == 0
        hit = [f for f in lg.handlers[0].filters if isinstance(f, SensitiveFilter)]
        assert len(hit) == 1
        lg.warning("读超时 api_key=%s", SECRET)
        assert "api_key=***" in buf.getvalue()


def test_ancestor_logger_filter_alone_leaves_plaintext():
    """钉住「为什么必须挂 handler」：root 上的 filter 对子 logger 传播来的记录无效。
    哪天 CPython 改成会跑祖先 filter，本用例红——那时这段实现可以简化。"""
    root = logging.getLogger()
    child = logging.getLogger("uvicorn.access")
    saved_root = (list(root.handlers), list(root.filters))
    saved_child = (list(child.handlers), child.propagate)
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(message)s"))
    try:
        root.handlers = [handler]
        root.addFilter(SensitiveFilter())        # 现状写法：挂在 logger 上
        child.handlers = []
        child.propagate = True
        child.warning("连接失败 authorization: Bearer %s", SECRET)
        assert SECRET in buf.getvalue()          # 明文照样落进 handler
    finally:
        root.handlers, root.filters = saved_root
        child.handlers, child.propagate = saved_child


def test_lifespan_attaches_before_yield():
    """结构性守卫：挂点必须在启动段（早于 yield）。
    挪进 setup_logging()（import 期）⇒ 本用例红，而那时 uvicorn 还没有 handler。"""
    tree = ast.parse(APP_PY.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "lifespan")
    calls = [n.lineno for n in ast.walk(fn)
             if isinstance(n, ast.Call)
             and getattr(n.func, "id", "") == "attach_redaction_to_handlers"]
    yields = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Yield)]
    assert calls, "lifespan 里没有调用 attach_redaction_to_handlers"
    assert yields, "lifespan 结构变了（找不到 yield），请重新确认挂点"
    assert min(calls) < min(yields)


def test_covered_loggers_include_third_party_namespaces():
    """清单本身也是判据之一：漏掉 uvicorn.error 就等于没挂。"""
    for name in ("uvicorn", "uvicorn.error", "httpx"):
        assert name in THIRD_PARTY_LOGGERS
