"""闸门处的停止语义（R1 / B1.1）。

三条断言各自盯一种错法，缺一条就会漏放一个真回归：

1. 事件已置位 + stop_requested ⇒ **必须抛 CancelledError**。今天不抛：/api/stop 把
   5 个闸门事件全 set，wait_gate 的 `while not event.is_set()` 因此根本不看停止标志，
   于是「用户按停止」被当成「用户点确认」，本阶段提交后还要把下一阶段整个跑完
   （几十次模型调用）才轮到下一个停止检查。
2. 抛之前**必须已经 absorb**。`run_pipeline` 的 finally 会把内存 progress 整份写回
   磁盘；如果因为停止而跳过并回，用户刚拿到 HTTP 200 的选择会被悄悄回退 ——
   正是 absorb_gate_result docstring 禁止的那个坑（由对抗审查提出，已复验）。
3. 超时路径**必须仍然抛 GateTimeoutError**。超时分支自己会置 stop_requested，
   所以把停止复查放进 finally（或循环之外任何晚于 raise 的位置）都会把它误吞成
   CancelledError，界面从此看不到「闸门超时已安全暂停」。
"""
from __future__ import annotations

import asyncio

import pytest

from routes import state
from utils.atomic import atomic_write_json


@pytest.fixture(autouse=True)
def _clean_gate_state():
    state.stop_requested = False
    state.reset_gate_state()
    yield
    state.stop_requested = False
    state.reset_gate_state()


def _disk_choice(tmp_path):
    """控制端点「先写盘再 set」之后的磁盘现场。"""
    pp = tmp_path / "progress.json"
    atomic_write_json(pp, {"accepted_batches": [7], "regen_count": {"5": 4},
                           "fix_list": [{"gap": 1}]})
    return pp


def test_stop_at_gate_raises_and_keeps_confirmed_choice(tmp_path) -> None:
    pp = _disk_choice(tmp_path)
    mem = {"accepted_batches": [], "current_phase": "phase3_accept"}
    event = asyncio.Event()
    event.set()                      # /api/stop 的副作用：闸门事件被 set

    async def run() -> None:
        state.stop_requested = True
        with pytest.raises(asyncio.CancelledError):
            await state.wait_gate(
                event, gate=state.GATE_ACCEPT, progress=mem, progress_path=pp,
                tick=0.01, absorb_keys=("accepted_batches", "regen_count"))

    asyncio.run(run())
    # 抛错之前必须已经把用户选择并回内存：随后 run_pipeline 的 finally 会整份写回磁盘
    assert mem["accepted_batches"] == [7], f"停止时并回被跳过，写回会抹掉用户确认：{mem!r}"
    assert mem["regen_count"] == {"5": 4}, "重写计数同样不能被 finally 抹掉"
    assert "fix_list" not in mem, "absorb_keys 没列的键不该被顺手并进"


def test_normal_confirm_still_absorbs_and_returns(tmp_path) -> None:
    pp = _disk_choice(tmp_path)
    mem = {"accepted_batches": [], "current_phase": "phase3_accept"}
    event = asyncio.Event()
    event.set()                      # 用户点了确认，且没有按停止

    async def run() -> None:
        await state.wait_gate(
            event, gate=state.GATE_ACCEPT, progress=mem, progress_path=pp,
            tick=0.01, absorb_keys=("accepted_batches", "regen_count"))

    asyncio.run(run())               # 正常返回，不抛
    assert mem["accepted_batches"] == [7]
    assert mem["regen_count"] == {"5": 4}


def test_gate_timeout_is_not_swallowed_by_stop_check() -> None:
    event = asyncio.Event()          # 没人确认 → 走到超时分支

    async def run() -> None:
        with pytest.raises(state.GateTimeoutError):
            await state.wait_gate(
                event, gate=state.GATE_FIX, progress={}, progress_path=None,
                tick=0.01, timeout=0.05)

    asyncio.run(run())
    # 超时分支自己置了停止标志：停止复查若被挪进 finally，这条用例就会变成
    # CancelledError 而红 —— 这正是它存在的意义。
    assert state.stop_requested is True
