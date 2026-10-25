"""对外错误出口统一收口。

#6（安全·错误信息泄露）：多个 500 端点此前把内部异常 `str(e)` 直接回传客户端，
异常文本可能携带绝对路径、操作失败细节、文件系统状态或上游响应片段。本模块统一：
- 422 / 409 / 400 这类**用户输入/状态校验**错误：保留可读文案（问题在用户侧，
  需要明确指引）；
- 500 这类**内部失败**：对外只返回通用文案，详细异常经 `logger.exception`
  进入本地日志，不进入 HTTP 响应。
"""

import logging

from fastapi import HTTPException

logger = logging.getLogger("textforge")


def raise_internal_error(exc: Exception,
                         public: str = "内部错误，详情请查看后端日志",
                         code: str = "internal_error") -> HTTPException:
    """记录完整异常到日志后，抛出不含细节的 500。"""
    logger.exception("[%s] 内部失败已记录到日志", code)
    return HTTPException(500, public)
