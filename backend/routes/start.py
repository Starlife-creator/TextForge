from fastapi import APIRouter, HTTPException
import asyncio, uuid
from routes import state
from models.start_request import StartRequest
from routes.pipeline_runner import run_pipeline
from utils.paths import validate_request_paths, PathValidationError
from utils.errors import raise_internal_error

router = APIRouter()

@router.post("/api/estimate")
async def estimate(req: StartRequest):
    """开跑前预估规模与 token 量级（纯本地读文件，不调模型、不写任何产物）。

    用与 /api/start 完全相同的 StartRequest 与路径校验：预估必须和真跑看到同一套
    约束，否则「预估很省、开跑即 422」。副作用只有一个 —— validate_request_paths
    会创建输出目录（/api/start 本来也会建），不写任何文件。
    """
    try:
        validate_request_paths(req)
    except PathValidationError as e:
        raise HTTPException(422, str(e))
    from core.estimate import estimate_run

    try:
        # #4：estimate_run 扫描输入文件并做字数/统计，属阻塞 CPU+磁盘 I/O，
        # 移出事件循环线程，避免开跑前预估把 SSE 心跳与并发请求堵在事件循环上。
        return await asyncio.to_thread(estimate_run, req)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        # #6：500 不回传内部细节。
        raise raise_internal_error(e, "规模预估失败，详情请查看后端日志", "estimate_run")


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
