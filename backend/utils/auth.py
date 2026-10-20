"""本地 API 的调用方鉴权。

## 为什么需要
后端绑在 127.0.0.1 的随机端口上，但**任何本地进程**读一下 data/port.json
就能直接调接口：
- POST /api/start —— 指定任意 input_path / output_path，以当前用户权限读写磁盘
- POST /api/system/open —— os.startfile 打开任意路径
浏览器里任意网页也可能打过来（text/plain 这类「简单请求」不触发 CORS 预检，
请求会照常执行，只是读不到响应体）。

## token 从哪来、为什么可信
外壳（Tauri）启动时用 CSPRNG 生成随机 token：
1. 只存在于**进程内存**与 sidecar 的环境变量里，**从不落盘** ——
   落盘就等于公开（任何本地进程都读得到 port.json）。
2. 经环境变量 TEXTFORGE_TOKEN 注入 sidecar。
3. 前端只能通过 Tauri IPC（同进程）取到，不会经过网络。

这样：别的本地进程读不到 token；恶意网页更读不到。

## 为什么没配置时默认拒绝
TEXTFORGE_TOKEN 未设置 = 生产默认拒绝（安全优先）。仅当显式设置
TEXTFORGE_DEV=1 时放行（开发/测试模式）。这样便携包里的 backend.exe
被手动双击启动时也会拒绝匿名请求，避免任意本地网页/进程调用 API。
单元测试和 e2e 通过显式设置 TEXTFORGE_DEV=1 绕过鉴权。
"""

import hmac
import os

ENV_KEY = "TEXTFORGE_TOKEN"
HEADER = "X-TextForge-Token"

# 放行的路径：仅健康检查。它不碰磁盘、不返回任何数据，
# 供外壳/脚本确认「后端活着但没配 token」，无泄露价值。
_EXEMPT = {"/api/health"}


def expected_token() -> str | None:
    """当前期望的 token；None 表示未配置（生产模式下将拒绝请求）。"""
    return os.environ.get(ENV_KEY) or None


def is_enforced() -> bool:
    """是否启用鉴权：生产默认开启，仅当 TEXTFORGE_DEV=1 时关闭。"""
    if os.environ.get("TEXTFORGE_DEV") == "1":
        return False
    return expected_token() is not None


def check(headers) -> bool:
    """校验请求头里的 token。用 compare_digest 防时序侧信道。

    头名按小写查找：HTTP 头大小写不敏感，中间件统一转小写存进 dict，
    而 HEADER 常量是混合大小写的 —— 直接用常量查会永远查不到。
    """
    want = expected_token()
    if want is None:
        return True
    lookup = {str(k).lower(): v for k, v in dict(headers).items()}
    got = lookup.get(HEADER.lower())
    if not got:
        return False
    return hmac.compare_digest(str(got), want)


class TokenAuthMiddleware:
    """纯 ASGI 中间件。

    刻意不用 BaseHTTPMiddleware：它会缓冲响应体，直接破坏 SSE
    （/api/events 是长连接流式响应，缓冲后前端再也收不到心跳）。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not is_enforced():
            await self.app(scope, receive, send)
            return

        # CORS 预检必须放行。本中间件在最外层（app.py 里最后注册），会先于
        # CORSMiddleware 拿到请求；而预检 OPTIONS **按规范不带任何自定义头**，
        # 也就永远带不上 X-TextForge-Token —— 若在此拦成 401，浏览器拿不到
        # Access-Control-Allow-*，真正的业务请求根本发不出去（release 下前端
        # 全废）。预检不碰业务、无泄露价值，交给内层 CORSMiddleware 应答即可。
        if scope.get("method") == "OPTIONS":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path in _EXEMPT:
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers") or []}
        if not check(headers):
            body = (b'{"detail":"missing or invalid '
                    + HEADER.encode() + b'"}')
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return

        await self.app(scope, receive, send)