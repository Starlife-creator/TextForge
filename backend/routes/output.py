"""输出目录管理：查看进度状态 / 一键清空（供 GUI 使用）。

背景：用户在中途断裂后需要重跑，但之前只能手动去文件夹里删。这里给出两个能力：
  - inspect：这个目录里有没有 TextForge 的进度？跑到哪了？失败原因是什么？
  - clear：只删 **TextForge 自己的产物**，绝不碰用户放在同一目录里的其它文件。

安全底线：clear 采用**白名单**逐个删除，绝不对输出目录整体 rmtree ——
用户完全可能把输出目录设成桌面/文档/某个已有项目目录，一把梭会毁掉无关数据。
"""

import json
import logging
import shutil
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException, Query

from routes import state

logger = logging.getLogger("textforge")

router = APIRouter()

# TextForge 写出的产物（白名单）。清空时只删这些。
ARTIFACTS = ("01_summaries", "02_workspace", "03_final", "_diagnostics",
             "_versions", "_backup", "Story_Bible.md", "progress.json")


def _summarize(output_path: str) -> dict:
    p = Path(output_path)
    if not p.exists():
        return {"exists": False, "resumable": False, "artifacts": [],
                "finished": False, "current_phase": None, "total_chapters": 0,
                "batches_done": 0, "batches_total": 0, "error_logs": []}
    info = {
        "exists": True,
        "artifacts": [n for n in ARTIFACTS if (p / n).exists()],
        "resumable": False, "finished": False, "current_phase": None,
        "total_chapters": 0, "batches_done": 0, "batches_total": 0,
        "novel_name": None, "error_logs": [],
    }
    pf = p / "progress.json"
    if pf.exists():
        try:
            from utils.progress_io import read_progress
            prog = read_progress(pf)
        except Exception as e:
            # 读不了就如实告知，绝不假装"什么都没有"
            info["progress_error"] = str(e)[:200]
            prog = {}
        phase = prog.get("current_phase")
        batches = prog.get("batches", []) or []
        info.update({
            "current_phase": phase,
            "novel_name": prog.get("novel_name"),
            "total_chapters": len(prog.get("chapters", []) or []),
            "batches_total": len(batches),
            "batches_done": sum(1 for b in batches if b.get("status") == "phase3_done"),
            "finished": bool(prog.get("finished_at")),
            # 可续跑：有阶段标记且尚未跑完 phase4（与 _load_or_init_progress 的判据一致）
            "resumable": bool(phase) and phase != "phase4",
            "error_logs": (prog.get("error_logs") or [])[-5:],
        })
    return info


@router.get("/api/output/inspect")
async def output_inspect(output_path: str = Query(...)):
    """查看输出目录里的进度状态：是否可续跑、已完成批次、失败原因等。"""
    if not output_path.strip():
        raise HTTPException(422, "缺少 output_path")
    return _summarize(output_path.strip())


@router.post("/api/output/clear")
async def output_clear(payload: dict = Body(...)):
    """清空输出目录里的 TextForge 产物（白名单逐个删，不动其它文件）。"""
    output_path = (payload.get("output_path") or "").strip()
    if not output_path:
        raise HTTPException(422, "缺少 output_path")
    if state.pipeline_running:
        raise HTTPException(409, "pipeline_already_running：请先停止当前任务再清空")
    p = Path(output_path)
    if not p.exists():
        return {"status": "nothing", "removed": []}
    # 安全兜底：拒绝文件系统根 / 主目录 / 桌面这类「用户根本不会拿它当输出目录」的位置。
    # 即便调用方传错路径，也不允许在这种目录下删任何东西。
    try:
        resolved = p.resolve()
    except Exception:
        raise HTTPException(422, "输出目录无法解析")
    if resolved.parent == resolved:   # C:\ 或 /
        raise HTTPException(400, "拒绝清空文件系统根目录")
    try:
        home = Path.home().resolve()
        if resolved == home:
            raise HTTPException(400, "拒绝清空用户主目录")
        desktop = home / "Desktop"
        if desktop.exists() and resolved == desktop.resolve():
            raise HTTPException(400, "拒绝清空桌面目录")
    except HTTPException:
        raise
    except Exception:
        pass

    removed, failed = [], []
    for name in ARTIFACTS:
        target = p / name
        if not target.exists():
            continue
        try:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
            removed.append(name)
        except Exception as e:
            logger.warning(f"[output] 清理 {name} 失败: {e}")
            failed.append(name)
    logger.info(f"[output] 已清理输出目录 {output_path}：removed={removed} failed={failed}")
    return {"status": "cleared", "removed": removed, "failed": failed}


# ---------------- 成品版本管理（重跑前的整版快照 / 回滚） ----------------

@router.get("/api/versions")
async def versions_list(output_path: str = Query(...)):
    """列出成品的所有历史版本（重跑前的整版快照）。"""
    if not output_path.strip():
        raise HTTPException(422, "缺少 output_path")
    from core.versions import list_versions
    return {"versions": list_versions(Path(output_path.strip()))}


@router.post("/api/versions/restore")
async def versions_restore(payload: dict = Body(...)):
    """把某个历史版本还原回 03_final/（还原前先给当前版本做一次快照，可再回退）。"""
    output_path = (payload.get("output_path") or "").strip()
    version_id = (payload.get("version_id") or "").strip()
    if not output_path:
        raise HTTPException(422, "缺少 output_path")
    if state.pipeline_running:
        raise HTTPException(409, "pipeline_already_running：请先停止当前任务再回滚")
    from core.versions import restore
    try:
        return restore(Path(output_path), version_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        logger.exception("[versions] 恢复失败")
        raise HTTPException(500, f"恢复失败：{e}")
