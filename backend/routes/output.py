"""输出目录管理：查看进度状态 / 一键清空（供 GUI 使用）。

背景：用户在中途断裂后需要重跑，但之前只能手动去文件夹里删。这里给出两个能力：
  - inspect：这个目录里有没有 TextForge 的进度？跑到哪了？失败原因是什么？
  - clear：只删 **TextForge 自己的产物**，绝不碰用户放在同一目录里的其它文件。

安全底线：clear 采用**白名单**逐个删除，绝不对输出目录整体 rmtree ——
用户完全可能把输出目录设成桌面/文档/某个已有项目目录，一把梭会毁掉无关数据。
"""

import asyncio
import json
import logging
import shutil
import os
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException, Query

from routes import state
from utils.errors import raise_internal_error

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
            from utils.progress_io import is_completed, read_progress
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
            "finished": is_completed(prog),
            # 可续跑：有阶段标记且真正完成前（与 _load_or_init_progress 的判据一致）。
            # 不能用 phase != 'phase4'：phase4 在 finalize 之前写入，渲染失败时
            # 仍应允许续跑，而不是让用户从头再跑一遍。
            "resumable": bool(phase) and not is_completed(prog),
            "error_logs": (prog.get("error_logs") or [])[-5:],
        })
    # 质量报告只取头部信息：inspect 会被界面频繁调用，没必要每次把整份
    # 逐章明细都读进来；完整报告由 /api/output/quality 按需拉取。
    quality_file = p / "_diagnostics" / "quality_report.json"
    if quality_file.is_file():
        try:
            q = json.loads(quality_file.read_text(encoding="utf-8"))
            info["quality"] = {
                "status": q.get("status"),
                "generated_at": q.get("generated_at"),
                "headline": q.get("headline"),
            }
        except (OSError, json.JSONDecodeError) as e:
            info["quality_error"] = str(e)[:200]
    return info


@router.get("/api/output/inspect")
async def output_inspect(output_path: str = Query(...)):
    """查看输出目录里的进度状态：是否可续跑、已完成批次、失败原因等。"""
    if not output_path.strip():
        raise HTTPException(422, "缺少 output_path")
    # #4：progress.json 与质量报告的读取/解析移出事件循环。
    return await asyncio.to_thread(_summarize, output_path.strip())


@router.get("/api/output/quality")
async def output_quality(output_path: str = Query(...)):
    """读取最近一次成品的质量报告。

    没有报告是正常状态（旧版本产出的成品、或报告生成失败），返回 404 让界面
    明确显示「尚无报告」，而不是拿一个空对象伪装成合格。
    """
    if not output_path.strip():
        raise HTTPException(422, "缺少 output_path")
    from core.quality import read_quality_report
    report = await asyncio.to_thread(read_quality_report, output_path.strip())
    if report is None:
        raise HTTPException(404, await asyncio.to_thread(_missing_report_detail, output_path.strip()))
    return report


def _missing_report_detail(output_path: str) -> str:
    """404 时把 finalize 记下的失败原因带出来，而不是只说「尚无报告」。

    没有报告有两种情况：跑的是旧版本成品，或者生成时抛了异常。后者的原因
    现在写在 progress.json 里（`quality_report.status == "failed"`），这里读出来
    拼进 404 详情 —— 否则用户与 CI 都只能看到一个没有任何线索的 404。
    """
    base = "尚无质量报告（完成一次重构后生成）"
    try:
        prog = json.loads(
            (Path(output_path) / "progress.json").read_text(encoding="utf-8-sig")
        )
    except (OSError, json.JSONDecodeError):
        return base
    qr = prog.get("quality_report") if isinstance(prog, dict) else None
    if isinstance(qr, dict) and qr.get("status") == "failed" and qr.get("error"):
        return f"{base}；上一次生成失败：{qr['error']}"
    return base


def _special_dirs(home: Path) -> set:
    """返回需要保护、绝不允许清空的「特殊目录」。

    之前这里只硬编码了英文 `~/Desktop`。但中文 Windows 在关闭了
    「显示此语言的其他文件夹」之后，桌面实际叫 `桌面` —— 此时
    `home / "Desktop"` 根本不存在，`desktop.exists()` 恒为假，
    那道防护会**静默失效**，清空桌面照样放行。

    正确做法是读注册表里系统自己记的真实路径（只读，不改系统），
    再把常见本地化名兜底加上，任一命中即拒绝。
    """
    names = ["Desktop", "桌面"]
    import sys
    if sys.platform == "win32":
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
            )
            try:
                val, _ = winreg.QueryValueEx(key, "Desktop")
                if val:
                    names.insert(0, val)
            finally:
                winreg.CloseKey(key)
        except Exception:
            # 非 Windows 或注册表不可读：下面按本地化名兜底即可
            pass

    out = set()
    for name in names:
        try:
            p = Path(os.path.expandvars(name))
            if p.is_dir():
                out.add(p.resolve())
        except OSError:
            continue
    return out


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
    home = Path.home().resolve()
    if resolved == home:
        raise HTTPException(400, "拒绝清空用户主目录")
    for special in _special_dirs(home):
        if resolved == special:
            raise HTTPException(400, f"拒绝清空特殊目录：{special.name}")

    # 正向证据：只有「明确像 TextForge 输出目录」才允许删。此前仅挡了根/主目录/桌面，
    # 指向 Documents 之类的任意目录时，仍会把碰巧同名的 03_final/_versions/_backup
    # 一并删掉。progress.json 与 02_workspace 是 TextForge 独有、几乎不会撞名的产物。
    if not ((p / "progress.json").exists() or (p / "02_workspace").exists()):
        raise HTTPException(
            400, "该目录不像 TextForge 输出目录（缺少 progress.json / 02_workspace），拒绝清空")

    removed, failed = await asyncio.to_thread(_clear_artifacts, p)
    logger.info(f"[output] 已清理输出目录 {output_path}：removed={removed} failed={failed}")
    return {"status": "cleared", "removed": removed, "failed": failed}


def _clear_artifacts(p: Path) -> tuple[list[str], list[str]]:
    """白名单逐个删除产物；目录 rmtree / 文件 unlink 属阻塞磁盘 I/O，由线程池执行。"""
    removed: list[str] = []
    failed: list[str] = []
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
    return removed, failed


# ---------------- 成品版本管理（重跑前的整版快照 / 回滚） ----------------

@router.get("/api/versions")
async def versions_list(output_path: str = Query(...)):
    """列出成品的所有历史版本（重跑前的整版快照）。"""
    if not output_path.strip():
        raise HTTPException(422, "缺少 output_path")
    from core.versions import list_versions
    # #4：目录扫描移出事件循环。
    return {"versions": await asyncio.to_thread(list_versions, Path(output_path.strip()))}


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
        # #4：快照/回滚涉及批量文件复制，阻塞 I/O 移出事件循环。
        return await asyncio.to_thread(restore, Path(output_path), version_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        # #6：500 不回传内部细节，异常已由 logger.exception 记录。
        raise raise_internal_error(e, "版本恢复失败，详情请查看后端日志", "versions_restore")
