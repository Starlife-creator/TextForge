"""事件流的行为验证（进程内直接驱动生成器，绕开网络与进程边界）。

覆盖三个只能靠重放缓冲 / seq 过滤才能成立的场景：
  1) 事件先于订阅发生 → 后连的客户端仍能收到（run_start 必达）
  2) 切换 run 时队列里的上一轮残留事件不会被套用到新 run
  3) 重放与实时推送不重复、不丢失
"""
import asyncio, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from routes import state
from routes import events as E
import routes.events as ev

# 控制台编码：本地 Windows 常是 GBK，GitHub runner 是 cp1252，
# 直接 print 中文会 UnicodeEncodeError 让整套测试「假失败」。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

FAILS = []
def check(name, cond, extra=""):
    try:
        print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  [{extra}]" if extra and not cond else ""))
    except UnicodeEncodeError:
        print(f"{'PASS' if cond else 'FAIL'}  {name.encode('ascii','replace').decode()}")
    if not cond:
        FAILS.append(name)

async def collect(gen, want_chunks, timeout=3.0):
    """从 SSE 生成器里读取 N 个数据块，返回 (全部文本, 数据块数)。"""
    buf = []
    t0 = asyncio.get_event_loop().time()
    try:
        while len(buf) < want_chunks and asyncio.get_event_loop().time() - t0 < timeout:
            buf.append(await asyncio.wait_for(gen.__anext__(), timeout=max(0.05, timeout)))
    except (asyncio.TimeoutError, StopAsyncIteration):
        pass
    return "".join(buf), len(buf)

async def t1_replay():
    """先 emit，后订阅 —— run_start 必须被重放。"""
    E._recent.clear(); E._recent_order.clear(); E._subscribers.clear()
    state.current_run_id = "R1"
    ev.sse_emit("run_start", {"run_id": "R1", "resumed": False})
    ev.sse_emit("batch_start", {"batch_id": 0, "chapter_ids": ["1"]})
    assert not E._subscribers, "此刻还不该有任何订阅者"
    resp = await E.events()
    gen = resp.body_iterator
    text, n = await collect(gen, 3)
    return text, n

async def t2_no_cross_run_contamination():
    """上一轮 run 的残留事件不得被套用到新 run。"""
    E._recent.clear(); E._recent_order.clear(); E._subscribers.clear()
    state.current_run_id = "RA"
    ev.sse_emit("run_start", {"run_id": "RA"})
    resp = await E.events()
    gen = resp.body_iterator
    await collect(gen, 2)                       # 绑定到 RA
    # RA 事件积压（客户端读得慢）
    ev.sse_emit("error", {"kind": "unknown", "code": "poison", "message": "上一轮的毒事件"})
    # 切换到 RB
    state.current_run_id = "RB"
    ev.sse_emit("run_start", {"run_id": "RB"})
    text, _ = await collect(gen, 3)
    return text

async def t3_no_duplicate():
    """订阅之后实时推送的事件必须恰好送达一次（不被重放重复）。"""
    E._recent.clear(); E._recent_order.clear(); E._subscribers.clear()
    state.current_run_id = "RC"
    resp = await E.events()
    gen = resp.body_iterator
    await collect(gen, 1)                       # 先拿到 run_id
    for i in range(3):                          # 订阅建立之后再发
        ev.sse_emit("batch_done", {"batch_id": i})
    text, _ = await collect(gen, 3)
    return text

t1, _n1 = asyncio.run(t1_replay())
check("先 emit 后订阅：run_start 被重放", "run_id" in t1 and "run_start" in t1, t1[:200])
check("先 emit 后订阅：非重放事件（batch_start）不被重投", "batch_start" not in t1, t1[:200])

t2 = asyncio.run(t2_no_cross_run_contamination())
check("切换 run 后收到新的 run_id", '"RB"' in t2, t2[:200])
check("切换 run 后收到新 run 的 run_start（RA 那条已在上一轮 collect 消费）",
      t2.count("run_start") == 1, t2[:300])
check("上一轮 run 的残留事件未被套用到新 run", "poison" not in t2, t2[:400])

t3 = asyncio.run(t3_no_duplicate())
check("订阅后的实时事件恰好送达一次（无重复）",
      t3.count("batch_done") == 3, f"batch_done 出现 {t3.count('batch_done')} 次")
check("三个 batch_done 全部送达", all(f'"batch_id": {i}' in t3 for i in range(3)), t3[:300])

