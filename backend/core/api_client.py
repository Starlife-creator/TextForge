import asyncio, json, re, time, logging, httpx, os
from urllib.parse import urlparse

logger = logging.getLogger("textforge")

# D2：流式输出节流阈值（SSE 增量最少多少字符冲刷一次），可用环境变量覆盖
STREAM_THROTTLE = int(os.environ.get("TEXTFORGE_STREAM_THROTTLE", "80"))

def pick_model(req, phase: str) -> str:
    """P4.1 分阶段模型：models[phase] 优先，缺省回退到单一 req.model。
    phase ∈ {diagnose, blueprint, refactor, stitch}，未配置则用全局 model，旧行为不变。"""
    return pick_endpoint(req, phase)["model"]


def _host_of(u: str) -> str:
    """取主机名（忽略 scheme/端口），本机回环统一成 localhost。

    解析不出主机名时返回带 `__` 前缀的占位串（绝不返回空串，也绝不悄悄
    退化成「整条 URL 字符串比较」—— 那会让所有同主机不同路径的配置都被误判成
    换了服务商、进而丢掉用户的 API Key，表现为莫名其妙的 401）。
    具体怎么解读这个占位串由 _same_provider 决定（那里是 fail-open）。
    """
    try:
        h = (urlparse(u or "").hostname or "").lower()
    except Exception as e:
        logger.warning(f"[api] URL 解析失败: {u!r} ({e})")
        return f"__unparsable__:{u}"
    if not h:
        return f"__nohost__:{u}"
    return "localhost" if h in ("127.0.0.1", "::1", "0.0.0.0") else h


def _same_provider(a: str, b: str) -> bool:
    """判断两个 URL 是否指向同一个服务商。

    只比主机名、忽略 scheme/端口，并把 localhost / 127.0.0.1 / ::1 视为同一主机。
    否则下面这些**完全正常**的配置会被误判成「换了服务商」而丢掉密钥 → 401：
      127.0.0.1:1234 ↔ localhost:1234
      http://api.x.com ↔ https://api.x.com
      :8000 ↔ :8080（同一台机器上的反向代理）

    解析不出主机名时**保守判为同一服务商**（保留 Key）：这类 URL 本来也发不出
    成功请求（httpx 会先因 scheme 非法而拒绝），谈不上泄露；而误丢 Key 只会把
    本来能跑的配置变成 401。这是有意的 fail-open。
    """
    ha, hb = _host_of(a), _host_of(b)
    if ha.startswith("__") or hb.startswith("__"):
        return True
    return ha == hb


def pick_endpoint(req, phase: str) -> dict:
    """A2 分阶段端点：返回该阶段 {model, api_url, api_key}。
    models[phase] 可为字符串（旧：同名模型但同一端点）或 dict {model, api_url?, api_key?}。
    缺省回退到全局 req.model / req.api_url / req.api_key，旧行为不变。

    安全：该阶段指向**不同主机**且没配自己的 api_key 时，不把全局密钥发过去 ——
    否则「本地匿名诊断 + 云端 Key」会把云端凭据泄露给第三方。
    同一主机的不同 scheme/端口仍沿用全局 Key，不打断既有配置。
    """
    # 延迟导入：core 不应反向依赖 routes（sse_emit 内部已默认用 current_run_id）
    from routes.events import sse_emit
    models = getattr(req, "models", {}) or {}
    model = req.model
    url = req.api_url
    key = getattr(req, "api_key", "") or ""
    entry = models.get(phase)
    if isinstance(entry, str) and entry:
        model = entry
    elif isinstance(entry, dict):
        if entry.get("model"):
            model = entry["model"]
        if entry.get("api_url"):
            url = entry["api_url"]
        if "api_key" in entry:
            key = entry.get("api_key") or ""
        elif key and not _same_provider(url, req.api_url):
            msg = (f"[api] 阶段 {phase} 指向了不同主机 {_host_of(url)}（全局是 {_host_of(req.api_url)}）"
                   f"且未单独提供 api_key，已丢弃全局密钥以避免向第三方泄露；"
                   f"如该服务需要鉴权，请在「分阶段模型」里为 {phase} 单独填写密钥")
            logger.warning(msg)
            # 必须同时推给用户：只写 backend.log 的话，用户只会看到 401 却在界面上毫无线索
            sse_emit("error", {"kind": "warning", "code": "phase_key_dropped",
                               "message": msg + "（该阶段请求将不带鉴权）"})
            key = ""
    return {"model": model, "api_url": url, "api_key": key}


