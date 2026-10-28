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
    # #12：第三方库（httpx/starlette 等）日志传播到 root，此前过滤器只挂在
    # textforge 的文件 handler 上会被绕过；root logger 同样挂过滤器兜底脱敏。
    logging.getLogger().addFilter(SensitiveFilter())
    log_queue = queue.Queue(-1)
    queue_handler = QueueHandler(log_queue)
    logger.addHandler(queue_handler)
    listener = QueueListener(log_queue, file_handler)
    listener.start()
    return logger, listener
