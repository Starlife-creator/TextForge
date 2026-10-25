"""#2 人工闸门超时与等待时长上报；#8 闸门状态机后端单测。

覆盖：
  - 正常确认 → 等待结束、计时清零；
  - 超时 → 置 stop_requested、progress 留痕、释放事件、抛 GateTimeoutError；
  - 外部停止（stop_requested）→ 抛 CancelledError，复用既有停止路径；
  - 备选事件（拆书「重新拆分」）可结束等待；
  - active_gate / gate_wait_seconds 上报口径；
  - 超时默认值 24h，可被较小 timeout 参数覆盖。

说明：本项目未装 pytest-asyncio，异步逻辑用 asyncio.run 在同步用例内驱动
（与 tests/test_stitch.py 同一约定）。
"""

import asyncio

import pytest

from routes import state


@pytest.fixture(autouse=True)
def _reset_gate_state():
    """每个用例前复位模块级闸门态与停止标志，避免跨用例污染。"""
    state.stop_requested = False
    state.reset_gate_state()
    state.blueprint_confirmed.clear()
    state.split_confirm.clear()
    state.split_redo.clear()
    state.fix_review_confirm.clear()
    state.accept_confirm.clear()
    state.qc_confirm.clear()
    yield
    state.stop_requested = False
    state.reset_gate_state()
    state.blueprint_confirmed.clear()
    state.split_confirm.clear()
    state.split_redo.clear()
    state.fix_review_confirm.clear()
    state.accept_confirm.clear()
    state.qc_confirm.clear()


def test_gate_enter_exit_reports_waiting():
    state.gate_enter(state.GATE_BLUEPRINT)
    assert state.active_gate() == state.GATE_BLUEPRINT
    assert state.gate_wait_seconds() >= 0
    state.gate_exit(state.GATE_BLUEPRINT)
    assert state.active_gate() is None
    assert state.gate_wait_seconds() == 0.0


def test_wait_gate_normal_release():
    async def _run():
        event = asyncio.Event()
        progress: dict = {}
        asyncio.get_running_loop().call_later(0.05, event.set)
        await state.wait_gate(event, gate=state.GATE_BLUEPRINT, progress=progress,
                              tick=0.02, timeout=5.0)
        return progress

    progress = asyncio.run(_run())
    assert state.active_gate() is None
    assert state.gate_wait_seconds() == 0.0
    assert progress == {}


def test_wait_gate_timeout_records_state_and_releases():
    async def _run():
        event = asyncio.Event()
        alt = asyncio.Event()
        progress: dict = {}
        with pytest.raises(state.GateTimeoutError):
            await state.wait_gate(
                event, alternative=alt, gate=state.GATE_QC,
                progress=progress, tick=0.02, timeout=0.15,
                release_events=(alt,))
        return progress, event, alt

    progress, event, alt = asyncio.run(_run())
    # 超时置停止标志，progress 留痕闸门与等待时长
    assert state.stop_requested is True
    assert progress["gate_timed_out"] is True
    assert progress["gate_timeout_gate"] == state.GATE_QC
    assert progress["gate_wait_seconds"] >= 0.15
    assert progress["last_gate_timeout_at"]
    # 本闸门与关联事件都被释放，避免现场遗留未决事件
    assert event.is_set()
    assert alt.is_set()


def test_wait_gate_stop_requested_cancels():
    async def _run():
        event = asyncio.Event()
        state.stop_requested = True
        progress: dict = {}
        with pytest.raises(asyncio.CancelledError):
            await state.wait_gate(event, gate=state.GATE_ACCEPT, progress=progress,
                                  tick=0.02, timeout=5.0)

    asyncio.run(_run())
    # 被取消时不留等待态
    assert state.active_gate() is None


def test_wait_gate_alternative_releases_without_timeout():
    """拆书预览门：split_redo 置位同样结束等待，且不算超时。"""
    async def _run():
        event = state.split_confirm
        alt = state.split_redo
        progress: dict = {}
        asyncio.get_running_loop().call_later(0.05, alt.set)
        await state.wait_gate(event, alternative=alt, gate=state.GATE_SPLIT,
                              progress=progress, tick=0.02, timeout=5.0)
        return progress

    progress = asyncio.run(_run())
    assert state.stop_requested is False
    assert progress == {}


def test_gate_timeout_default_is_one_day():
    # 默认 24h；环境变量在导入时读取，这里校验常量口径
    assert state.GATE_TIMEOUT_SECONDS == 24 * 3600


def test_wait_gate_releases_on_timeout_even_without_progress():
    async def _run():
        event = asyncio.Event()
        with pytest.raises(state.GateTimeoutError):
            await state.wait_gate(event, gate=state.GATE_FIX, progress=None,
                                  tick=0.02, timeout=0.1)
        return event

    event = asyncio.run(_run())
    assert event.is_set()
    assert state.stop_requested is True
