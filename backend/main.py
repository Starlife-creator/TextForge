import socket, sys, os, time

IS_DEBUG = os.environ.get("TEXTFORGE_DEBUG", "0") == "1"

if sys.version_info[:2] < (3, 12):
    print(f"ERROR: 需要 Python 3.12+，当前 {sys.version}", file=sys.stderr)
    sys.exit(1)

def _bind_socket(port: int = 0):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('127.0.0.1', port))
    return sock

def _append_boot_log(port: int, text: str) -> None:
    """启动兜底日志：logging_setup 之前的崩溃（缺依赖/导入失败）也要留下证据。

    便携包是 GUI 程序，stderr 用户看不到；只写 backend.log 的话，如果 app.py
    导入阶段就崩了，用户只会看到「后端没起来」而没有任何可诊断信息。
    """
    try:
        from utils.appdir import ensure_data_dir
        log_dir = ensure_data_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / "backend-boot.log"
        if path.exists() and path.stat().st_size > 2 * 1024 * 1024:
            path.unlink()
        with open(path, "a", encoding="utf-8") as f:
            f.write(
                f"[{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] "
                f"port={port} pid={os.getpid()} "
                f"version={os.environ.get('TEXTFORGE_VERSION', 'dev')} {text}\n"
            )
    except Exception:
        pass


if __name__ == "__main__":
    dev_port = os.environ.get("TEXTFORGE_DEV_PORT")
    try:
        if IS_DEBUG and dev_port:
            sock = _bind_socket(int(dev_port))
        else:
            sock = _bind_socket(0)
        port = sock.getsockname()[1]
        sock.listen(128)
        _append_boot_log(port, "booting")

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
    except Exception:
        import traceback
        _append_boot_log(locals().get("port", 0), "STARTUP FAILED\n" + traceback.format_exc())
        raise
