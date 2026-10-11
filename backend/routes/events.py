from fastapi import APIRouter
from fastapi.responses import StreamingResponse
import asyncio, json, time, itertools
from collections import deque
from routes import state

router = APIRouter()
_subscribers: dict[str, list[asyncio.Queue]] = {}

# 心跳间隔。必须显著小于前端 sse.ts 的静默看门狗（90s），
# 否则「等 run_id」的空窗期会让客户端先 abort，重试耗尽后误报「后端连接断开」。
HEARTBEAT_SEC = 15.0
# 轮询间隔：决定「开始重构」后事件送达的延迟上限（等待期与已绑定期都用它，
# 所以第二轮 run 的首批事件延迟 ≤ POLL_SEC，而不是一个心跳周期）。
POLL_SEC = 0.25
# 无 run 时的最长挂起时长（安全阀，正常不会触发；客户端断开走 GeneratorExit）
MAX_WAIT_SEC = 6 * 3600.0
# 重放缓冲：/api/start 里 create_task 后微秒级就会 emit("run_start")，而 SSE
# 生成器最多要 POLL_SEC 才发现新 run_id —— 没有重放缓冲，run_start 是**必然**丢失的。
#
# 只重放 run_start 这一个「run 序章」事件，**不**重放其余事件。早期版本重放最近 64 条，
# 结果在网络抖动重连时把一批旧事件重投给前端，而前端对它们大多不是幂等的：
#   batch_start  → 清空流式预览
#   stream_chunk → 拼接旧文本，污染当前预览
#   phase_done/batch_done → 重复日志
#   run_start    → 重置 confirmedGates / finalFiles / topError
# 而 run 之后的全部状态本来就由 /api/status + _build_resume 负责恢复，不需要重放兜底。
REPLAYABLE = frozenset({"run_start"})
REPLAY_MAXLEN = 8
REPLAY_RUNS = 3

_seq = itertools.count(1)
_recent: dict[str, deque] = {}
_recent_order: deque = deque()

def sse_emit(event: str, payload: dict, run_id: str | None = None):
    rid = run_id or state.current_run_id
    if not rid: return
    item = {"seq": next(_seq), "run_id": rid, "event": event, "data": payload}
    if event in REPLAYABLE:
        buf = _recent.get(rid)
        if buf is None:
            buf = _recent[rid] = deque(maxlen=REPLAY_MAXLEN)
            _recent_order.append(rid)
            while len(_recent_order) > REPLAY_RUNS:
                _recent.pop(_recent_order.popleft(), None)
        buf.append(item)
    for q in list(_subscribers.get(rid, [])):
        try:
            q.put_nowait(item)
        except asyncio.QueueFull:
            # 队列满：丢弃最旧、保留最新，保证进度/终态事件最终可达
            try:
                q.get_nowait()
                q.put_nowait(item)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass

def _unsubscribe(rid: str, q: asyncio.Queue):
    subs = _subscribers.get(rid)
    if not subs: return
    try: subs.remove(q)
    except ValueError: pass
    if not subs: _subscribers.pop(rid, None)

def _fmt(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

@router.get("/api/events")
async def events(run_id: str | None = None):
    """事件流。连接会「跟随」当前 run：run_id 仅作为首次绑定的提示，
    之后当 state.current_run_id 变化时自动改绑新 run。

    三个必须同时满足的不变式：
    1) 等待期与绑定后都发心跳 —— 否则前端的 90s 静默看门狗会 abort 掉连接；
    2) 绑定前先重放 `_recent[rid]` —— 否则 run_start 这类「emit 早于订阅」的事件必丢；
    3) 事件按 seq 过滤 + 改绑时清空队列 —— 否则上一轮 run 的残留事件会被当成
       新 run 的事件应用（例如旧的 error 会把新 run 的轮询停掉并弹红条）。
    """
    q = asyncio.Queue(maxsize=1000)

    async def gen():
        nonlocal run_id
        bound: str | None = run_id or None
        try:
            while True:
                if bound is None:
                    started = last_beat = time.monotonic()
                    while not state.current_run_id:
                        if time.monotonic() - started > MAX_WAIT_SEC:
                            yield _fmt("error", {"kind": "unknown", "code": "no_run",
                                                  "message": "等待 run_id 超时"})
                            return
                        await asyncio.sleep(POLL_SEC)
                        if time.monotonic() - last_beat >= HEARTBEAT_SEC:
                            last_beat = time.monotonic()
                            yield ": waiting\n\n"
                    bound = state.current_run_id

                # 快照重放缓冲 → 丢弃队列中已重放的部分 → 再注册订阅。
                # 顺序保证不会漏（入缓冲先于入队）也不会重（seq 过滤）。
                replay = list(_recent.get(bound) or ())
                seen_seq = replay[-1]["seq"] if replay else 0
                while True:
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                _subscribers.setdefault(bound, []).append(q)
                yield _fmt("run_id", {"run_id": bound})
                for item in replay:
                    # 必须由服务端标记「这是重放」。
                    # 前端无法靠 run_id 相等来区分重放与首次投递 —— 本行
                    # run_id 事件已经把 runId.value 设成了同一个值，
                    # 任何基于 run_id 相等的判断都会 100% 命中，
                    # 把 run_start 的正常处理（断点恢复提示、计时器、阶段标签）全部废掉。
                    yield _fmt(item["event"], {**item["data"], "_replay": True})

                switched = False
                last_beat = time.monotonic()
                try:
                    while not switched:
                        try:
                            item = await asyncio.wait_for(q.get(), timeout=POLL_SEC)
                        except asyncio.TimeoutError:
                            if time.monotonic() - last_beat >= HEARTBEAT_SEC:
                                last_beat = time.monotonic()
                                yield ": heartbeat\n\n"
                            switched = state.current_run_id != bound
                            continue
                        # 必须先判切换、再投送。反过来的话，队列里上一轮 run 的
                        # 残留事件（比如一条 error）会在发现切换之前被 yield 出去，
                        # 被前端当成新 run 的事件处理 —— 停掉新 run 的轮询并弹红条。
                        if state.current_run_id != bound:
                            switched = True
                            break
                        if item["seq"] <= seen_seq:
                            continue          # 已在重放阶段投送过
                        if item["run_id"] != bound:
                            continue          # 理论上到不了这里，防御性保留
                        seen_seq = item["seq"]
                        yield _fmt(item["event"], item["data"])
                finally:
                    _unsubscribe(bound, q)

                if not switched:
                    return
                bound = None  # 跟随新的 current_run_id
        except asyncio.CancelledError:
            return

    return StreamingResponse(gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"})