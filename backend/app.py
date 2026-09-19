from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from routes import state
from utils.logging_setup import setup_logging
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
        state.log_listener.stop()

from routes.health import router as health_router
from routes.start import router as start_router
from routes.events import router as events_router
from routes.control import router as control_router
from routes.status import router as status_router

app = FastAPI(title="TextForge", version="8.8", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "tauri://localhost", "http://tauri.localhost", "https://tauri.localhost",
        "http://localhost:1420", "http://127.0.0.1:1420",
    ],
    allow_methods=["*"], allow_headers=["*"], allow_credentials=False,
)

app.include_router(health_router)
app.include_router(start_router)
app.include_router(events_router)
app.include_router(control_router)
app.include_router(status_router)
