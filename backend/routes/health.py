from fastapi import APIRouter
from routes import state
from utils.version import VERSION
router = APIRouter()

@router.get("/api/health")
async def health():
    return {"status": "ok", "version": VERSION, "pipeline_running": state.pipeline_running}