def make_httpx_client(req, api_url: str | None = None, api_key: str | None = None):
    """统一构造 httpx.AsyncClient：注入鉴权头（Key 仅存内存）、超时、代理、SSL 校验。
    Authorization 作为 client 默认头随每个请求发送，不落盘、不进日志。
    A2：可传入 api_url/api_key 覆盖（分阶段独立服务商），缺省用 req 全局端点。"""
    headers = {"Content-Type": "application/json"}
    key = api_key if api_key is not None else (getattr(req, "api_key", "") or "")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return httpx.AsyncClient(
        timeout=httpx.Timeout(connect=30.0, read=300.0, write=30.0, pool=30.0),
        verify=req.ssl_verify, proxy=req.proxy, follow_redirects=True,
        headers=headers,
    )

class FatalAPIError(Exception):
    def __init__(self, kind, code, message):
        self.kind = kind
        self.code = code
        self.message = message
        super().__init__(message)

class EmptyContentError(Exception):
    pass

class RateLimitError(Exception):
    """P0.4：429 限流。携带服务端 Retry-After（秒，可缺省）。"""
    def __init__(self, retry_after: int | None = None, message: str = ""):
        self.retry_after = retry_after
        self.message = message
        super().__init__(message or f"429 限流，Retry-After={retry_after}")

class RetryableBusinessError(Exception):
    """模型输出不符合契约，需要重试的业务异常基类。"""
    pass


class ContextOverflowError(Exception):
    """请求超出模型/服务端的上下文窗口。

    单独一类（而不是混进 FatalAPIError）的原因：**重试同样的请求毫无意义** ——
    必须把批次拆小再发。所以它必须能被上层识别并触发「拆批」，而不是被当成
    网络抖动盲重试，更不能被当成致命错误直接终止整轮。
    """

    def __init__(self, requested: int | None, available: int | None, raw: str):
        self.requested = requested
        self.available = available
        self.raw = raw
        detail = ""
        if requested and available:
            detail = f"（请求 {requested} token，模型上限 {available} token）"
        elif available:
            detail = f"（模型上限 {available} token）"
        super().__init__(f"上下文超限{detail}：{raw[:200]}")


# 认「上下文超限」的语义关键词。必须同时命中「上下文/token」与「超限」两组词，
# 否则会把无效模型名之类的 400 误判成超限，进而做出毫无意义的拆批。
_CTX_TOPIC = ("context", "token", "prompt length", "input length", "上下文", "最大长度")
_CTX_OVER = ("exceed", "too long", "too large", "maximum", "max ", "limit",
             "out of range", "greater than", "超过", "超出", "过长")

# 从各家报错里扒出「模型上限」数字。顺序即优先级。
_AVAIL_PATTERNS = (
    re.compile(r'exceeds?\s+the\s+available\s+context\s+size[^\d]{0,20}(\d+)', re.I),
    re.compile(r'maximum\s+context\s+length\s+is[^\d]{0,20}(\d+)', re.I),
    re.compile(r'>\s*(\d+)\s*maximum', re.I),
    re.compile(r'exceeds?\s+the\s+maximum\s+(?:number\s+of\s+)?(?:tokens?|length)[^\d]{0,30}(\d+)', re.I),
    re.compile(r'context\s+(?:length|window|size)[^\d]{0,20}(\d+)', re.I),
    re.compile(r'max(?:imum)?\s+(?:context|tokens?|length)[^\d]{0,20}(\d+)', re.I),
    re.compile(r'(?:上限|最大)[^\d]{0,12}(\d+)', re.I),
)
# 请求侧 token 数（用于日志/提示，非必需）
_REQ_PATTERNS = (
    re.compile(r'request[^\d]{0,12}(\d+)\s*tokens?', re.I),
    re.compile(r'(\d+)\s*tokens?\s*>', re.I),
    re.compile(r'输入[^\d]{0,8}(\d+)', re.I),
)


