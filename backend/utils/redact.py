import json, re, logging
SENSITIVE_KEYS = {'api_key', 'token', 'authorization', 'password', 'secret'}

def redact(data):
    if isinstance(data, dict):
        return {k: ('***' if k.lower() in SENSITIVE_KEYS else redact(v)) for k, v in data.items()}
    if isinstance(data, list):
        return [redact(x) for x in data]
    return data

class SensitiveFilter(logging.Filter):
    def filter(self, record):
        msg = str(record.msg)
        try:
            obj = json.loads(msg)
            record.msg = json.dumps(redact(obj), ensure_ascii=False)
        except (json.JSONDecodeError, TypeError):
            msg = re.sub(r'(api[_-]?key|token|authorization|password|secret)(["\s:=]+)([^\s",}]+)', r'\1\2***', msg, flags=re.IGNORECASE)
            record.msg = msg
        return True
