import logging, queue, os
from pathlib import Path
from logging.handlers import RotatingFileHandler, QueueHandler, QueueListener
from utils.redact import SensitiveFilter

def setup_logging():
    log_dir = Path(os.environ["APPDATA"]) / "TextForge" / "logs"
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
    log_queue = queue.Queue(-1)
    queue_handler = QueueHandler(log_queue)
    logger.addHandler(queue_handler)
    listener = QueueListener(log_queue, file_handler)
    listener.start()
    return logger, listener
