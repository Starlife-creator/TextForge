import socket

sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind(('127.0.0.1', 0))
sock.listen(128)
port = sock.getsockname()[1]
print(f"socket bound to port {port}", flush=True)

from uvicorn import Config, Server
from app import app
config = Config(app, host="127.0.0.1", port=port, log_level="info", access_log=False)
server = Server(config)
server.run(sockets=[sock])
