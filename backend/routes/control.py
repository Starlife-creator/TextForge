from fastapi import APIRouter, HTTPException, Body
from routes import state
from pathlib import Path
from utils.atomic import atomic_write_text, atomic_write_json
import json

router = APIRouter()

def _check_run_id(run_id: str | None):
    if run_id and run_id != state.current_run_id:
        raise HTTPException(409, "run_id_mismatch")

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
    return {"status": "stopping"}

@router.post("/api/split/confirm")
async def split_confirm(payload: dict = Body(...)):
    """P0.3：拆书预览确认。仅 phase0_confirm 阶段允许，确认后进入 phase1。"""
    _check_run_id(payload.get("run_id"))
    output_path = state.current_output_path
    if not output_path: raise HTTPException(409, "no_active_run")
    progress_path = Path(output_path) / "progress.json"
    async with state.progress_lock:
        if not progress_path.exists():
            raise HTTPException(409, "progress.json 不存在，可能输出目录已被清理")
        try:
            progress = json.loads(progress_path.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, OSError) as e:
            raise HTTPException(409, f"progress.json 无法读取: {e}")
        if progress.get('current_phase') != 'phase0_confirm':
            raise HTTPException(409, f"当前阶段 {progress.get('current_phase')} 不允许确认拆分")
        progress['current_phase'] = 'phase1'
        atomic_write_json(progress_path, progress)
    state.split_confirm.set()
    return {"status": "confirmed"}

@router.post("/api/fix/confirm")
async def fix_confirm(payload: dict = Body(...)):
    """fix_gaps 断层勾选确认：仅 phase1_fixreview 阶段允许；
    持久化勾选断层 fix_list 到 progress.json，确认后进入 phase2。"""
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
            progress = json.loads(progress_path.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, OSError) as e:
            raise HTTPException(409, f"progress.json 无法读取: {e}")
        if progress.get('current_phase') != 'phase1_fixreview':
            raise HTTPException(409, f"当前阶段 {progress.get('current_phase')} 不允许确认断层")
        progress['fix_list'] = fix_list
        progress['current_phase'] = 'phase2'
        atomic_write_json(progress_path, progress)
    state.fix_review_confirm.set()
    return {"status": "confirmed", "fix_list": fix_list}

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
            progress = json.loads(progress_path.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, OSError) as e:
            raise HTTPException(409, f"progress.json 无法读取: {e}")
        if progress.get('current_phase') != 'phase3_accept':
            raise HTTPException(409, f"当前阶段 {progress.get('current_phase')} 不允许验收")
        progress.setdefault('accepted_batches', [])
        pend = progress.get('accept_pending_batch')
        if pend is not None and pend not in progress['accepted_batches']:
            progress['accepted_batches'].append(pend)
        progress['current_phase'] = 'phase2'
        atomic_write_json(progress_path, progress)
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
            progress = json.loads(progress_path.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, OSError) as e:
            raise HTTPException(409, f"progress.json 无法读取: {e}")
        # 只在 phase2_waiting 阶段允许确认
        if progress.get('current_phase') != 'phase2_waiting':
            raise HTTPException(409, f"当前阶段 {progress.get('current_phase')} 不允许确认蓝图")
        context_window = progress["api_config"]["context_window"]
        chars = context_window * 1.5
        max_blueprint = int(chars * 0.20)
        if len(blueprint) > max_blueprint:
            raise HTTPException(422, f"blueprint 超长: {len(blueprint)} > {max_blueprint}")
        if len(blueprint) < 100:
            raise HTTPException(422, "blueprint 过短")
        story_bible = Path(output_path) / "Story_Bible.md"
        atomic_write_text(story_bible, blueprint)
        progress["blueprint_confirmed"] = True
        progress["blueprint_user_edited"] = user_edited
        atomic_write_json(progress_path, progress)
    
    state.blueprint_confirmed.set()
    return {"status": "confirmed"}
