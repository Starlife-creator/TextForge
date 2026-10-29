import json, re, logging

# 归一化后的敏感键名集合：键名先 lower、再去掉 _ - 空格，命中即脱敏。
# 这样 `api_key` / `apiKey` / `API-KEY` / `access_token` 都能命中；
# 同时不会误伤 `keyword` / `tokenize` / 人名 `Auth` / `Token`（归一化后不在集合内）。
_SENSITIVE_KEYS_NORMALIZED = {
    "apikey", "token", "authorization", "password", "secret",
    "accesstoken", "secretkey",
}


def _normalize_key(k) -> str:
    return re.sub(r"[\s_\-]+", "", str(k).lower())


# 泛词子串：`openai_api_key` / `github_secret` / `refresh_token` 这类复合键名归一化后
# 不在精确集合里，但含有这些词根。**命中泛词不再等于必须遮**——`prompt_tokens`、
# `completion_tokens`、estimate 的 `tokens` 都是正常业务字段（R5a 的另一半）。
_GENERIC_KEY_HINTS = ("apikey", "accesstoken", "authorization", "password", "secretkey",
                      "token", "secret", "credential")

# 值形状：像凭据才遮。要么是已知服务商前缀（sk-/ghp-/AIza/xox…/ya29.），
# 要么是 ≥16 个「密码字符」（不含空白，中文散文天然不命中）。
_CREDENTIAL_VALUE = (r"sk-[A-Za-z0-9_\-]{4,}|ghp-[A-Za-z0-9]{4,}|AIza[A-Za-z0-9_\-]{4,}"
                     r"|xox[abprs]?-[A-Za-z0-9_\-]{4,}|ya29\.[A-Za-z0-9_\-]{4,}"
                     r"|[A-Za-z0-9_:.\-+/=]{16,}")
_CREDENTIAL_VALUE_RE = re.compile(r"(?:%s)\Z" % _CREDENTIAL_VALUE)


def _is_sensitive_key(k) -> bool:
    """键名**是否可能**指向凭据（精确集 ∪ 泛词子串）。

    单参语义必须保留：`_fingerprint` 就是单参调用，改成双参会让它退化成「不哈希」，
    于是明文进指纹。要不要真遮一个值，由 redact() 结合值形状判断。
    """
    n = _normalize_key(k)
    if n in _SENSITIVE_KEYS_NORMALIZED:
        return True
    return any(s in n for s in _GENERIC_KEY_HINTS)


def _looks_like_secret(value) -> bool:
    if not isinstance(value, str):
        return False
    s = value.strip()
    return bool(s) and bool(_CREDENTIAL_VALUE_RE.match(s))

# 字符串内嵌密钥（如 "api_key=xxx"、"token: xxx"、"?api_key=xxx&token=yyy"）的脱敏模式。
# 关键收紧：分隔符必须是 = / : / ? / & 这类赋值或 URL 语境，不能只是空格——否则
# 自由文本里的 "secret identity"、"每章 token 数量上限" 会被误改成 "secret ***"。
# 值也要求像凭据（ASCII 6 字符以上），避免吃掉中文叙述。
# 已知取舍（review E 节）：散文形态 "token <空格> sk-..." 不遮——v2 旧正则会遮，
# v3 为避免误咬自由文本改成必须含赋值符后，这种形态不再命中。相对 main（字符串
# 兜底根本没有）仍是净增强，字典/列表里的 api_key/token 键照常被 _is_sensitive_key 命中。
_SECRET_PATTERN = re.compile(
    r'(api[_-]?key|token|authorization|password|secret)(\s*[=:?&]\s*["\']?)([A-Za-z0-9_\-.]{6,})',
    re.IGNORECASE,
)
# Bearer 形态：Authorization: Bearer <token>，整段凭据在 Bearer 之后。
_BEARER_PATTERN = re.compile(r'(Bearer\s+)([^\s",}&]+)', re.IGNORECASE)
# 裸 `key=`/`?key=`：只有值像凭据才遮（R5a：`?key=sk-live-…` 此前完全不命中）。
# 前面用负向后视排除 `monkey=`、`publickey=` 这类撞词。
_BARE_KEY_PATTERN = re.compile(
    r'(?<![A-Za-z0-9_])(key)(\s*[=:?&]\s*["\']?)(?:%s)' % _CREDENTIAL_VALUE,
    re.IGNORECASE)
# 单独出现的凭据串（没有 `xxx=` 前缀），例如异常正文里直接拼了 sk-live-…。
_STANDALONE_TOKEN_PATTERN = re.compile(
    r'(?<![A-Za-z0-9_])((?:sk|ghp|xox[abprs]?|AIza|ya29)[._\-][A-Za-z0-9._\-]{4,})')


def _mask_text(text: str) -> str:
    text = _BEARER_PATTERN.sub(r"\1***", text)
    text = _SECRET_PATTERN.sub(r"\1\2***", text)
    text = _BARE_KEY_PATTERN.sub(r"\1\2***", text)
    return _STANDALONE_TOKEN_PATTERN.sub("***", text)


def redact(data):
    if isinstance(data, dict):
        out = {}
        for k, v in data.items():
            n = _normalize_key(k)
            if n in _SENSITIVE_KEYS_NORMALIZED:
                # 精确键名：无条件遮。值带尾随换行也要遮（前端提交的全局 api_key
                # 没有 trim，这是真实路径）。
                out[k] = '***'
            elif any(s in n for s in _GENERIC_KEY_HINTS) and _looks_like_secret(v):
                # 泛词键名 + 值像凭据 ⇒ 遮；值不是凭据形状（int、dict、中文散文、
                # `prompt_tokens`）⇒ 继续递归，不误伤业务字段。
                out[k] = '***'
            else:
                out[k] = redact(v)
        return out
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