async def t4_replay_is_narrow():
    """重放必须只覆盖 run_start，不得把 phase_done/stream_chunk 之类重投。"""
    E._recent.clear(); E._recent_order.clear(); E._subscribers.clear()
    state.current_run_id = "RD"
    ev.sse_emit("run_start", {"run_id": "RD"})
    ev.sse_emit("phase_done", {"phase": "phase1"})
    ev.sse_emit("stream_chunk", {"text": "旧文本"})
    ev.sse_emit("batch_start", {"batch_id": 9})
    resp = await E.events()
    gen = resp.body_iterator
    text, _ = await collect(gen, 3)
    return text

t4 = asyncio.run(t4_replay_is_narrow())
check("重放包含 run_start（它才是 emit 早于订阅的那一条）", "run_start" in t4, t4[:200])
check("重放不含 phase_done（否则重连会刷重复日志）", "phase_done" not in t4, t4[:300])
check("重放不含 stream_chunk（否则污染流式预览）", "stream_chunk" not in t4, t4[:300])
check("重放不含 batch_start（否则会清空流式预览）", "batch_start" not in t4, t4[:300])

# ---------------------------------------------------------------------------
# 前端 run_start 处理器的判据
#
# 这里复刻 src/App.vue 的 handleSSEEvent 前端逻辑，逐场景验证：
# 曾经的 bug 是用 `payload.run_id === runId.value` 判断重放 —— 但后端在重放
# run_start **之前**必定先发 run_id 并把它写进 runId.value，判据 100% 命中，
# 整个 handler 变成死代码（断点恢复提示 / 运行计时器 / 阶段标签全失效），
# 而当时所有测试都是绿的。只有真正把前端逻辑跑起来才能发现。
# ---------------------------------------------------------------------------
def fe_handle(events, initial=None):
    """最小前端模型：喂进 SSE 事件序列，返回最终 UI 状态。"""
    run_id = None
    st = {"pipelineRunning": False, "reconnectLogged": 0,
          "confirmedGates": {"old"}, "finalFiles": ["上一轮的成品"], "topTip": None,
          "currentPhase": "", "elapsedStarted": False}
    if initial:
        st.update(initial)
    for ev, data in events:
        if ev == "run_id":
            run_id = data["run_id"]
        elif ev == "run_start":
            if data.get("_replay"):
                st["reconnectLogged"] += 1
                continue
            st["pipelineRunning"] = True
            st["topTip"] = "断点继续" if data.get("resumed") else None
            run_id = data.get("run_id") or run_id
            st["confirmedGates"] = set()
            st["finalFiles"] = []
            st["currentPhase"] = "（断点恢复中）" if data.get("resumed") else "启动"
            st["elapsedStarted"] = True
    return run_id, st

# 场景 A：全新一轮 run（run_start 是实时投递，不是重放）
_, a = fe_handle([("run_id", {"run_id": "NEW"}),
                  ("run_start", {"run_id": "NEW", "resumed": False})])
check("新 run：run_start 被正常处理（不是重放）", a["pipelineRunning"] and a["confirmedGates"] == set(), str(a))
check("新 run：阶段标签与计时器被设置", a["currentPhase"] == "启动" and a["elapsedStarted"], str(a))
check("新 run：成品列表被清空", a["finalFiles"] == [], str(a))

# 场景 B：断点恢复（run_start 带 resumed=True）
_, b = fe_handle([("run_id", {"run_id": "RES"}),
                  ("run_start", {"run_id": "RES", "resumed": True})])
check("断点恢复：提示条被设置", b["topTip"] == "断点继续", str(b))
check("断点恢复：阶段标签正确", b["currentPhase"] == "（断点恢复中）", str(b))

# 场景 C：运行中网络抖动重连（服务端标记了 _replay）
mid = {"pipelineRunning": True, "confirmedGates": {"accept:3"}, "finalFiles": ["本次成品"]}
_, c = fe_handle([("run_id", {"run_id": "RES"}),
                  ("run_start", {"run_id": "RES", "resumed": True, "_replay": True})], mid)
check("重连重放：日志提示已记录", c["reconnectLogged"] == 1, str(c))
# 关键：重放绝不能清掉本轮的闸门闩锁 / 成品列表
check("重放不会执行 run_start 的正常处理（否则闩锁被清）",
      c["confirmedGates"] == {"accept:3"}, str(c))
check("重放不会清空成品列表", c["finalFiles"] == ["本次成品"], str(c))

# 场景 D：run 结束后重连（run_start 仍会被重放，因为 current_run_id 从不清空）
_, d = fe_handle([("run_id", {"run_id": "OLD"}),
                  ("run_start", {"run_id": "OLD", "resumed": False, "_replay": True})])
check("run 结束后重连：被识别为重放", d["reconnectLogged"] == 1, str(d))

print()
print("=== 全部通过 ===" if not FAILS else f"=== {len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
