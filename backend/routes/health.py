from fastapi import APIRouter
from routes import state
router = APIRouter()

@router.get("/api/health")
async def health():
    return {"status": "ok", "version": "0.9.1", "pipeline_running": state.pipeline_running}
