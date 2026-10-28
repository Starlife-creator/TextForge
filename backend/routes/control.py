"""人工闸门控制端点。

铁律：本模块**只写业务字段，不写 current_phase**。
阶段推进由流水线在读到 Event 后自行完成（state.absorb_gate_result 负责并回用户选择）。
端点若也去写 current_phase，会与等待循环的写盘互相覆盖，且历史上曾错误地回退到
'phase2'，导致断点恢复时蓝图被重新生成（见 routes/state.py:absorb_gate_result 注释）。
"""
import asyncio, os, subprocess, sys
from fastapi import APIRouter, HTTPException, Body
from core.budget import blueprint_accept_max_chars
from core.refactor import REGEN_MAX_PER_CHAPTER
from routes import state
from pathlib import Path
from utils.atomic import atomic_write_text
from utils.errors import raise_internal_error
from utils.progress_io import (
    load_progress_dict, save_progress_dict, ProgressUnreadable,
)
import json

router = APIRouter()

def _check_run_id(run_id: str | None):
    if run_id and run_id != state.current_run_id:
        raise HTTPException(409, "run_id_mismatch")


async def _save_progress_guarded(progress_path: Path, progress: dict) -> None:
    """控制端点写 progress.json 的统一出口：Windows 上目标文件被杀软/资源管理器
    占用时 os.replace 会抛 PermissionError [WinError 5]，退避重试仍失败就转成
    503 让前端提示「文件正被占用，请重试」，而不是裸 500 + traceback 堵死 e2e。
    """
    try:
        await asyncio.to_thread(save_progress_dict, progress_path, progress)
    except PermissionError as e:
        raise HTTPException(503, f"文件正被占用，请稍后重试：{progress_path.name}") from e

@router.post("/api/pause")
async def pause(run_id: str | None = Body(None, embed=True)):
    _check_run_id(run_id)
    state.pause_event.clear()
    return {"status": "paused"}

@router.post("/api/resume")
async def resume(run_id: str | None = Body(None, embed=True)):
    _check_run_id(run_id)
    state.pause_event.set()
    return {"status": "running"}

@router.post("/api/stop")
async def stop(run_id: str | None = Body(None, embed=True)):
    _check_run_id(run_id)
    state.stop_requested = True
    state.pause_event.set()  # 解除暂停
    state.blueprint_confirmed.set()  # 解除蓝图等待
    state.split_confirm.set()  # P0.3：解除拆书预览等待
    state.fix_review_confirm.set()  # 解除断层勾选等待
    state.accept_confirm.set()  # 解除逐章验收等待
    state.qc_confirm.set()  # 解除质检拦截等待
    return {"status": "stopping"}

@router.post("/api/split/confirm")
async def split_confirm(payload: dict = Body(...)):
    """P0.3：拆书预览确认。仅 phase0_confirm 阶段允许，确认后流水线自行推进到 phase1。"""
    _check_run_id(payload.get("run_id"))
    output_path = state.current_output_path
    if not output_path: raise HTTPException(409, "no_active_run")
    progress_path = Path(output_path) / "progress.json"
    async with state.progress_lock:
        if not progress_path.exists():
            raise HTTPException(409, "progress.json 不存在，可能输出目录已被清理")
        try:
            # #4：progress.json 读取与 JSON 解析移出事件循环（可达数十 MB）。
            progress = await asyncio.to_thread(load_progress_dict, progress_path)
        except (ProgressUnreadable, json.JSONDecodeError, OSError) as e:
            raise HTTPException(409, f"progress.json 无法读取: {e}")
        if progress.get('current_phase') != 'phase0_confirm':
            raise HTTPException(409, f"当前阶段 {progress.get('current_phase')} 不允许确认拆分")
    state.split_confirm.set()
    return {"status": "confirmed"}

@router.post("/api/split/redo")
async def split_redo(payload: dict = Body(...)):
    """P0.5 在拆书预览里按自定义章节标记重新拆分。

    必须与原文标题行**完全一致**（精确匹配）。用于规则与 AI 都搞不定的创新排版。
    这里只登记标记并唤醒闸门，真正的重拆在流水线线程内串行执行（避免并发改 progress）。
    """
    _check_run_id(payload.get("run_id"))
    markers = payload.get("markers", []) or []
    if not isinstance(markers, list) or not any(str(m).strip() for m in markers):
        raise HTTPException(422, "markers 必须是非空的字符串数组")
    req = state.current_req
    if getattr(req, "input_mode", "") != "single_file":
        raise HTTPException(422, "只有单文件模式支持按自定义标记重新拆分；多文件模式请直接调整文件")
    output_path = state.current_output_path
    if not output_path: raise HTTPException(409, "no_active_run")
    progress_path = Path(output_path) / "progress.json"
    async with state.progress_lock:
        if not progress_path.exists():
            raise HTTPException(409, "progress.json 不存在，可能输出目录已被清理")
        try:
            # #4：progress.json 读取与 JSON 解析移出事件循环（可达数十 MB）。
            progress = await asyncio.to_thread(load_progress_dict, progress_path)
        except (ProgressUnreadable, json.JSONDecodeError, OSError) as e:
            raise HTTPException(409, f"progress.json 无法读取: {e}")
        if progress.get('current_phase') != 'phase0_confirm':
            raise HTTPException(409, f"当前阶段 {progress.get('current_phase')} 不允许重新拆分")
    state.split_redo_markers = [str(m).strip() for m in markers if str(m).strip()]
    state.split_redo.set()
    return {"status": "redoing", "markers": len(state.split_redo_markers)}

