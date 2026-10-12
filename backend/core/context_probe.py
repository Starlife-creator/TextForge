"""探测服务端模型真实上下文窗口（通用，不绑定某一家）。

为什么需要：应用让用户手填「上下文」，但这个值**经常和模型实际加载的窗口不符**
（典型：用户填 16384，LM Studio 实际只加载了 8448）。填大了，程序按自己的算式
认为放得下，请求却超限；填小了，白白浪费窗口。这里尽力探测真实值，用来自检与提示。

两条腿，务实优先：
  1. **主动探测**：对已知本地/兼容服务逐个试端点（llama.cpp /props、
     LM Studio /api/v0/models、Ollama /api/show、OpenAI 兼容 /v1/models）。
     全是短超时的 GET/POST，探不到就静默放弃，绝不打断主流程。
  2. **被动学习**：任何一次上下文超限报错里带的「模型上限」都会被记住
     （见 observe_overflow）。这条对**任意厂商**都有效，包括云端 API 不暴露窗口的。

原则：绝不因为探测失败而阻塞或报错 —— 探测只是「锦上添花」的自检。
"""

import logging
from urllib.parse import urlparse

import httpx

logger = logging.getLogger("textforge")

PROBE_TIMEOUT = 4.0

# 从各种响应结构里找上下文长度的候选键。不同厂商叫法不同，全部收进来。
_LIMIT_KEYS = (
    "max_context_length", "max_position_embeddings", "context_length",
    "context_window", "max_model_len", "n_ctx", "n_ctx_train", "max_tokens",
)

# 被动学习到的「(主机, 模型) -> 真实上限」。进程内存活，不落盘（避免污染配置）。
_OBSERVED: dict[tuple[str, str], int] = {}


def base_url(api_url: str) -> str:
    """把 …/v1/chat/completions 归一成 scheme://host:port。"""
    try:
        p = urlparse(api_url or "")
        if p.scheme and p.netloc:
            return f"{p.scheme}://{p.netloc}"
    except Exception:
        pass
    return (api_url or "").rstrip("/")


def observe_overflow(api_url: str, model: str, available: int | None) -> None:
    """把一次报错里解析到的真实上限记下来（供后续提示/自检）。"""
    if not available or available <= 0:
        return
    _OBSERVED[(base_url(api_url), model or "")] = available
    logger.info(f"[ctx] 记录实测上下文上限 {available}（{base_url(api_url)} / {model}）")


def observed_limit(api_url: str, model: str = "") -> int | None:
    return _OBSERVED.get((base_url(api_url), model or ""))


def _dig(obj, depth=0):
    """深度优先找出响应里第一个「像上下文长度」的正整数。

    两级匹配：先按优先级精确匹配已知键名；再按后缀匹配（Ollama 的键形如
    `llama.context_length`、`qwen2.context_length`，精确匹配抓不到）。
    <512 的值一律忽略 —— 那是 max_tokens 之类的采样参数，不是窗口。
    """
    if depth > 6:
        return None
    if isinstance(obj, dict):
        for k in _LIMIT_KEYS:
            v = obj.get(k)
            if isinstance(v, (int, float)) and v >= 512:
                return int(v)
        for k, v in obj.items():
            if (isinstance(v, (int, float)) and v >= 512
                    and any(k == lk or k.endswith("." + lk) for lk in _LIMIT_KEYS)):
                return int(v)
        for v in obj.values():
            r = _dig(v, depth + 1)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _dig(v, depth + 1)
            if r:
                return r
    return None


async def probe_context_limit(api_url: str, model: str = "", *, api_key: str = "",
                              ssl_verify: bool = True, proxy: str | None = None) -> dict | None:
    """尽力探测真实上下文窗口。返回 {"limit","source"} 或 None（探不到）。

    绝不抛异常：任何失败都当作「这家不支持探测」。
    """
    root = base_url(api_url)
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    attempts = (
        ("llama.cpp", "GET", f"{root}/props", None),
        ("lmstudio", "GET", f"{root}/api/v0/models", None),
        ("openai", "GET", f"{root}/v1/models", None),
        ("ollama", "POST", f"{root}/api/show", {"name": model} if model else None),
    )
    async with httpx.AsyncClient(timeout=httpx.Timeout(PROBE_TIMEOUT), verify=ssl_verify,
                                 proxy=proxy, follow_redirects=True, headers=headers) as client:
        for source, method, url, body in attempts:
            try:
                if method == "GET":
                    resp = await client.get(url)
                else:
                    if body is None:
                        continue
                    resp = await client.post(url, json=body)
                if resp.status_code != 200:
                    continue
                limit = _dig(resp.json())
                if limit:
                    return {"limit": limit, "source": source, "url": url}
            except Exception:
                continue
    return None
