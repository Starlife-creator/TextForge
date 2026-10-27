import json, re, logging

# 归一化后的敏感键名集合：键名先 lower、再去掉 _ - 空格，命中即脱敏。
# 这样 `api_key` / `apiKey` / `API-KEY` / `access_token` 都能命中；
# 同时不会误伤 `keyword` / `tokenize` 这类普通词（归一化后不在集合内）。
_SENSITIVE_KEYS_NORMALIZED = {
    "apikey", "token", "authorization", "password", "secret",
    "accesstoken", "secretkey", "auth", "credential",
}


def _normalize_key(k) -> str:
    return re.sub(r"[\s_\-]+", "", str(k).lower())


def _is_sensitive_key(k) -> bool:
    n = _normalize_key(k)
    if n in _SENSITIVE_KEYS_NORMALIZED:
        return True
    # 子串兜底：`openai_api_key` / `github_secret` 这类复合键名
    # 归一化后是 `openaiapikey`，精确集合不命中，但子串命中。
    return any(s in n for s in ("apikey", "accesstoken", "authorization", "password", "secretkey"))

# 字符串内嵌密钥（如 "api_key=xxx"、"token: xxx"、"Bearer xxx"、URL 查询参数
# "?api_key=xxx&token=yyy"）的脱敏模式；日志 SensitiveFilter 与 redact 兜底共用同一
# 口径，避免两侧正则漂移。分隔符含 ? & 以覆盖 URL query；值截断字符含 & 避免连带
# 脱敏到下一个参数。
_SECRET_PATTERN = re.compile(
    r'(api[_-]?key|token|authorization|password|secret)(["\s:=?&]+)([^\s",}&]+)',
    re.IGNORECASE,
)
# Bearer 形态：Authorization: Bearer <token>，整段凭据在 Bearer 之后。
_BEARER_PATTERN = re.compile(r'(Bearer\s+)([^\s",}&]+)', re.IGNORECASE)


def _mask_text(text: str) -> str:
    text = _BEARER_PATTERN.sub(r"\1***", text)
    return _SECRET_PATTERN.sub(r"\1\2***", text)


def redact(data):
    if isinstance(data, dict):
        return {k: ('***' if _is_sensitive_key(k) else redact(v)) for k, v in data.items()}
    if isinstance(data, list):
        return [redact(x) for x in data]
    # 字符串元素（如 config_drift 里 "模型 … api_key=xxx" 的人类可读差异串）也要兜底，
    # 否则历史残留明文会原样进入状态响应与 runs.json。
    if isinstance(data, str):
        return _mask_text(data)
    return data

class SensitiveFilter(logging.Filter):
    def filter(self, record):
        # 必须先 getMessage()：日志常用 logger.info("key=%s", secret) 把密钥放在
        # record.args 里，只看 record.msg 会漏掉，密钥会原样落进 backend.log。
        # getMessage 会把 args 合并进来（失败则退回 str(record.msg)）。
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        try:
            obj = json.loads(msg)
            record.msg = json.dumps(redact(obj), ensure_ascii=False)
        except (json.JSONDecodeError, TypeError):
            record.msg = _mask_text(msg)
        # #12：异常栈文本（record.exc_text）是独立于消息的第二段落盘内容，
        # 异常消息里以非常规格式拼接的密钥在这里再跑一遍正则兜底。
        if getattr(record, "exc_text", None):
            record.exc_text = _mask_text(record.exc_text)
        # 消息已合并进 record.msg，args 必须清空，否则格式化阶段会二次插值。
        record.args = ()
        return True
