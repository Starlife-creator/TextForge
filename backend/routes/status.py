from fastapi import APIRouter
from pathlib import Path
from collections import OrderedDict
import json, logging, os, re
from routes import state
from utils.progress_io import is_completed
from utils.redact import redact

router = APIRouter()
logger = logging.getLogger("textforge")

# progress.json 在缝合阶段可达数十 MB，而 /api/status 被前端每 5s 轮询一次。
# 缓存「按 (mtime_ns, size) 解析后的 progress」：文件没变直接复用，免去每轮
# 全量 json.loads；文件一变（atomic_write_json 会换 mtime）立即失效。
# 用 LRU 封顶（每个值是一份完整 dict，可达数十 MB），避免浏览历史、轮询过
# 许多输出目录时无限累积内存。纯同步、只在事件循环里访问，故无需加锁。
_MAX_PROGRESS_CACHE = 8
_PROGRESS_CACHE: "OrderedDict[str, tuple[tuple[int, int], dict]]" = OrderedDict()

# 摘要预览缓存：按 (mtime_ns, size) 缓存前 200 字预览
_MAX_SUMMARY_CACHE = 32
_SUMMARY_CACHE: "OrderedDict[str, tuple[tuple[int, int], str]]" = OrderedDict()

# #9：对照稿文件名严格白名单 chapter_<非分隔符>.comparison.md，禁止 ../ 与 \\。
_COMPARISON_NAME_RE = re.compile(r"^chapter_[^/\\]+\.comparison\.md$")


def _read_progress_cached(path: Path) -> dict:
    st = path.stat()
    key = str(path)
    stamp = (st.st_mtime_ns, st.st_size)
    cached = _PROGRESS_CACHE.get(key)
    if cached is not None and cached[0] == stamp:
        _PROGRESS_CACHE.move_to_end(key)
        return cached[1]
    data = json.loads(path.read_text(encoding='utf-8-sig'))  # utf-8-sig: 容忍 BOM
    _PROGRESS_CACHE[key] = (stamp, data)
    _PROGRESS_CACHE.move_to_end(key)
    while len(_PROGRESS_CACHE) > _MAX_PROGRESS_CACHE:
        _PROGRESS_CACHE.popitem(last=False)
    return data


def _get_summary_preview(path: Path) -> str:
    """返回文件前 200 字预览，带 LRU 缓存（按 mtime_ns+size 失效）。"""
    st = path.stat()
    key = str(path)
    stamp = (st.st_mtime_ns, st.st_size)
    cached = _SUMMARY_CACHE.get(key)
    if cached is not None and cached[0] == stamp:
        _SUMMARY_CACHE.move_to_end(key)
        return cached[1]
    txt = path.read_text(encoding='utf-8', errors='replace')
    preview = txt[:200]
    _SUMMARY_CACHE[key] = (stamp, preview)
    _SUMMARY_CACHE.move_to_end(key)
    while len(_SUMMARY_CACHE) > _MAX_SUMMARY_CACHE:
        _SUMMARY_CACHE.popitem(last=False)
    return preview


