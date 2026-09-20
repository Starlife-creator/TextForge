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
    # #2 章级进度：按逻辑章（虚拟章组折叠到父章）统计总数与已重构数
    logic_ids = _logic_chapter_ids(progress.get("chapters", []))
    reconstructed = sum(
        1 for oid in logic_ids
        if (Path(target) / "02_workspace/reconstructed" / f"chapter_{oid}.txt").exists()
    )
    data = {
        "pipeline_running": state.pipeline_running,
        "paused": paused,
        "run_id": state.current_run_id,
        "has_progress": True,
        "current_phase": progress.get("current_phase"),
        "novel_name": progress.get("novel_name"),
        "total_chapters": len(progress.get("chapters", [])),
        "total_logic_chapters": len(logic_ids),
        "reconstructed_chapters": reconstructed,
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


def _logic_chapter_ids(chapters) -> list:
    """返回最终成书的逻辑章 id：空章剔除；虚拟章组折叠到父章（original_id）；去重保序。"""
    ids = []
    seen = set()
    for ch in chapters:
        if ch.get('is_empty'):
            continue
        if ch.get('is_virtual') or ch.get('is_virtual_parent'):
            out_id = ch.get('original_id') or ch.get('id')
        else:
            out_id = ch.get('id')
        if out_id and out_id not in seen:
            seen.add(out_id)
            ids.append(out_id)
    return ids
