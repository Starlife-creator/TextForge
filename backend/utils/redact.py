import json, re, logging
SENSITIVE_KEYS = {'api_key', 'token', 'authorization', 'password', 'secret'}

# 字符串内嵌密钥（如 "api_key=xxx"、"token: xxx"、"Bearer xxx"）的脱敏模式；
# 日志 SensitiveFilter 与 redact 兜底共用同一口径，避免两侧正则漂移。
_SECRET_PATTERN = re.compile(
    r'(api[_-]?key|token|authorization|password|secret)(["\s:=]+)([^\s",}]+)',
    re.IGNORECASE,
)

def redact(data):
    if isinstance(data, dict):
        return {k: ('***' if k.lower() in SENSITIVE_KEYS else redact(v)) for k, v in data.items()}
    if isinstance(data, list):
        return [redact(x) for x in data]
    # 字符串元素（如 config_drift 里 "模型 … api_key=xxx" 的人类可读差异串）也要兜底，
    # 否则历史残留明文会原样进入状态响应与 runs.json。
    if isinstance(data, str):
        return _SECRET_PATTERN.sub(r'\1\2***', data)
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
            msg = _SECRET_PATTERN.sub(r'\1\2***', msg)
            record.msg = msg
        # 消息已合并进 record.msg，args 必须清空，否则格式化阶段会二次插值。
        record.args = ()
        return True
