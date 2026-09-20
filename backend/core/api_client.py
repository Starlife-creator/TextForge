import asyncio, json, time, logging, httpx, os

logger = logging.getLogger("textforge")

# D2：流式输出节流阈值（SSE 增量最少多少字符冲刷一次），可用环境变量覆盖
STREAM_THROTTLE = int(os.environ.get("TEXTFORGE_STREAM_THROTTLE", "80"))

def pick_model(req, phase: str) -> str:
    """P4.1 分阶段模型：models[phase] 优先，缺省回退到单一 req.model。
    phase ∈ {diagnose, blueprint, refactor, stitch}，未配置则用全局 model，旧行为不变。"""
    models = getattr(req, "models", {}) or {}
    return models.get(phase) or req.model

def make_httpx_client(req):
    """统一构造 httpx.AsyncClient：注入鉴权头（Key 仅存内存）、超时、代理、SSL 校验。
    Authorization 作为 client 默认头随每个请求发送，不落盘、不进日志。"""
    headers = {"Content-Type": "application/json"}
    api_key = getattr(req, "api_key", "") or ""
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
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

async def call_with_retry(client, api_url, payload, sse_emit_fn, run_id, max_retries=3):
    """带分策略重试的 API 调用（P0.4）：
    - RetryableBusinessError（缺分隔符等契约错误）：重试 1 次给模型修正机会
    - EmptyContentError（空响应）：按全部次数重试（分开处理）
    - RateLimitError（429）：按 Retry-After / 退避重试
    - 网络错误：指数退避重试"""
    last_exc = None
    for attempt in range(max_retries):
        try:
            result, usage = await _stream_call(client, api_url, payload, sse_emit_fn, run_id)
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
            # 业务格式错误：只重试 1 次
            logger.warning(f"[api] 业务格式错误 attempt={attempt+1}: {str(e)[:200]}")
            if attempt >= 1:
                raise
            last_exc = e
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
                raise FatalAPIError("api_error", resp.status_code,
                    (await resp.aread()).decode('utf-8', errors='replace')[:500])
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
