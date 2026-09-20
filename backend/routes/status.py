from fastapi import APIRouter
from pathlib import Path
import json, logging
from routes import state

router = APIRouter()
logger = logging.getLogger("textforge")

@router.get("/api/status")
async def status(output_path: str | None = None):
    """查询当前/指定输出目录的流水线状态。
    用途：前端刷新或 SSE 重连后恢复 UI（阶段、暂停态、待确认蓝图、用量、错误）。"""
    target = output_path or state.current_output_path
    paused = not state.pause_event.is_set()
    if not target:
        return {"pipeline_running": state.pipeline_running, "paused": paused,
                "run_id": state.current_run_id, "has_progress": False}

    progress_path = Path(target) / "progress.json"
    if not progress_path.exists():
        return {"pipeline_running": state.pipeline_running, "paused": paused,
                "run_id": state.current_run_id, "has_progress": False}
    try:
        progress = json.loads(progress_path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError) as e:
        return {"pipeline_running": state.pipeline_running, "paused": paused,
                "has_progress": True, "progress_error": f"progress.json 无法读取: {e}"}

    batches = progress.get("batches", [])
    data = {
        "pipeline_running": state.pipeline_running,
        "paused": paused,
        "run_id": state.current_run_id,
        "has_progress": True,
        "current_phase": progress.get("current_phase"),
        "novel_name": progress.get("novel_name"),
        "total_chapters": len(progress.get("chapters", [])),
        "resumed": progress.get("resumed", False),
        "blueprint_confirmed": progress.get("blueprint_confirmed", False),
        "usage": progress.get("usage", {}),
        "last_heartbeat": progress.get("last_heartbeat"),
        "batches": {
            "total": len(batches),
            "phase1_done": sum(1 for b in batches if b["status"] in ("phase1_done", "phase3_done")),
            "phase3_done": sum(1 for b in batches if b["status"] == "phase3_done"),
        },
        "error_logs": progress.get("error_logs", [])[-5:],
    }

    # 仅在等待蓝图确认时回传蓝图全文，供前端重新弹窗
    if progress.get("current_phase") == "phase2_waiting":
        story_bible = Path(target) / "Story_Bible.md"
        if story_bible.exists():
            data["blueprint"] = story_bible.read_text(encoding='utf-8')

    # P0.5：诊断摘要预览（供前端「可开关查看本步蓝图摘要」），每文件仅取前 200 字避免接口过大
    summaries_dir = Path(target) / "01_summaries"
    if summaries_dir.is_dir():
        prev = []
        for f in sorted(summaries_dir.glob("batch_*.txt")):
            txt = f.read_text(encoding='utf-8', errors='replace')
            prev.append({"name": f.name, "preview": txt[:200]})
        if prev:
            data["summaries"] = prev
    return data
