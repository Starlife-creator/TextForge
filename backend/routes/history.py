"""任务历史：跨会话记录「跑过哪些书、跑到哪、为什么停的」。

动机：progress.json 住在各个输出目录里，而用户往往只记得「我跑过一本书」，
不记得当时选了哪个输出目录 —— 于是看到「没保存」就以为白跑了。这里用一份
集中注册表把「输出路径 → 上次状态」串起来，并让界面能一键续跑/打开/清空。

存储：数据目录/runs.json（小型元数据，不含进度正文）。便携包下数据目录即
exe 旁的 data/ 子目录，随包移动；安装版回退 %APPDATA%/TextForge/runs.json。
**刻意不扫盘、不读 progress.json 来列历史**：progress.json 在缝合阶段可能达数十 MB
（锚点内嵌全文），每次列历史都去读一遍会直接卡住界面。元数据由流水线在关键节点
（心跳/起止/错误）主动 upsert 进来。
"""

import asyncio
import json
import logging
import os
import threading
import time
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException, Query

from routes import state
from utils.appdir import ensure_data_dir
from utils.atomic import atomic_write_json
from utils.progress_io import is_completed
from utils.redact import redact

logger = logging.getLogger("textforge")
router = APIRouter()

MAX_ENTRIES = 50
_TRIM_CHARS = 200
# 进程内串行化 runs.json 的读-改-写：record_progress（流水线线程）与
# history_forget/clear（用户点按钮）可能并发，无锁时后写者整份覆盖前写者。
_REGISTRY_LOCK = threading.Lock()


def _registry_path() -> Path:
    return ensure_data_dir() / "runs.json"


def _load() -> dict:
    p = _registry_path()
    if not p.exists():
        return {"entries": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("entries"), dict):
            # 历史一次性清洗：修复前落盘的 config_drift 可能含明文密钥，读侧兜底脱敏。
            for e in data["entries"].values():
                if isinstance(e, dict) and "config_drift" in e:
                    e["config_drift"] = redact(e["config_drift"])
            return data
    except Exception as e:
        # 与 progress.json 同一口径：读失败不静默清空，先把坏文件留 .bak，
        # 避免一次半截写入/杀软扫盘就丢掉全部历史且无可恢复副本。
        try:
            import shutil
            shutil.copy2(p, p.with_suffix(".json.bak"))
        except OSError:
            pass
        logger.warning(f"[history] runs.json 无法解析，已备份为 .bak 后重置：{e}")
    return {"entries": {}}


def _norm(path: str) -> str:
    try:
        return os.path.normcase(os.path.abspath(path))
    except Exception:
        return path or ""


