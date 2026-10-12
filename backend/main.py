import socket, sys, os, time
from pathlib import Path

IS_DEBUG = os.environ.get("TEXTFORGE_DEBUG", "0") == "1"

if sys.version_info[:2] < (3, 12):
    print(f"ERROR: 需要 Python 3.12+，当前 {sys.version}", file=sys.stderr)
    sys.exit(1)

def _bind_socket(port: int = 0):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('127.0.0.1', port))
    return sock

if __name__ == "__main__":
    dev_port = os.environ.get("TEXTFORGE_DEV_PORT")
    if IS_DEBUG and dev_port:
        sock = _bind_socket(int(dev_port))
    else:
        sock = _bind_socket(0)
    port = sock.getsockname()[1]
    sock.listen(128)
    
    from utils.atomic import atomic_write_json
    from utils.appdir import ensure_data_dir
    # 端口文件必须和 Rust 外壳读的是**同一个**路径，否则前端永远等不到端口。
    # 两边都经由 appdir 解析（便携包 = exe 旁 data/，否则 %APPDATA%）。
    PORT_FILE = ensure_data_dir() / "port.json"
    if PORT_FILE.exists():
        PORT_FILE.unlink()
    atomic_write_json(PORT_FILE, {"port": port, "timestamp": time.time(), "pid": os.getpid()})
    
    from uvicorn import Config, Server
    from app import app
    config = Config(app, log_level="warning", access_log=False)
    server = Server(config)
    server.run(sockets=[sock])
