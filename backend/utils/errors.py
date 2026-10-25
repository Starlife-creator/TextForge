"""对外错误出口统一收口。

#6（安全·错误信息泄露）：多个 500 端点此前把内部异常 `str(e)` 直接回传客户端，
异常文本可能携带绝对路径、操作失败细节、文件系统状态或上游响应片段。本模块统一：
- 422 / 409 / 400 这类**用户输入/状态校验**错误：保留可读文案（问题在用户侧，
  需要明确指引）；
- 500 这类**内部失败**：对外只返回通用文案，详细异常经 `logger.exception`
  进入本地日志，不进入 HTTP 响应。

#6 小项：`ERROR_KIND_MAP` 把常见异常类型归类成稳定的 code/标题，供日志归类与
后续 422/409 提示口径复用；对外 500 响应仍不携带这些细节。
"""

import logging

from fastapi import HTTPException

logger = logging.getLogger("textforge")

#: 异常类型 → 对外归类（code 稳定、title 可读；仅用于日志归类与后续提示映射，
#: 不把异常文本回传客户端）。按子类在前、基类在后的顺序匹配。
ERROR_KIND_MAP: dict[type[Exception], dict[str, str]] = {
    FileNotFoundError: {"code": "file_missing", "title": "文件不存在"},
    PermissionError: {"code": "permission_denied", "title": "权限不足"},
    IsADirectoryError: {"code": "not_a_file", "title": "目标是目录而非文件"},
    TimeoutError: {"code": "upstream_timeout", "title": "模型服务响应超时"},
    ConnectionError: {"code": "upstream_unreachable", "title": "模型服务不可达"},
    OSError: {"code": "filesystem_error", "title": "文件系统错误"},
    ValueError: {"code": "invalid_input", "title": "参数不合法"},
    KeyError: {"code": "missing_field", "title": "缺少必要字段"},
}


def classify_exception(exc: Exception) -> dict[str, str]:
    """把异常归类为 {kind, code, title}；未登记类型回退 internal_error。"""
    for cls, meta in ERROR_KIND_MAP.items():
        if isinstance(exc, cls):
            return {"kind": cls.__name__, **meta}
    return {
        "kind": type(exc).__name__,
        "code": "internal_error",
        "title": "内部错误",
    }


def raise_internal_error(exc: Exception,
                         public: str = "内部错误，详情请查看后端日志",
                         code: str = "internal_error") -> HTTPException:
    """记录完整异常到日志后，抛出不含细节的 500。"""
    kind = classify_exception(exc)
    logger.exception(
        "[%s] 内部失败已记录到日志（归类 %s/%s）", code, kind["kind"], kind["code"])
    return HTTPException(500, public)