def upsert(output_path: str, meta: dict) -> None:
    """把某个输出目录的状态写入注册表（幂等，按路径去重，最新在前）。"""
    if not output_path:
        return
    key = _norm(output_path)
    with _REGISTRY_LOCK:
        reg = _load()
        entries = reg["entries"]
        prev = entries.get(key) or {}
        e = dict(prev)
        e["output_path"] = output_path
        for k in ("novel_name", "input_path", "input_mode", "current_phase",
                  "total_chapters", "batches_done", "batches_total", "finished",
                  "finished_at", "chapter_detection", "regen_total", "config_drift"):
            if k in meta and meta[k] is not None:
                # P0-1：历史注册表不得留存明文密钥；config_drift 落 runs.json 前兜底脱敏。
                e[k] = redact(meta[k]) if k == "config_drift" else meta[k]
        errs = meta.get("last_error")
        if errs:
            e["last_error"] = str(errs)[:_TRIM_CHARS]
        elif meta.get("clear_error"):
            e.pop("last_error", None)
        e["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        e.setdefault("created_at", e["updated_at"])
        entries[key] = e
        # 只保留最近 MAX_ENTRIES 条
        ordered = sorted(entries.items(), key=lambda kv: kv[1].get("updated_at", ""), reverse=True)
        reg["entries"] = dict(ordered[:MAX_ENTRIES])
        try:
            atomic_write_json(_registry_path(), reg)
        except Exception as ex:
            logger.warning(f"[history] 写入 runs.json 失败：{ex}")


def _meta_from_progress(progress: dict) -> dict:
    """从 progress.json 抽取注册表需要的元数据。"""
    batches = progress.get("batches") or []
    errs = progress.get("error_logs") or []
    regen_count = progress.get("regen_count") or {}
    return {
        "novel_name": progress.get("novel_name"),
        "input_path": progress.get("input_path"),
        "input_mode": progress.get("input_mode"),
        "current_phase": progress.get("current_phase"),
        "total_chapters": len(progress.get("chapters") or []),
        "batches_done": sum(1 for b in batches if b.get("status") == "phase3_done"),
        "batches_total": len(batches),
        "finished": is_completed(progress),
        "finished_at": progress.get("finished_at"),
        "chapter_detection": progress.get("chapter_detection"),
        # #3：单章重生成总次数随历史展示（与质量报告摘要同源）。
        "regen_total": sum(int(v or 0) for v in regen_count.values()),
        # P0-1：抽取元数据时即脱敏，杜绝明文密钥写入 runs.json。
        "config_drift": redact(progress.get("config_drift") or []),
        "last_error": (errs[-1].get("message") if errs else None),
    }


def record_progress(progress_path: Path, progress: dict) -> None:
    """由流水线调用：把当前 progress 的元数据同步进历史注册表。"""
    try:
        output_path = progress.get("output_path") or str(Path(progress_path).parent)
        upsert(output_path, _meta_from_progress(progress))
    except Exception as e:
        logger.warning(f"[history] record_progress 失败：{e}")


def _write_registry(reg: dict) -> None:
    """同步写注册表（供 asyncio.to_thread 调用）。"""
    atomic_write_json(_registry_path(), reg)


@router.get("/api/history")
async def history_list(limit: int = Query(30)):
    """任务历史列表（最近优先）。额外标注当前正在跑的那条。"""
    # #4：runs.json 读取移出事件循环。
    reg = await asyncio.to_thread(_load)
    items = list(reg["entries"].values())
    items.sort(key=lambda e: e.get("updated_at", ""), reverse=True)
    cur = _norm(state.current_output_path or "")
    out = []
    for e in items[:max(1, min(limit, MAX_ENTRIES))]:
        e = dict(e)
        e["is_current"] = bool(cur) and _norm(e.get("output_path", "")) == cur
        e["running"] = e["is_current"] and state.pipeline_running
        e.pop("chapter_detection", None)   # 列表不需要，减小体积
        out.append(e)
    return {"items": out}


def _forget_locked(key: str) -> None:
    with _REGISTRY_LOCK:
        reg = _load()
        reg["entries"].pop(key, None)
        atomic_write_json(_registry_path(), reg)


def _clear_locked() -> None:
    with _REGISTRY_LOCK:
        atomic_write_json(_registry_path(), {"entries": {}})


@router.post("/api/history/forget")
async def history_forget(payload: dict = Body(...)):
    """把某条从历史里移除（只删注册表记录，不动磁盘文件）。"""
    output_path = (payload.get("output_path") or "").strip()
    if not output_path:
        raise HTTPException(422, "缺少 output_path")
    try:
        await asyncio.to_thread(_forget_locked, _norm(output_path))
    except Exception as e:
        raise HTTPException(500, f"写入失败：{e}")
    return {"status": "forgotten"}


@router.post("/api/history/clear")
async def history_clear():
    """清空整个历史注册表（只清注册表，不动任何磁盘文件）。"""
    try:
        await asyncio.to_thread(_clear_locked)
    except Exception as e:
        raise HTTPException(500, f"清空失败：{e}")
    return {"status": "cleared"}


def export_history() -> dict:
    """导出全量历史注册表（纯同步，供线程池调用）。条目落库时已脱敏。"""
    reg = _load()
    entries = sorted(
        reg["entries"].values(),
        key=lambda e: e.get("updated_at", ""),
        reverse=True,
    )
    return {
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "count": len(entries),
        "entries": entries,
    }


@router.get("/api/history/export")
async def history_export():
    """#17：把历史注册表导出为 JSON 附件下载（备份/迁移用，不含 progress 正文）。"""
    from fastapi.responses import JSONResponse
    data = await asyncio.to_thread(export_history)
    filename = f"textforge-history-{time.strftime('%Y%m%d-%H%M%S')}.json"
    return JSONResponse(
        content=data,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )