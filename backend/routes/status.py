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

    # B3：刷新后恢复等待态
    data["resume"] = _build_resume(progress, target)
    # A3：章节概览（逻辑章折叠到父章，去重）
    data["chapters_summary"] = _chapter_summary(progress, target)
    return data


def _read_diag_gaps(diag_path):
    """读取诊断 JSON 目录的 gaps，供断层勾选恢复。"""
    diag_dir = Path(diag_path)
    gaps = []
    if not diag_dir.is_dir():
        return gaps
    for f in sorted(diag_dir.glob("diagnose_*.json")):
        try:
            obj = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for g in (obj.get("gaps") if isinstance(obj, dict) else None) or []:
            if isinstance(g, dict):
                gaps.append({"id": g.get("id"), "batch_id": obj.get("batch_id"),
                             "type": g.get("type"), "severity": g.get("severity"),
                             "location": g.get("location"),
                             "description": g.get("description"), "suggestion": g.get("suggestion")})
    return gaps


def _build_resume(progress, target):
    """按当前等待阶段返回可重开弹窗所需的前端数据，否则 None。"""
    phase = progress.get("current_phase")
    if phase == "phase1_fixreview":
        djson = progress.get("diagnose_json")
        return {"key": "fix", "gaps": _read_diag_gaps(djson) if djson else []}
    if phase == "phase3_accept":
        pend = progress.get("accept_pending_batch")
        chapters = []
        if pend is not None:
            for b in progress.get("batches", []):
                if b.get("batch_id") == pend:
                    for cid in b.get("chapter_ids", []):
                        pf = Path(target) / "02_workspace/reconstructed" / f"chapter_{cid}.txt"
                        preview = pf.read_text(encoding="utf-8", errors="replace")[:80] if pf.exists() else ""
                        chapters.append({"id": cid, "preview": preview})
                    break
        return {"key": "accept", "batch_id": pend, "chapters": chapters}
    if phase == "phase3_qc":
        return {"key": "qc", "issues": progress.get("qc_issues", [])}
    return None


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


def _chapter_summary(progress, target) -> list:
    """A3：逻辑章概览 [{id, label, status}]。status ∈ pending|reconstructed。"""
    seen = set()
    rows = []
    for ch in progress.get('chapters', []):
        if ch.get('is_empty'):
            continue
        if ch.get('is_virtual'):
            out_id = ch.get('original_id') or ch.get('id')
        else:
            out_id = ch.get('id')
        if not out_id or out_id in seen:
            continue
        seen.add(out_id)
        recon = (Path(target) / "02_workspace/reconstructed" / f"chapter_{out_id}.txt").exists()
        rows.append({
            "id": out_id,
            "label": ch.get("filename") or str(out_id),
            "status": "reconstructed" if recon else "pending",
        })
    return rows


@router.get("/api/final_files")
async def final_files(output_path: str):
    """C4：列出 03_final 成品目录内的文件（供前端一键打开成品文档）。"""
    fin = Path(output_path) / "03_final"
    if not fin.is_dir():
        return {"files": []}
    files = []
    for f in sorted(fin.iterdir()):
        if f.is_file():
            files.append({"name": f.name, "path": str(f)})
    return {"files": files}


@router.get("/api/comparisons")
async def comparisons(output_path: str):
    """C1：列出 00_comparison 左右对照 md 文件及其原文（前端转义渲染）。"""
    comp_dir = Path(output_path) / "00_comparison"
    if not comp_dir.is_dir():
        return {"items": []}
    items = []
    for f in sorted(comp_dir.glob("chapter_*.comparison.md")):
        items.append({
            "name": f.name,
            "text": f.read_text(encoding='utf-8', errors='replace'),
        })
    return {"items": items}
