import asyncio, json, logging, os, time
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
split_redo = asyncio.Event()               # P0.5 拆书预览里「按自定义标记重新拆分」
split_redo_markers: list = []              # 随 split_redo 一起传的标记清单
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

# ---------------------------------------------------------------------------
# #2 人工闸门超时与等待时长上报
#
# 五个人工闸门此前是模块级内存 Event + 无界 `while not event` 循环：事件丢失、
# 前端未弹窗、刷新后 resume 未命中时任务永久挂起，除手动 stop 外无自救途径。
# 这里登记每个闸门的进入时间戳，提供统一的 `wait_gate` 等待原语：
#   - 超过 GATE_TIMEOUT_SECONDS（默认 24h，TEXTFORGE_GATE_TIMEOUT 可配）自动安全暂停；
#   - 等待时长经 `/api/status` 的 `active_gate` / `waiting_seconds` 上报前端；
#   - 超时只置 stop_requested 并把状态写进内存 progress，由 run_pipeline 的
#     finally 统一落盘，不在这里重复写盘竞争 progress_lock。
# ---------------------------------------------------------------------------

# 闸门标识（与 /api/status、_build_resume、SSE 事件共用同一组 key）。
GATE_BLUEPRINT = "blueprint"
GATE_SPLIT = "split"
GATE_FIX = "fix"
GATE_ACCEPT = "accept"
GATE_QC = "qc"

DEFAULT_GATE_TIMEOUT_SECONDS = 24 * 3600  # 24h：人工确认通常分钟级；超时即异常挂起

def _env_gate_timeout() -> float:
    try:
        raw = os.environ.get("TEXTFORGE_GATE_TIMEOUT", "").strip()
        return max(1.0, float(raw or DEFAULT_GATE_TIMEOUT_SECONDS))
    except (TypeError, ValueError):
        return DEFAULT_GATE_TIMEOUT_SECONDS

GATE_TIMEOUT_SECONDS = _env_gate_timeout()

# gate key → monotonic 进入时间；_gate_waiting 记录当前正等待的闸门。
_gate_enter: dict[str, float] = {}
_gate_waiting: Optional[str] = None


class GateTimeoutError(asyncio.CancelledError):
    """闸门等待超时终止。

    继承 CancelledError：与用户主动停止同走 run_pipeline 的「停止收尾」路径
    （SSE stopped + finally 落盘），只是携带更具体的闸门标识供界面提示。
    """


def gate_enter(name: str) -> None:
    """登记闸门进入时间（同一闸门重复进入覆盖旧时间戳）。"""
    global _gate_waiting
    _gate_enter[name] = time.monotonic()
    _gate_waiting = name


def gate_exit(name: str) -> None:
    """注销闸门等待态；只有当前等待中的闸门才清空 _gate_waiting。"""
    global _gate_waiting
    _gate_enter.pop(name, None)
    if _gate_waiting == name:
        _gate_waiting = None


def active_gate() -> Optional[str]:
    """当前正等待用户确认的闸门 key；无等待返回 None。"""
    return _gate_waiting


def gate_wait_seconds(name: Optional[str] = None) -> float:
    """返回某闸门（缺省为当前等待闸门）已等待的秒数，保留 1 位小数；未在等待返回 0。"""
    key = name or _gate_waiting
    if not key or key not in _gate_enter:
        return 0.0
    return round(time.monotonic() - _gate_enter[key], 1)


def reset_gate_state() -> None:
    """测试辅助：清空闸门内存登记。事件本身与 stop_requested 由测试按需复位。"""
    global _gate_waiting
    _gate_enter.clear()
    _gate_waiting = None


async def wait_gate(event: asyncio.Event, *, gate: str, progress: Optional[dict],
                    progress_path=None, tick: float = 1.0,
                    timeout: Optional[float] = None,
                    alternative: Optional[asyncio.Event] = None,
                    release_events: tuple[asyncio.Event, ...] = ()) -> None:
    """等待一个人工闸门，替代各循环里无界的 `while not event`。

    - `alternative`：同闸门内的备选出路（如拆书预览门的「重新拆分」），它被
      置位时同样结束等待；调用方随后按两个 Event 的状态分支处理；
    - `stop_requested` 已置位 → 抛 asyncio.CancelledError（复用既有停止路径）；
    - 等待超过 `timeout`（缺省 GATE_TIMEOUT_SECONDS）→ 置 stop_requested、把超时
      闸门与已等待时长记进内存 progress、释放本闸门与 `release_events`，
      抛 GateTimeoutError；
    - 正常确认 → 注销进入时间戳，供下一次等待重新计时。

    `progress_path` 仅留作接口对称（写盘由 run_pipeline finally 统一完成）。
    """
    global stop_requested
    deadline = time.monotonic() + (GATE_TIMEOUT_SECONDS if timeout is None else timeout)
    gate_enter(gate)
    try:
        while not event.is_set() and not (alternative is not None and alternative.is_set()):
            if stop_requested:
                raise asyncio.CancelledError()
            if time.monotonic() >= deadline:
                stop_requested = True
                if progress is not None:
                    progress["gate_timed_out"] = True
                    progress["gate_timeout_gate"] = gate
                    progress["gate_wait_seconds"] = gate_wait_seconds(gate)
                    progress["last_gate_timeout_at"] = time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                for ev in release_events:
                    ev.set()
                event.set()
                logger.warning(
                    f"[gate] 闸门 {gate} 等待超时（{gate_wait_seconds(gate)}s），任务已安全暂停")
                raise GateTimeoutError(f"人工闸门 {gate} 等待超时，任务已安全暂停")
            await asyncio.sleep(tick)
    finally:
        gate_exit(gate)

# 最近一次心跳时间（ISO8601 Z）。**只在内存里维护**。
#
# 原先心跳是把整个 progress.json 读出来、改这一个字段、再全量写回 + fsync。
# 缝合阶段 progress.json 可达数十 MB（锚点内嵌全文），每 15s 一次全量重写
# 对磁盘是实打实的写放大，对 CPU 则是每 15s 解析+序列化几十 MB JSON。
# 而 last_heartbeat 的唯一用途是前端判断「是否僵死」，并不参与任何恢复决策，
# 根本不需要落盘。改成内存维护后：
#   - 界面读到的心跳反而更准（不再有 fsync 延迟）；
#   - progress.json 只在状态真正变化时才写。
last_heartbeat: Optional[str] = None

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
