import asyncio, json, logging
from pathlib import Path
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
qc_confirm = asyncio.Event()               # 质检拦截决策确认（强制导出/停止）（仅内存，不落盘）
qc_confirm.set()
stop_requested = False
current_run_id: Optional[str] = None
current_output_path: Optional[str] = None
current_req = None  # A1：运行中的 StartRequest（含 api_key，仅内存、不落盘），供单章重生成复用鉴权
progress_lock = asyncio.Lock()
log_listener = None

logger = logging.getLogger("textforge")

# 流水线阶段顺序（用于断点恢复时判断阶段是否已完成）
#
# 必须**严格递增**、且覆盖每一个会被写进 current_phase 的值。历史上这里有两处坑：
# 1) phase2_waiting / phase3_accept / phase3_qc 全映射到 3，于是
#    phase_passed(progress,'phase2_waiting') 在停在 phase3_accept 时返回 False
#    → phase2_blueprint 认为蓝图未完成而**重新调用 LLM 生成并覆盖 Story_Bible.md**
#    （前面批次按旧蓝图重构、后面按新蓝图，整书逻辑撕裂），逐章验收门的恢复逻辑也因此永远走不到。
# 2) phase0_confirm 缺失 → phase_index 返回 -1，比 phase0 还「早」；
#    而 status._build_resume 又把它当作一等公民的无限等待闸门处理。两层认知不一致。
PHASE_ORDER = {
    "phase0": 0, "phase0_confirm": 1,
    "phase1": 2, "phase1_fixreview": 3,
    "phase2": 4, "phase2_waiting": 5,
    "phase3_accept": 6, "phase3_qc": 7,
    "phase3-1": 8, "phase4": 9,
}

class GateReadError(RuntimeError):
    """闸门回读失败。单独成类，便于 run_pipeline 归类为可恢复错误并给出可操作提示。"""

def phase_index(name: str | None) -> int:
    return PHASE_ORDER.get(name or "", -1)

def phase_passed(progress: dict, phase_name: str) -> bool:
    """当前进度阶段是否已越过指定阶段（严格大于）。"""
    return phase_index(progress.get("current_phase")) > PHASE_ORDER[phase_name]


async def absorb_gate_result(progress: dict, progress_path, keys) -> None:
    """闸门通过后，把控制端点写入 progress.json 的字段并回流水线内存。

    为什么需要它：控制端点（split/fix/accept/export/blueprint confirm）为了做阶段校验，
    必须自己读盘写盘；而等待循环又每秒把自己的内存 progress 整个写回。
    两者互不知情，导致用户刚确认的 fix_list / accepted_batches / _qc_force
    在 ≤1s 内被心跳覆盖 —— 「修断层」功能静默失效且无任何报错。

    约定：控制端点**先 atomic_write_json 再 set Event**，因此 Event 一旦置位，
    这里读盘必然拿到用户刚写的值。current_phase 仍由流水线自己推进（端点不再写它），
    保证进度单调前进、不会回退到 phase2 触发蓝图重生成。

    读盘失败时**必须抛**，不能静默沿用内存旧值：调用方紧接着就会把内存写回磁盘，
    那样等于把用户刚确认的选择用空值覆盖掉，且界面早已收到 200「已确认」。
    抛 GateReadError，由 run_pipeline 单独归类为「可断点恢复」并给出可操作提示。

    必须持有 progress_lock：读者不加锁时，Windows 上 os.replace 竞争会瞬时抛
    PermissionError（OneDrive/桌面同步、杀软扫描都会），这正是重试存在的理由 ——
    加锁后这些竞争从根上消失。

    退避预算 ~6.2s（0.2+0.4+0.8+1.6+3.2）。最后一次失败后**不再睡** —— 醒来就抛是
    纯浪费，而且 progress_lock 是所有 progress.json 写者（6 个控制端点 + 心跳 +
    log_error + 收尾写）的全局锁，白白占住 6.4s 会让界面所有按钮卡死。
    """
    last_err = None
    async with progress_lock:
        if not Path(progress_path).exists():
            raise GateReadError(f"进度文件不存在：{progress_path}（输出目录可能被移动或清理）")
        for attempt in range(6):
            try:
                disk = json.loads(Path(progress_path).read_text(encoding='utf-8'))
                break
            except (OSError, json.JSONDecodeError, TypeError) as e:
                last_err = e
                if attempt < 5:          # 醒来就要抛的那次不睡
                    await asyncio.sleep(0.2 * (2 ** attempt))
        else:
            logger.error(f"[gate] 读回 progress.json 连续 6 次失败，中止以免丢失用户选择: {last_err}")
            raise GateReadError(
                f"读回进度文件连续失败（{type(last_err).__name__}: {last_err}）。"
                f"你的选择已保存在 progress.json 中未丢失；"
                f"若输出目录位于 OneDrive / 桌面同步等会锁文件的目录，请把它移到本地磁盘后重新开始（会从断点继续）。"
            ) from last_err
    for k in keys:
        if k in disk:
            progress[k] = disk[k]
