from fastapi import APIRouter, HTTPException
import asyncio, uuid
from routes import state
from models.start_request import StartRequest
from routes.pipeline_runner import run_pipeline
from utils.paths import validate_request_paths, PathValidationError

router = APIRouter()

@router.post("/api/start")
async def start(req: StartRequest):
    # 前后端双重校验：路径规范化、存在性、输出目录隔离、URL 合法性
    try:
        validate_request_paths(req)
    except PathValidationError as e:
        raise HTTPException(422, str(e))
    async with state.pipeline_lock:
        if state.pipeline_running:
            raise HTTPException(409, "pipeline_already_running")
        state.pipeline_running = True
        state.stop_requested = False
        state.pause_event.set()
        # blueprint_confirmed 的 clear 统一由 phase2_blueprint 负责
        run_id = str(uuid.uuid4())
        state.current_run_id = run_id
        state.current_output_path = req.output_path
        state.pipeline_task = asyncio.create_task(run_pipeline(req, run_id))
    return {"run_id": run_id, "status": "started"}