@router.post("/api/fix/confirm")
async def fix_confirm(payload: dict = Body(...)):
    """fix_gaps 断层勾选确认：仅 phase1_fixreview 阶段允许；
    持久化勾选断层 fix_list 到 progress.json，确认后流水线自行推进到 phase2。"""
    _check_run_id(payload.get("run_id"))
    fix_list = payload.get("fix_list", []) or []
    if not isinstance(fix_list, list):
        raise HTTPException(422, "fix_list 必须为数组")
    output_path = state.current_output_path
    if not output_path: raise HTTPException(409, "no_active_run")
    progress_path = Path(output_path) / "progress.json"
    async with state.progress_lock:
        if not progress_path.exists():
            raise HTTPException(409, "progress.json 不存在，可能输出目录已被清理")
        try:
            # #4：progress.json 读取与 JSON 解析移出事件循环（可达数十 MB）。
            progress = await asyncio.to_thread(load_progress_dict, progress_path)
        except (ProgressUnreadable, json.JSONDecodeError, OSError) as e:
            raise HTTPException(409, f"progress.json 无法读取: {e}")
        if progress.get('current_phase') != 'phase1_fixreview':
            raise HTTPException(409, f"当前阶段 {progress.get('current_phase')} 不允许确认断层")
        progress['fix_list'] = fix_list
        await _save_progress_guarded(progress_path, progress)
    state.fix_review_confirm.set()
    return {"status": "confirmed", "fix_list": fix_list}

@router.post("/api/open_output")
async def open_output(payload: dict = Body(...)):
    """在系统文件管理器打开输出目录（本地调用，无需额外插件）。"""
    path = payload.get("output_path") or ""
    p = Path(path)
    if not p.is_dir():
        raise HTTPException(404, f"输出目录不存在: {path}")
    try:
        if sys.platform == "win32":
            os.startfile(str(p))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])
    except Exception as e:
        # #6 错误信息泄露：500 不对客户端回传异常细节，仅记录到本地日志。
        raise raise_internal_error(e, "打开目录失败，请检查系统文件管理器", "open_output")
    return {"status": "opened"}

@router.post("/api/export/force")
async def export_force(payload: dict = Body(...)):
    """质检拦截决策：用户选择「忽略质检、强制导出」。仅 phase3_qc 阶段允许。
    记录 _qc_force 后放行，管道继续进入 phase4。"""
    _check_run_id(payload.get("run_id"))
    output_path = state.current_output_path
    if not output_path: raise HTTPException(409, "no_active_run")
    progress_path = Path(output_path) / "progress.json"
    async with state.progress_lock:
        if not progress_path.exists():
            raise HTTPException(409, "progress.json 不存在，可能输出目录已被清理")
        try:
            # #4：progress.json 读取与 JSON 解析移出事件循环（可达数十 MB）。
            progress = await asyncio.to_thread(load_progress_dict, progress_path)
        except (ProgressUnreadable, json.JSONDecodeError, OSError) as e:
            raise HTTPException(409, f"progress.json 无法读取: {e}")
        if progress.get('current_phase') != 'phase3_qc':
            raise HTTPException(409, f"当前阶段 {progress.get('current_phase')} 不允许强制导出")
        progress['_qc_force'] = True
        await _save_progress_guarded(progress_path, progress)
    state.qc_confirm.set()
    return {"status": "exporting"}