@router.get("/api/status")
async def status(output_path: str | None = None):
    """查询当前/指定输出目录的流水线状态。
    用途：前端刷新或 SSE 重连后恢复 UI（阶段、暂停态、待确认蓝图、用量、错误）。"""
    target = output_path or state.current_output_path
    paused = not state.pause_event.is_set()
    # #2：闸门等待态只活在内存（与心跳同口径），无 progress.json 时也必须能上报。
    gate_mem = {"active_gate": state.active_gate(),
                "waiting_seconds": state.gate_wait_seconds()}
    if not target:
        return {"pipeline_running": state.pipeline_running, "paused": paused,
                "run_id": state.current_run_id, "has_progress": False, **gate_mem}

    progress_path = Path(target) / "progress.json"
    if not progress_path.exists():
        return {"pipeline_running": state.pipeline_running, "paused": paused,
                "run_id": state.current_run_id, "has_progress": False, **gate_mem}
    try:
        progress = _read_progress_cached(progress_path)
    except (json.JSONDecodeError, OSError) as e:
        return {"pipeline_running": state.pipeline_running, "paused": paused,
                "has_progress": True, "progress_error": f"progress.json 无法读取: {e}",
                **gate_mem}

    batches = progress.get("batches", [])
    # #2 章级进度：按逻辑章（虚拟章组折叠到父章）统计总数与已重构数
    logic_ids = _logic_chapter_ids(progress.get("chapters", []))
    # 一次性扫描 reconstructed 目录，避免逐章 Path.exists() 产生 O(n) 个 stat 调用
    recon_dir = Path(target) / "02_workspace/reconstructed"
    recon_files = set()
    if recon_dir.is_dir():
        for entry in os.scandir(recon_dir):
            if entry.name.startswith("chapter_") and entry.name.endswith(".txt"):
                recon_files.add(entry.name[:-4])  # 去掉 .txt
    reconstructed = sum(1 for oid in logic_ids if f"chapter_{oid}" in recon_files)
    data = {
        "pipeline_running": state.pipeline_running,
        "paused": paused,
        "run_id": state.current_run_id,
        "has_progress": True,
        "current_phase": progress.get("current_phase"),
        "novel_name": progress.get("novel_name"),
        "total_chapters": len(progress.get("chapters", [])),
        # 拆章方式/置信度：界面据此提示「是否按预期拆分」，低置信时引导人工确认
        "chapter_detection": progress.get("chapter_detection"),
        # 续跑时配置与上次不一致（模型/API/上下文/输入路径），前端提示以免风格割裂
        # P0-1：回传前递归脱敏兜底，防止历史 progress 残留明文密钥进入响应。
        "config_drift": redact(progress.get("config_drift") or []),
        "total_logic_chapters": len(logic_ids),
        "reconstructed_chapters": reconstructed,
        "resumed": progress.get("resumed", False),
        # 前端据此在 done 事件丢失时靠轮询收尾；finished_at 不能作为完成判据
        "completed": is_completed(progress),
        "blueprint_confirmed": progress.get("blueprint_confirmed", False),
        "usage": progress.get("usage", {}),
        "last_heartbeat": state.last_heartbeat or progress.get("last_heartbeat"),
        "batches": {
            "total": len(batches),
            "phase1_done": sum(1 for b in batches if b["status"] in ("phase1_done", "phase3_done")),
            "phase3_done": sum(1 for b in batches if b["status"] == "phase3_done"),
        },
        "error_logs": progress.get("error_logs", [])[-5:],
        # #2：当前等待闸门、已等待时长、本轮是否发生过闸门超时。
        "active_gate": state.active_gate(),
        "waiting_seconds": state.gate_wait_seconds(),
        "gate_timed_out": bool(progress.get("gate_timed_out")),
        "gate_timeout_gate": progress.get("gate_timeout_gate"),
        "gate_wait_seconds": progress.get("gate_wait_seconds"),
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
            prev.append({"name": f.name, "preview": _get_summary_preview(f)})
        if prev:
            data["summaries"] = prev

    # B3：刷新后恢复等待态
    data["resume"] = _build_resume(progress, target, recon_files)
    # A3：章节概览（逻辑章折叠到父章，去重）
    data["chapters_summary"] = _chapter_summary(progress, recon_files)
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


def _build_resume(progress, target, recon_files: set[str] | None = None):
    """按当前等待阶段返回可重开弹窗所需的前端数据，否则 None。

    必须覆盖**所有**会无限等待的闸门阶段：wait_split_confirm 是无界 while，
    漏掉任何一个，刷新页面/SSE 重连后就再也弹不出确认框，任务 0% 死转且无法恢复。
    """
    phase = progress.get("current_phase")
    if phase == "phase0_confirm":
        # 拆书预览：与 split_ready 推送的字段保持一致
        return {"key": "split",
                "chapters": [{k: c.get(k) for k in ("id", "filename", "title", "is_empty", "is_virtual")}
                             for c in progress.get("chapters", [])]}
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
                        # 使用 recon_files 缓存避免逐文件 stat
                        preview = ""
                        if recon_files is not None and f"chapter_{cid}" in recon_files:
                            pf = Path(target) / "02_workspace/reconstructed" / f"chapter_{cid}.txt"
                            preview = pf.read_text(encoding="utf-8", errors="replace")[:80]
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


def _chapter_summary(progress, recon_files: set[str] | None = None) -> list:
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
        recon = recon_files is not None and f"chapter_{out_id}" in recon_files
        rows.append({
            "id": out_id,
            "label": ch.get("title") or ch.get("filename") or str(out_id),
            "status": "reconstructed" if recon else "pending",
        })
    return rows


@router.get("/api/final_files")
async def final_files(output_path: str):
    """C4：列出 03_final 成品目录内的交付文档（供前端一键打开成品文档）。

    只收 txt/md/docx：03_final 里可能出现诊断类产物，按扩展名过滤比按文件名可靠。
    """
    fin = Path(output_path) / "03_final"
    if not fin.is_dir():
        return {"files": []}
    files = []
    for f in sorted(fin.iterdir()):
        if f.is_file() and f.suffix.lower() in (".txt", ".md", ".docx"):
            files.append({"name": f.name, "path": str(f)})
    return {"files": files}


@router.get("/api/comparisons")
async def comparisons(output_path: str, name: str | None = None):
    """C1：列出 00_comparison 左右对照 md 文件及其原文（前端转义渲染）。

    支持按需加载：
    - 不带 name：返回文件列表（不含正文），供前端下拉选择
    - 带 name：返回单个文件的完整正文
    """
    comp_dir = Path(output_path) / "00_comparison"
    if not comp_dir.is_dir():
        return {"items": []} if name is None else {"items": []}

    if name is not None:
        # #9 路径校验加固：严格正则白名单 + resolve 后前缀断言，杜绝 ../ 穿越与
        # 符号链接逃逸到 00_comparison 目录之外（此前仅靠 startswith 与后缀弱校验）。
        if not _COMPARISON_NAME_RE.match(name):
            return {"items": []}
        try:
            f = (comp_dir / name).resolve()
            base = comp_dir.resolve()
            if not str(f).startswith(str(base) + os.sep):
                return {"items": []}
        except OSError:
            return {"items": []}
        if f.is_file() and f.suffix.lower() == ".md":
            return {"items": [{"name": f.name, "text": f.read_text(encoding='utf-8', errors='replace')}]}
        return {"items": []}

    # 列表模式：只返回文件名和大小，不读正文
    items = []
    for f in sorted(comp_dir.glob("chapter_*.comparison.md")):
        items.append({"name": f.name, "size": f.stat().st_size})
    return {"items": items}