def parse_context_overflow(text: str) -> tuple[int | None, int | None] | None:
    """判断一段错误文本是否为「上下文超限」，并尽力解析 (requested, available)。

    返回 None 表示**不是**超限错误（调用方应走原有错误路径）。
    这套规则是跨厂商的：LM Studio/llama.cpp、OpenAI、Anthropic、Gemini、vLLM、
    Ollama 的同类报错文案都能覆盖，而不是只认某一家。
    """
    if not text:
        return None
    low = text.lower()
    if not any(k in low for k in _CTX_TOPIC):
        return None
    if not any(k in low for k in _CTX_OVER):
        return None
    avail = None
    for p in _AVAIL_PATTERNS:
        m = p.search(text)
        if m:
            avail = int(m.group(1))
            break
    req = None
    for p in _REQ_PATTERNS:
        m = p.search(text)
        if m:
            req = int(m.group(1))
            break
    return req, avail


async def call_with_retry(client, api_url, payload, sse_emit_fn, run_id, max_retries=3,
                         on_retry=None, validate=None):
    """带分策略重试的 API 调用（P0.4）：
    - RetryableBusinessError（缺分隔符等契约错误）：重试 1 次给模型修正机会
    - EmptyContentError（空响应）：按全部次数重试（分开处理）
    - RateLimitError（429）：按 Retry-After / 退避重试
    - 网络错误：指数退避重试

    on_retry(payload, exc) -> None：业务契约错误重试前的钩子，用来**改写 payload**
    把「上一次错在哪」喂回模型。没有它时重试是完全相同的请求，模型无从纠正，
    只能靠采样随机性碰运气（实测同一批一次过、一次不过，整批耗时白扔）。

    validate(result) -> None：结果校验器，抛 RetryableBusinessError 即可触发重试。
    **业务契约校验必须放在这里**，而不是在 call_with_retry 返回之后再做 ——
    后者抛出的异常根本进不了 except 分支，重试形同虚设
    （历史上「===CHAPTER_BREAK=== 数量不符」就是这么一直不重试的）。
    """
    last_exc = None
    for attempt in range(max_retries):
        try:
            result, usage = await _stream_call(client, api_url, payload, sse_emit_fn, run_id)
            if validate is not None:
                validate(result)
            return result, usage, attempt
        except RateLimitError as e:
            delay = (e.retry_after or 0) or (2 * (attempt + 1))
            logger.warning(f"[api] 429 限流 attempt={attempt+1} retry_after={e.retry_after or 'n/a'}")
            if attempt >= max_retries - 1:
                raise
            last_exc = e
            await asyncio.sleep(min(delay, 30))
        except EmptyContentError as e:   # P0.4：空响应与业务错误分开重试
            logger.warning(f"[api] 空响应 attempt={attempt+1}: {str(e)[:200]}")
            if attempt >= max_retries - 1:
                raise
            last_exc = e
            await asyncio.sleep(2 * (attempt + 1))
        except RetryableBusinessError as e:
            # 业务格式错误：只重试 1 次，且带上纠正提示
            logger.warning(f"[api] 业务格式错误 attempt={attempt+1}: {str(e)[:200]}")
            if attempt >= 1:
                raise
            last_exc = e
            if on_retry is not None:
                try:
                    on_retry(payload, e)
                except Exception as hook_err:
                    logger.warning(f"[api] on_retry 钩子异常（忽略）: {hook_err}")
            await asyncio.sleep(2)
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            logger.warning(f"[api] 网络错误 attempt={attempt+1}: {str(e)[:200]}")
            last_exc = e
            await asyncio.sleep(2 * (attempt + 1))
        except FatalAPIError:
            raise
    raise last_exc

async def _stream_call(client, api_url, payload, sse_emit_fn, run_id):
    """v8.8 修正：
    1. 直接发完整 payload；仅当收到 400 且错误文本与 stream_options 相关时才降级重试。
    2. 不再依赖 httpx.HTTPStatusError（stream 不会自动 raise），显式处理状态码。"""
    full_text = []
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    # #2 流式增量节流缓冲：累计 content 到阈值再一次性 emit，避免碎片事件放大 SSE 流量
    _stream = {"buf": ""}

    def _flush_stream_chunk():
        if sse_emit_fn and _stream["buf"]:
            sse_emit_fn("stream_chunk", {"text": _stream["buf"]}, run_id)
            _stream["buf"] = ""

    # 流式模式默认请求 usage（OpenAI 规范字段）；服务端不支持时由下方 400 降级逻辑移除
    if payload.get("stream") and "stream_options" not in payload:
        payload = {**payload, "stream_options": {"include_usage": True}}
    
    async def _do(req_payload):
        async with client.stream("POST", api_url, json=req_payload) as resp:
            if resp.status_code == 400:
                body = await resp.aread()
                text = body.decode('utf-8', errors='replace')
                # 上下文超限：单独成类，交给上层「拆批重试」。重发同样的请求必然再失败，
                # 所以绝不能当作可重试的临时错误，也不能当作致命错误终止整轮。
                ov = parse_context_overflow(text)
                if ov is not None:
                    logger.warning(f"[api] 上下文超限: {text[:200]}")
                    # 被动学习真实窗口：对不暴露窗口的云端 API，这是唯一来源
                    from core.context_probe import observe_overflow
                    observe_overflow(api_url, req_payload.get("model", ""), ov[1])
                    raise ContextOverflowError(ov[0], ov[1], text)
                # 仅当错误确实与 stream_options 相关才降级
                if "stream_options" in text.lower():
                    clean = {k: v for k, v in req_payload.items() if k != "stream_options"}
                    if clean == req_payload:
                        raise FatalAPIError("api_error", 400, text[:500])
                    logger.info("[api] 400 涉及 stream_options，去掉该字段重试")
                    return await _do(clean)
                raise FatalAPIError("api_error", resp.status_code, text[:500])
            elif resp.status_code == 429:   # P0.4：限流，读取 Retry-After 供上层按秒重试
                retry_after = None
                if resp.headers.get("retry-after"):
                    try:
                        retry_after = int(resp.headers["retry-after"])
                    except ValueError:
                        retry_after = None
                await resp.aread()
                raise RateLimitError(retry_after)
            elif resp.status_code != 200:
                _body = (await resp.aread()).decode('utf-8', errors='replace')
                # 413/422 也常被用来表达上下文超限（如某些网关），一并识别
                ov = parse_context_overflow(_body)
                if ov is not None:
                    logger.warning(f"[api] 上下文超限({resp.status_code}): {_body[:200]}")
                    from core.context_probe import observe_overflow
                    observe_overflow(api_url, req_payload.get("model", ""), ov[1])
                    raise ContextOverflowError(ov[0], ov[1], _body)
                raise FatalAPIError("api_error", resp.status_code, _body[:500])
            async for line in resp.aiter_lines():
                chunk = _process_sse_line(line, full_text, usage)
                if chunk:
                    _stream["buf"] += chunk
                    if len(_stream["buf"]) >= STREAM_THROTTLE:
                        _flush_stream_chunk()
    
    await _do(payload)
    _flush_stream_chunk()  # 冲刷末尾不足阈值的残留
    text = "".join(full_text).strip()
    if not text:
        raise EmptyContentError("API 返回空内容")
    return text, usage

def _process_sse_line(line, full_text, usage):
    if not line or not line.startswith("data: "):
        return None
    data = line[6:]
    if data == "[DONE]":
        return None
    try:
        obj = json.loads(data)
        delta = obj.get("choices", [{}])[0].get("delta", {})
        content = delta.get("content")
        if content:
            full_text.append(content)
        if obj.get("usage"):
            u = obj["usage"]
            # 兼容 OpenAI 规范(prompt_tokens) 与部分国产兼容 API(input_tokens)；
            # usage 通常仅末帧携带，以非零值覆盖，避免分帧实现重复累加
            pt = u.get("prompt_tokens", u.get("input_tokens", 0)) or 0
            ct = u.get("completion_tokens", u.get("output_tokens", 0)) or 0
            if pt: usage["prompt_tokens"] = pt
            if ct: usage["completion_tokens"] = ct
        return content if content else None
    except (json.JSONDecodeError, KeyError, IndexError):
        return None
