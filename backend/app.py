from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from routes import state
from utils.auth import TokenAuthMiddleware
from utils.logging_setup import setup_logging
from utils.version import VERSION
import asyncio

logger, listener = setup_logging()
state.log_listener = listener

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    if state.pipeline_task and not state.pipeline_task.done():
        state.pipeline_task.cancel()
        try:
            await asyncio.wait_for(state.pipeline_task, timeout=5)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
    if state.log_listener:
        # 测试进程内多次 import app 时，listener._thread 可能已被上一次 stop()
        # join 后置 None；只吞 AttributeError，不吞 join 其他真异常。
        try:
            state.log_listener.stop()
        except AttributeError:
            pass

from routes.health import router as health_router
from routes.start import router as start_router
from routes.events import router as events_router
from routes.control import router as control_router
from routes.status import router as status_router
from routes.system import router as system_router
from routes.output import router as output_router
from routes.history import router as history_router

app = FastAPI(title="TextForge", version=VERSION, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "tauri://localhost", "http://tauri.localhost", "https://tauri.localhost",
        "http://localhost:1420", "http://127.0.0.1:1420",
    ],
    allow_methods=["*"], allow_headers=["*"], allow_credentials=False,
)

# 本地 API 调用方鉴权。放在 CORS **之后**注册，使其在请求链上更靠外层，
# 无效请求不会被 CORS 处理逻辑白跑一遍。生产默认拒绝（未注入 token 也拒绝），
# 仅显式设置 TEXTFORGE_DEV=1 时放行；正常由外壳注入 TOKEN。
app.add_middleware(TokenAuthMiddleware)

app.include_router(health_router)
app.include_router(start_router)
app.include_router(events_router)
app.include_router(control_router)
app.include_router(status_router)
app.include_router(system_router)
app.include_router(output_router)
app.include_router(history_router)
