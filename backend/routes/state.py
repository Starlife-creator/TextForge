import asyncio, logging
from typing import Optional

pipeline_lock = asyncio.Lock()
pipeline_running = False
pipeline_task: Optional[asyncio.Task] = None
pause_event = asyncio.Event()              # 用户主动暂停（仅内存，不落盘）
pause_event.set()
blueprint_confirmed = asyncio.Event()      # 蓝图确认
split_confirm = asyncio.Event()            # P0.3 拆书预览确认（仅内存，不落盘）
split_confirm.set()
fix_review_confirm = asyncio.Event()       # fix_gaps 断层勾选确认（仅内存，不落盘）
fix_review_confirm.set()
accept_confirm = asyncio.Event()           # 逐章验收确认（仅内存，不落盘）
accept_confirm.set()
stop_requested = False
current_run_id: Optional[str] = None
current_output_path: Optional[str] = None
progress_lock = asyncio.Lock()
log_listener = None

logger = logging.getLogger("textforge")

# 流水线阶段顺序（用于断点恢复时判断阶段是否已完成）
PHASE_ORDER = {
    "phase0": 0, "phase1": 1, "phase1_fixreview": 2, "phase2": 2,
    "phase2_waiting": 3, "phase3_accept": 3, "phase3-1": 4, "phase4": 5,
}

def phase_index(name: str | None) -> int:
    return PHASE_ORDER.get(name or "", -1)

def phase_passed(progress: dict, phase_name: str) -> bool:
    """当前进度阶段是否已越过指定阶段（严格大于）。"""
    return phase_index(progress.get("current_phase")) > PHASE_ORDER[phase_name]