@router.post("/api/regen_chapter")
async def regen_chapter(payload: dict = Body(...)):
    """A1：逐章验收中驳回某章并重生成。仅 phase3_accept 阶段允许；复用运行中 req 鉴权（内存）。"""
    _check_run_id(payload.get("run_id"))
    chapter_id = payload.get("chapter_id")
    if not chapter_id:
        raise HTTPException(422, "缺少 chapter_id")
    req = state.current_req
    output_path = state.current_output_path
    if not req or not output_path:
        raise HTTPException(409, "no_active_run")
    progress_path = Path(output_path) / "progress.json"
    async with state.progress_lock:
        if not progress_path.exists():
            raise HTTPException(409, "progress.json 不存在，可能输出目录已被清理")
        try:
            # #4：progress.json 读取与 JSON 解析移出事件循环（可达数十 MB）。
            progress = await asyncio.to_thread(load_progress_dict, progress_path)
        except (ProgressUnreadable, json.JSONDecodeError, OSError) as e:
            raise HTTPException(409, f"progress.json 无法读取: {e}")
        if progress.get('current_phase') != 'phase3_accept':
            raise HTTPException(409, f"当前阶段 {progress.get('current_phase')} 不允许重生成")
        pend = progress.get('accept_pending_batch')
        allowed = any(str(cid) == str(chapter_id)
                      for b in progress.get('batches', []) if b.get('batch_id') == pend
                      for cid in b.get('chapter_ids', []))
        if not allowed:
            raise HTTPException(422, f"章节 {chapter_id} 不在当前待验收批次内")
        # #3 单章重生成次数熔断：达到上限拒绝再次驳回，避免重复计费。
        regen_count = progress.get('regen_count') or {}
        used = int(regen_count.get(str(chapter_id), 0) or 0)
        if used >= REGEN_MAX_PER_CHAPTER:
            raise HTTPException(
                409,
                f"章节 {chapter_id} 已达重写上限（{REGEN_MAX_PER_CHAPTER} 次），"
                "请人工定稿或停止任务")
    from core.refactor import regen_single_chapter
    try:
        new_text = await regen_single_chapter(req, output_path, str(chapter_id))
    except ValueError as e:
        raise HTTPException(409, str(e))
    except Exception as e:
        # #6：500 不回传内部异常细节，详情进日志。
        raise raise_internal_error(e, "单章重生成失败，详情请查看后端日志", "regen_chapter")
    # 成功后登记单章重写次数（与主路径整轮护栏口径一致）。
    async with state.progress_lock:
        try:
            updated = await asyncio.to_thread(load_progress_dict, progress_path)
        except (ProgressUnreadable, json.JSONDecodeError, OSError):
            updated = progress
        updated.setdefault('regen_count', {})
        cid = str(chapter_id)
        updated['regen_count'][cid] = int(updated['regen_count'].get(cid, 0)) + 1
        await _save_progress_guarded(progress_path, updated)
    return {"status": "regenerated", "chapter_id": chapter_id, "preview": new_text[:80]}

@router.post("/api/accept")
async def accept(payload: dict = Body(...)):
    """逐章验收确认：仅 phase3_accept 阶段允许。记录该批次已验收章节后放行进下一批。"""
    _check_run_id(payload.get("run_id"))
    output_path = state.current_output_path
    if not output_path: raise HTTPException(409, "no_active_run")
    progress_path = Path(output_path) / "progress.json"
    async with state.progress_lock:
        if not progress_path.exists():
            raise HTTPException(409, "progress.json 不存在，可能输出目录已被清理")
        try:
            # #4：progress.json 读取与 JSON 解析移出事件循环（可达数十 MB）。
            progress = await asyncio.to_thread(load_progress_dict, progress_path)
        except (ProgressUnreadable, json.JSONDecodeError, OSError) as e:
            raise HTTPException(409, f"progress.json 无法读取: {e}")
        if progress.get('current_phase') != 'phase3_accept':
            raise HTTPException(409, f"当前阶段 {progress.get('current_phase')} 不允许验收")
        progress.setdefault('accepted_batches', [])
        pend = progress.get('accept_pending_batch')
        if pend is not None and pend not in progress['accepted_batches']:
            progress['accepted_batches'].append(pend)
        await _save_progress_guarded(progress_path, progress)
    state.accept_confirm.set()
    return {"status": "accepted"}

@router.post("/api/blueprint/confirm")
async def blueprint_confirm(payload: dict = Body(...)):
    _check_run_id(payload.get("run_id"))
    blueprint = payload.get("blueprint", "")
    user_edited = payload.get("user_edited", False)
    output_path = state.current_output_path
    if not output_path: raise HTTPException(409, "no_active_run")
    
    progress_path = Path(output_path) / "progress.json"
    async with state.progress_lock:
        # v8.8：progress.json 缺失/损坏时返回明确错误，避免未捕获异常导致 500
        if not progress_path.exists():
            raise HTTPException(409, "progress.json 不存在，可能输出目录已被清理")
        try:
            # #4：progress.json 读取与 JSON 解析移出事件循环（可达数十 MB）。
            progress = await asyncio.to_thread(load_progress_dict, progress_path)
        except (ProgressUnreadable, json.JSONDecodeError, OSError) as e:
            raise HTTPException(409, f"progress.json 无法读取: {e}")
        # 只在 phase2_waiting 阶段允许确认
        if progress.get('current_phase') != 'phase2_waiting':
            raise HTTPException(409, f"当前阶段 {progress.get('current_phase')} 不允许确认蓝图")
        context_window = progress["api_config"]["context_window"]
        max_blueprint = blueprint_accept_max_chars(context_window)
        if len(blueprint) > max_blueprint:
            raise HTTPException(422, f"blueprint 超长: {len(blueprint)} > {max_blueprint}")
        if len(blueprint) < 100:
            raise HTTPException(422, "blueprint 过短")
        story_bible = Path(output_path) / "Story_Bible.md"
        # #4：同步原子写（含 fsync）移出事件循环线程。
        try:
            await asyncio.to_thread(atomic_write_text, story_bible, blueprint)
        except PermissionError as e:
            raise HTTPException(503, "文件正被占用，请稍后重试：Story_Bible.md") from e
        progress["blueprint_confirmed"] = True
        progress["blueprint_user_edited"] = user_edited
        await _save_progress_guarded(progress_path, progress)
    
    state.blueprint_confirmed.set()
    return {"status": "confirmed"}
