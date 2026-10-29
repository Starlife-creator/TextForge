import logging, queue
from logging.handlers import RotatingFileHandler, QueueHandler, QueueListener
from utils.appdir import ensure_data_dir
from utils.redact import SensitiveFilter

def setup_logging():
    # 数据目录统一走 appdir：便携包下日志落在 exe 旁的 data/logs，
    # 整个文件夹拷走即带走日志（原先固定 %APPDATA%，便携包拷走后日志留原地）。
    log_dir = ensure_data_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "backend.log"
    logger = logging.getLogger("textforge")
    logger.setLevel(logging.INFO)
    # v8.8 修正：避免向上传播到 root logger 导致重复输出
    logger.propagate = False
    file_handler = RotatingFileHandler(log_path, maxBytes=10*1024*1024, backupCount=5, encoding='utf-8')
    file_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
    file_handler.setLevel(logging.INFO)
    file_handler.addFilter(SensitiveFilter())
    # #12：root 上挂一份，只覆盖「直接从 root 发」的记录。第三方库（httpx/uvicorn）
    # 是从自己的子 logger 发出的：callHandlers 只跑 handler 自己的 filter，祖先 logger
    # 的 filter 从不参与 ⇒ 这一行对它们无效，真正的兜底见 attach_redaction_to_handlers。
    logging.getLogger().addFilter(SensitiveFilter())
    log_queue = queue.Queue(-1)
    queue_handler = QueueHandler(log_queue)
    logger.addHandler(queue_handler)
    listener = QueueListener(log_queue, file_handler)
    listener.start()
    return logger, listener


# uvicorn 把异常正文（含请求头/URL 里的密钥）写进这几个 logger；httpx 在被调用方
# 记录错误响应时同样可能带上 Authorization 头。
THIRD_PARTY_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access",
                       "httpx", "httpcore", "starlette")


def attach_redaction_to_handlers(loggers: tuple[str, ...] = THIRD_PARTY_LOGGERS) -> int:
    """给这些 logger 的每个 handler 挂 SensitiveFilter（已挂过则跳过），返回新增数。

    必须在 FastAPI 启动段调用而不是 import 期：main.py 是「先 import app（此时
    setup_logging 已跑完）→ 再构造 uvicorn Config/Server」，而 uvicorn 的 handler
    是 dictConfig 在那之后才建立的——import 期去挂，挂到的是空列表。
    """
    attached = 0
    for name in loggers:
        for h in logging.getLogger(name).handlers:
            if not any(isinstance(f, SensitiveFilter) for f in h.filters):
                h.addFilter(SensitiveFilter())
                attached += 1
    return attached
