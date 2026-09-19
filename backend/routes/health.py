from fastapi import APIRouter
from routes import state
router = APIRouter()

@router.get("/api/health")
async def health():
    return {"status": "ok", "version": "8.8", "pipeline_running": state.pipeline_running}
