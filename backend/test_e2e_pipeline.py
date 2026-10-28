"""端到端：真正把整条 phase0→phase4 流水线跑一遍，验证成品质量。

这是四轮静态审查都覆盖不到的一类问题：成品 docx 到底对不对。
用 mock_llm.py 起本地 OpenAI 兼容端点，全程走真实 HTTP，不打桩被测代码。
"""
import json, os, re, shutil, subprocess, sys, tempfile, threading, time, urllib.request
from pathlib import Path

# 控制台编码：本地 Windows 常是 GBK，GitHub runner 是 cp1252，
# 直接 print 中文会 UnicodeEncodeError 让整套测试「假失败」。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BREAK_MARK = "===CHAPTER_BREAK==="
BACKEND = Path(__file__).parent
# 父进程（本脚本）本身要 import docx/requests/uvicorn，必然由装好依赖的解释器
# 启动；子进程没有理由换另一个解释器（venv 存在但陈旧时会悄悄用错）。
# CI 激活 venv 后 sys.executable 就是 venv 里的 python。
PY = sys.executable
# 端口可经环境变量覆盖：固定端口在 CI 并发/残留进程下会假失败
LLM_PORT = int(os.environ.get("TF_E2E_LLM_PORT", "8299"))
API_PORT = int(os.environ.get("TF_E2E_API_PORT", "8298"))

FAILS = []
# 现有场景刻意验证调用次数/恢复语义，关闭自动重写避免多出的重试干扰断言；
# 自动重写由场景九单独覆盖。
NO_AUTO_REWRITE = {"auto_rewrite_bad_batch": False}
def check(name, cond, extra=""):
    try:
        print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  [{extra}]" if extra and not cond else ""))
    except UnicodeEncodeError:
        print(f"{'PASS' if cond else 'FAIL'}  {name.encode('ascii','replace').decode()}")
    if not cond:
        FAILS.append(name)


def post(url, obj=None, method="POST", timeout=120):
    data = json.dumps(obj).encode() if obj is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        # 带上响应体：422 之类的校验错误只有 body 才说得清是哪个字段
        detail = e.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"{method} {url} → HTTP {e.code}: {detail}") from e


def get(url, timeout=30):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        # 带上响应体：404 只有 body 说得清原因（如「上一次生成失败：…」）
        detail = e.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"GET {url} → HTTP {e.code}: {detail}") from e


def quality_report(port, outd):
    """拉质量报告；拿不到时**不抛**，把原因交回给调用方去判 FAIL。

    报告缺失时该接口返回 404。原先直接 urlopen 会在场景中途抛 HTTPError，
    把后面所有场景一起带走 —— v0.13.0 首次发布时 CI 上就只剩一个 traceback，
    连「为什么没有报告」都看不到。
    """
    try:
        url = f"{port}/api/output/quality?output_path={urllib.request.quote(str(outd))}"
        return get(url), ""
    except RuntimeError as e:
        return {}, str(e)


def put(url, obj, timeout=30):
    req = urllib.request.Request(url, data=json.dumps(obj).encode(), method="PUT",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


# 队列放在 dict 里是为了能整体换掉（scene() 会重绑一个新列表），
# 而驱动线程每次循环重新读取 —— 直接对列表 clear() 会让驱动的游标
# 越界在旧长度上，从此再也看不到任何闸门事件（表现为整轮卡死）。
_EVENTS = {"q": []}


def scene(title):
    """进入下一个场景：换掉事件队列、把驱动状态归零，再确认后端已空闲。

    驱动是全局唯一的，事件队列若不清，上一轮迟到的闸门事件会被当作当前这轮去确认
    —— 表现为场景标题下方凭空多出一串「已确认蓝图」（真实干扰过一次场景八的判读，
    而它同时也是潜在的串台确认）。seen/run/_CUR_RUN 一起归零，等价于每个场景
    拿到一个干净的驱动。
    """
    _EVENTS["q"] = []          # 换列表而不是 clear()：驱动按列表身份跟踪游标
    _GATE.seen = set()
    _GATE.run = None
    _CUR_RUN["id"] = None      # 缓存的 run_id 也必须作废，否则 1 秒内会拿旧 run 去确认
    _CUR_RUN["t"] = 0.0
    if not wait_idle(API_PORT, timeout=60):
        print("    [scene] 警告：上一轮 60s 内未释放（/api/start 会自行重试 409）")
    print(f"\n--- {title} ---")



def sse_events(port, seconds, stop_on=None):
    """后台订阅事件流，返回收集到的事件列表。"""
    import threading
    out = []
    def run():
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/events",
                                     headers={"Accept": "text/event-stream"})
        try:
            with urllib.request.urlopen(req, timeout=seconds + 20) as r:
                buf = ""
                t0 = time.time()
                while time.time() - t0 < seconds:
                    line = r.readline()
                    if not line:
                        break
                    buf += line.decode("utf-8", "replace")
                    while "\n\n" in buf:
                        blk, buf = buf.split("\n\n", 1)
                        ev = re.search(r"^event:\s*(\S+)", blk, re.M)
                        da = re.search(r"^data:\s*(.*)$", blk, re.M)
                        if ev and da:
                            _pair = (ev.group(1), da.group(1).strip())
                            out.append(_pair)
                            # 同时喂给全局闸门驱动：每个场景各开一条订阅，
                            # 驱动是全局唯一的，必须能看到所有场景的闸门事件。
                            # 读 _EVENTS["q"] 而不是固定列表：scene() 会整体换队列，
                            # 绑死旧列表就等于把上一场景的残留继续灌进来。
                            _EVENTS["q"].append(_pair)
                            if stop_on and ev.group(1) == stop_on:
                                return
        except Exception:
            pass
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return out, t


def wait_idle(port, timeout=30):
    """等后端把 pipeline_running 复位 —— UI 收到 done 就认为结束，
    若此时 /api/start 还返回 409，用户会遇到「界面说完成、按钮点不动」。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if not get(f"http://127.0.0.1:{port}/api/status")["pipeline_running"]:
                return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


class GateControl:
    """全局闸门控制状态（单线程访问，无需加锁）。"""

    def __init__(self):
        self.enabled = True          # 总开关
        self.suppress = ()           # 本轮不自动确认的闸门名（场景七用）
        self.seen = set()            # 已处理过的闸门键
        self.run = None              # 当前轮次的 run_id
        self.failed = []             # 确认重试 3 次仍失败的闸门（计入 FAIL）


_GATE = GateControl()
_CUR_RUN = {"id": None, "t": 0.0}


def _cur_run(port):
    """当前正在跑的 run_id（缓存 1 秒，避免每个事件都打一次 HTTP）。"""
    t = time.time()
    if t - _CUR_RUN["t"] < 1.0:
        return _CUR_RUN["id"]
    try:
        rid = get(f"http://127.0.0.1:{port}/api/status", timeout=5).get("run_id")
    except Exception:
        rid = _CUR_RUN["id"]
    _CUR_RUN.update({"id": rid, "t": t})
    return rid


def _post_gate(port, path, body, ev):
    """闸门确认 POST：慢响应/瞬时 5xx 重试 3 次，仍失败记入 _GATE.failed（计入 FAIL），
    不再「吞掉异常然后伪装成流水线卡死」。409（已确认过）仍视为正常。"""
    last = None
    for attempt in range(3):
        try:
            return post(f"http://127.0.0.1:{port}{path}", body)
        except Exception as e:
            last = e
            if "409" in str(e):
                return
            time.sleep(0.5 * (attempt + 1))
    print(f"    [gate] {ev} 确认 3 次仍失败，计入 FAIL: {type(last).__name__} {last}")
    _GATE.failed.append(ev)


def gate_driver(port, enabled=True):
    """唯一的闸门驱动：像真实用户一样把所有人工闸门点掉。

    两条信号并行，任意一条先到就确认，天然免疫漏事件：
      A. 事件流：闸门事件正是在闸门弹出瞬间发出的，时序最准。
         但 SSE 的 payload 里没有 run_id，因此 run_id 一律现取当前 run ——
         本驱动全局只有一个，同一时刻只可能有一个 run 在跑，不存在串台。
      B. /api/status 轮询：兜底「订阅刚建立、闸门事件被漏掉」的情况。
         注意它的 current_phase 来自定期落盘的 progress.json，**在闸门处会滞后**，
         因此不能只靠它判断蓝图门。

    run_id 变化即视为进入新一轮，清空去重状态 —— 必须在处理事件流**之前**完成，
    否则新一轮的闸门事件会拿上一轮的记录比对而被跳过。

    队列每次循环重新取（_EVENTS["q"]）：scene() 会在场景边界整体换掉它，
    游标按「列表身份」走 —— 换了列表就从 0 重新开始，于是上一场景迟到的事件
    既不会被当成当前场景去确认，也不会把驱动卡死
    （对同一个列表 clear() 才是会卡死的那种写法：长度清零而游标还在旧位置）。
    """
    t0 = time.time()
    _resume_ev = {"split": "split_ready", "fix": "fix_ready",
                  "accept": "accept_ready", "qc": "qc_failed"}
    gates = set(_resume_ev.values()) | {"blueprint_ready"}
    q = None        # 游标跟着「列表身份」走：scene() 换列表时自然从 0 开始，
    cursor = 0      # 也就不会拿旧列表的长度把新队列当成「已经超过」而永久停住
    while time.time() - t0 < 900:
        if _EVENTS["q"] is not q:
            q = _EVENTS["q"]
            cursor = 0
        events = q
        # ---- 先取一次状态，顺带做 run 切换检测 ----
        # 顺序很关键：run 切换必须在处理事件流**之前**完成去重清理。
        # 否则新一轮的 blueprint_ready 会拿上一轮的记录去比对而被跳过，
        # 随后才清空 —— 流水线永远卡在 phase2_waiting。
        st = None
        cur = None
        if enabled and _GATE.enabled:
            try:
                st = get(f"http://127.0.0.1:{port}/api/status", timeout=5)
                cur = st.get("run_id")
                if cur != _GATE.run:
                    _GATE.run = cur
                    _GATE.seen = set()
            except Exception as e:
                print(f"    [gate] 状态轮询异常（忽略）: {type(e).__name__} {e}")

        # ---- A. 事件流 ----
        while cursor < len(events):
            ev, data = events[cursor]; cursor += 1
            if ev not in gates:
                continue
            try:
                d = json.loads(data) if isinstance(data, (str, bytes)) else data
                if not isinstance(d, dict):
                    d = {}
            except Exception:
                d = {}
            if ev in _GATE.suppress:
                continue
            # 这里**不能去重**。idx 会一直前进，每个事件本来就只处理一次，
            # 去重检查唯一的实际作用是「跳过」—— 而一旦跳过就是永久丢失：
            # /api/status 的 run_id 来自定期落盘的 progress.json，会滞后于
            # 流水线真实状态，于是新 run 的闸门事件可能正好落在「状态还没显示
            # 换 run」的窗口里被跳过，随后 seen 被清空也救不回来。
            # 症状是流水线永久卡在 phase2_waiting，且无任何报错。
            #
            # 去掉去重是安全的：闸门一旦确认，后续确认请求会被端点的阶段校验
            # 挡下返回 409，而 _drive_gate 对 409 直接忽略。
            if enabled and _GATE.enabled:
                _drive_gate(port, ev, d)

        # ---- B. 状态轮询兜底（订阅漏事件时用） ----
        if st is not None:
            try:
                r = st.get("resume")
                if isinstance(r, dict) and r.get("key"):
                    ev = _resume_ev.get(r["key"])
                    key = (ev, r.get("batch_id")) if ev == "accept_ready" else ev
                    if ev and ev not in _GATE.suppress and key not in _GATE.seen:
                        _GATE.seen.add(key)
                        _drive_gate(port, ev, {**r, "run_id": cur})
                elif st.get("current_phase") == "phase2_waiting" and st.get("blueprint"):
                    if "blueprint_ready" not in _GATE.suppress \
                            and "blueprint_ready" not in _GATE.seen:
                        _GATE.seen.add("blueprint_ready")
                        _drive_gate(port, "blueprint_ready",
                                    {"blueprint": st["blueprint"], "run_id": cur})
            except Exception as e:
                print(f"    [gate] 状态轮询异常（忽略）: {type(e).__name__} {e}")
        time.sleep(0.2)


def start_run(payload, tries=120):
    """发起一次运行，遇到 409（上一轮尚未完全释放）就重试。

    闸门驱动是全局的，这里不再逐场景起线程 —— 多个驱动会互相确认对方的闸门。
    tries×0.5s=60s：慢速 CI runner 上一次 finalize 落盘可能就要好几秒。
    """
    last = None
    for _ in range(tries):
        try:
            return post(f"{B}/api/start", payload)
        except RuntimeError as e:
            last = e
            if "409" in str(e):
                time.sleep(0.5)
                continue
            raise
    raise RuntimeError(f"start 反复 409：{last}")


def _drive_gate(port, ev, d):
    """按事件类型提交对应闸门确认。异常只打印不抛出（闸门已被别处确认时属正常）。

    刻意**不带 run_id**：_check_run_id 只在「传了且不匹配」时才拒绝，而
    /api/status 的 run_id 在 run 起始的接管窗口内会滞后于 pipeline 的真实 run，
    带着滞后的 id 去确认会被 409 静默吞掉，流水线就永远卡在闸门上（实测复现）。
    后端同一时刻只可能有一个 run，交给它用 state.current_run_id 判定即可。
    """
    try:
        if ev == "split_ready":
            _post_gate(port, "/api/split/confirm", {}, ev)
            print("    [gate] 已确认拆书")
        elif ev == "blueprint_ready":
            bp = d.get("blueprint") or ""
            if len(bp) < 100:
                bp = "（测试桩：蓝图正文过短时补齐，确认接口要求 >=100 字）" + bp
            _post_gate(port, "/api/blueprint/confirm", {"blueprint": bp}, ev)
            print("    [gate] 已确认蓝图")
        elif ev == "fix_ready":
            _post_gate(port, "/api/fix/confirm",
                       {"action": "accept", "gaps": []}, ev)
            print("    [gate] 已确认诊断")
        elif ev == "accept_ready":
            # 端点是 /api/accept（不是 /api/accept/confirm）；批次取自 progress.json
            # 的 accept_pending_batch，因此无需在此处传 batch_id。
            _post_gate(port, "/api/accept", {}, ev)
            print(f"    [gate] 已验收批次 {d.get('batch_id')}")
        elif ev == "qc_failed":
            _post_gate(port, "/api/export/force", {}, ev)
            print("    [gate] 已强制导出")
    except Exception as e:
        # 409（已被别处确认）仍视为正常；其余异常已由 _post_gate 记入 _GATE.failed。
        if "409" in str(e):
            return
        print(f"    [gate] {ev} 确认异常: {type(e).__name__} {e}")


def make_novel(path: Path, chapters: int, paras: int = 3):
    path.mkdir(parents=True, exist_ok=True)
    cn = "零一二三四五六七八九十"
    for i in range(1, chapters + 1):
        name = cn[i] if i <= 10 else f"{i}"
        body = []
        for p in range(paras):
            body.append(f"第{name}章 第{p+1}段。这是一个用于端到端测试的段落，"
                        f"包含足够长度的中文内容以便触发分批逻辑。" + "填充文本。" * 20)
        (path / f"第{name}章.txt").write_text("\n\n".join(body), encoding="utf-8")


def is_terminal_event(event, data):
    """这条 SSE 事件是否代表「本轮结束」。

    产品把**可继续的告警**也发成 `event: error`（payload 里 `kind: "warning"`，
    如缝合跳过 / 断层 JSON 缺失 / 拆章兜底）。把它们当终态会让 wait_done 在
    流水线还在跑的时候就返回，后续断言便与 finalize 抢跑 —— 场景十会持续发
    「缝合跳过」告警，v0.13.0 首发的 CI 上就是这样在质量报告写盘前拿到 404，
    而且只有部分轮次撞上，看起来像随机失败。
    """
    if event in ("done", "stopped"):
        return True
    if event != "error":
        return False
    try:
        payload = json.loads(data)
    except (TypeError, ValueError):
        return True
    return not (isinstance(payload, dict) and payload.get("kind") == "warning")


# 自检：这条谓词判错曾在 CI 上伪装成「随机丢报告」，改坏时必须立刻炸（脚本形态
# 的用例被 pytest 收集不到，所以把断言放在这里）。
assert is_terminal_event("error", json.dumps({"kind": "unknown", "code": "exception"}))
assert is_terminal_event("done", "{}") and is_terminal_event("stopped", "{}")
assert not is_terminal_event(
    "error", json.dumps({"kind": "warning", "code": "stitch_skipped"}))
assert not is_terminal_event("progress", "{}")


def wait_done(events, t, timeout=240, api_port=None):
    """等终态。返回 (是否到达, 终态瞬间的 pipeline_running 快照)。

    必须在 done 出现的**同一刻**采样 pipeline_running：
    UI 收到 done 就认为结束，若此刻后端仍报 running，用户会撞上 409。

    兜底：SSE 订阅可能没改绑到本 run 而漏掉 done（慢速 runner 上出现过）。
    此时以**磁盘状态**为准：current_phase == phase4 且 pipeline_running 已复位
    即视为跑完 —— 与真实前端靠 /api/status 恢复状态是同一条路径。

    两个必须的前置条件，否则会把「还没开始跑」误判成「已跑完」：
      1. 5s 宽限期（避开刚 start 时读到上一轮残留状态）；
      2. 期间必须**真的观测到** pipeline_running=True —— 否则新 run 刚被接受、
         progress.json 还停留在上一轮的 phase4 时就会误判完成。
    """
    t0 = time.time()
    saw_running = False
    while time.time() - t0 < timeout:
        if any(is_terminal_event(e, d) for e, d in events):
            snap = None
            if api_port:
                try:
                    snap = get(f"http://127.0.0.1:{api_port}/api/status")["pipeline_running"]
                except Exception:
                    snap = None
            return True, snap
        if api_port:
            try:
                st = get(f"http://127.0.0.1:{api_port}/api/status")
                if st.get("pipeline_running"):
                    saw_running = True
                if saw_running and time.time() - t0 > 5 \
                        and st.get("current_phase") == "phase4" and not st.get("pipeline_running"):
                    return True, False
            except Exception:
                pass
        time.sleep(0.1)
    return False, None


llm = subprocess.Popen([PY, str(BACKEND / "mock_llm.py"), str(LLM_PORT)],
                       cwd=str(BACKEND), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
api = subprocess.Popen([PY, "-m", "uvicorn", "app:app", "--host", "127.0.0.1",
                        "--port", str(API_PORT), "--log-level", "warning"],
                       cwd=str(BACKEND), env={**os.environ, "TEXTFORGE_DEV": "1"},
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
tmp = Path(tempfile.mkdtemp(prefix="tf_e2e_"))
try:
    B = f"http://127.0.0.1:{API_PORT}"
    L = f"http://127.0.0.1:{LLM_PORT}"
    # 等两个服务就绪：固定 sleep 在慢机器/端口 TIME_WAIT 时会不够，
    # 导致健康检查连接被拒（整轮全 FAIL）。改成轮询重试。
    _up = False
    for _ in range(60):
        try:
            get(f"{B}/api/health", timeout=3)
            get(f"{L}/v1/models", timeout=3)
            _up = True
            break
        except Exception:
            time.sleep(0.5)
    if not _up:
        # stderr 已 DEVNULL（避免 Windows 匿名管道缓冲写满堵死被检进程），
        # 排查线索统一到 backend.log。
        log_tail = ""
        try:
            log_tail = (BACKEND / "data" / "logs" / "backend.log").read_text(encoding="utf-8", errors="replace")[-1500:]
        except OSError:
            pass
        raise RuntimeError(f"后端/mock 未就绪。backend.log 尾部:\n{log_tail}")
    # 所有场景共用一个事件队列（_EVENTS["q"]），全局驱动据此按闸门事件行动；
    # 队列在场景边界被 scene() 整体换掉，上一场景的残留不会灌进下一场景。
    print("mock / health:", get(f"{L}/v1/models")["data"][0]["id"], "|", get(f"{B}/api/health")["status"])
    # 全局唯一的闸门驱动：像真实用户那样把所有人工闸门都点掉。
    # 队列放在 _EVENTS 里，由 scene() 在场景边界整体换掉（见 scene 的说明）。
    threading.Thread(target=gate_driver, args=(API_PORT,), daemon=True).start()

    # ---------- 准备输入 ----------
    inp = tmp / "in"; outd = tmp / "out"
    make_novel(inp, chapters=6)
    src_files = sorted(p.name for p in inp.glob("*.txt"))
    print("输入章节:", src_files)

    events, th = sse_events(API_PORT, 240, stop_on="done")

    body = {
        "input_mode": "multi_file", "input_path": str(inp), "output_path": str(outd),
        "input_format": "txt", "api_url": f"{L}/v1/chat/completions", "api_key": "",
        "model": "mock-model", "context_window": 8000,
        "author_style": "冷峻克制", "novel_name": "端到端测试书",
        "refactor_mode": "full_rewrite", "review_split": False,
        # 逐章验收门 + 质检门都打开：无人值守跑通必须把这两道门也走一遍
        "refactor_gates": {"require_chapter_accept": True, "qc_block_export": True,
                           "auto_rewrite_bad_batch": False},
        "forbidden_canon": [], "name_map": {},
        "compare_output": True, "chapter_snapshots": True,
        "diagnose_json": None, "proxy": None, "rich_text": False,
        "temperatures": {"diagnose": 0.3, "blueprint": 0.5, "refactor": 0.8, "stitch": 0.6},
        "batch_interval_sec": 0, "models": {},
    }
    r = post(f"{B}/api/start", body)
    print("start ->", r["status"], r["run_id"][:8])
    check("/api/start 返回 run_id", bool(r.get("run_id")))

    ok, snap_running = wait_done(events, th, api_port=API_PORT)
    idle = wait_idle(API_PORT)
    names = [e for e, _ in events]
    if not ok:
        print("事件序列:", names[-25:])
        err = [d for e, d in events if e == "error"]
        print("错误:", err[-1][:300] if err else "（无 error 事件）")
    check("流水线跑完并发出 done", ok, f"事件数={len(events)} 末尾={names[-6:]}")
    check("发出 done 后后端立即变为空闲（不会出现 409 窗口）", idle)
    # 关键产品不变式：UI 收到 done 的那一刻，/api/start 必须已经能接受新任务
    try:
        probe = dict(body, output_path=str(tmp / "probe_out"), input_path=str(inp))
        post(f"{B}/api/start", probe)
        post(f"{B}/api/stop", {"run_id": None})
        check("done 之后可立即发起新任务（无 pipeline_already_running）", True)
    except urllib.error.HTTPError as e:
        check("done 之后可立即发起新任务（无 pipeline_already_running）", False, f"HTTP {e.code}")
    check("收到 run_start 事件", "run_start" in names)
    check("收到 batch_done 事件", "batch_done" in names)
    check("收到 done 事件", "done" in names)
    check("未收到 error 事件", not any(e == "error" for e in names),
          str([d for e, d in events if e == "error"][:1]))
    # 注：phase0 的 phase_done 发生在 SSE 订阅建立之前（run 刚起、生成器还没绑上），
    # 后端只对 run_start 做重放。这是有意为之 —— 阶段/章数状态由 /api/status 兜底，
    # 下面用 status 断言，而不是依赖事件。

    # ---------- 产物结构 ----------
    check("progress.json 存在", (outd / "progress.json").exists())
    check("Story_Bible.md 存在", (outd / "Story_Bible.md").exists())
    check("01_summaries 有摘要", len(list((outd / "01_summaries").glob("batch_*.txt"))) > 0)
    recon = sorted((outd / "02_workspace" / "reconstructed").glob("chapter_*.txt"))
    check("reconstructed 有全部 6 章", len(recon) == 6, f"实际 {len(recon)}: {[p.name for p in recon]}")
    split_files = sorted((outd / "02_workspace" / "split").glob("*.txt"))
    check("split 有全部 6 章", len(split_files) == 6, f"实际 {len(split_files)}")

    # ---------- 成品目录（交付物必须干净）----------
    final = outd / "03_final"
    outs = sorted(final.glob("*")) if final.is_dir() else []
    chapters = [p for p in outs if p.is_file() and p.name.startswith("重构_")]
    merged = [p for p in outs if p.is_file() and p.name.endswith("_重构终稿.txt")]
    check("03_final 有 6 个逐章成品", len(chapters) == 6, f"实际 {len(chapters)}: {[p.name for p in outs]}")
    check("03_final 另有整书合并稿", len(merged) == 1, str([p.name for p in merged]))
    check("交付目录里没有混入缝合报告（已移到 _diagnostics）",
          not any("缝合" in p.name for p in outs), str([p.name for p in outs]))
    check("_diagnostics 里有缝合报告",
          (outd / "_diagnostics" / "缝合补丁报告.json").exists())
    _anchors_path = outd / "_diagnostics" / "stitch_anchors.jsonl"
    _anchor_lines = (_anchors_path.read_text(encoding="utf-8").splitlines()
                     if _anchors_path.exists() else [])
    check("缝合锚点完整落盘（含原文/替换文，不是只有哈希引用）",
          bool(_anchor_lines)
          and all({"original", "replacement"} <= set(json.loads(x)) for x in _anchor_lines),
          f"{len(_anchor_lines)} 条")
    # 改动率观测：progress.json 里的轻量引用必须带 change_rate/chars，且 applied 为真
    # （applied 恒为 False 说明记录又跑到了应用之前，质量报告会因此永远量不到东西）
    _pa = json.loads((outd / "progress.json").read_text(encoding="utf-8")).get(
        "stitch_anchors", [])
    check("缝合锚点带改动率与覆盖字数",
          bool(_pa) and all("change_rate" in a and isinstance(a.get("chars"), int)
                            for a in _pa), str(_pa[:1]))
    check("已应用的缝合锚点 applied=true",
          any(a.get("applied") for a in _pa), str([a.get("applied") for a in _pa][:8]))
    qpath = outd / "_diagnostics" / "quality_report.json"
    check("_diagnostics 里有质量报告", qpath.exists())
    quality = json.loads(qpath.read_text(encoding="utf-8")) if qpath.exists() else {}
    check("质量报告覆盖全部章节",
          quality.get("summary", {}).get("chapters") == 6
          and quality.get("status") in ("good", "warn", "bad"),
          str(quality.get("summary")))
    _st = quality.get("stitch", {})
    check("质量报告汇总了缝合观测（数量与 progress 里已应用的锚点一致）",
          _st.get("applied_patches") == sum(1 for a in _pa if a.get("applied"))
          and _st.get("exposed_chars", 0) > 0, str(_st)[:200])
    qr, qr_err = quality_report(B, outd)
    check("/api/output/quality 返回同一份报告",
          qr.get("summary", {}).get("chapters") == 6, qr_err or str(qr.get("summary")))
    inspect = get(f"{B}/api/output/inspect?output_path={urllib.request.quote(str(outd))}")
    check("inspect 带出质量状态", inspect.get("quality", {}).get("status") == quality.get("status"))
    check("03_final 只有交付物（无非 txt/md/docx 文件）",
          all(p.suffix.lower() in (".txt", ".md", ".docx") for p in outs if p.is_file()),
          str([p.name for p in outs]))
    order_key = lambda n: int(re.sub(r"\D", "", n) or 0)
    check("逐章成品按章号排序",
          [order_key(p.name) for p in chapters] == sorted(order_key(p.name) for p in chapters),
          str([p.name for p in chapters]))

    # /api/final_files 不能把诊断产物当成交付物列出来
    ff = get(f"{B}/api/final_files?output_path={urllib.request.quote(str(outd))}")["files"]
    check("final_files 只列交付物", all("缝合" not in f["name"] for f in ff), str(ff))
    check("final_files 非空", len(ff) >= 6, str(len(ff)))

    from docx import Document

    def read_out(p: Path) -> tuple:
        """读成品内容。docx 走 python-docx，其余按纯文本。"""
        if p.suffix.lower() == ".docx":
            d = Document(str(p))
            return "\n".join(par.text for par in d.paragraphs), len(d.paragraphs)
        return p.read_text(encoding="utf-8", errors="replace"), \
            len([x for x in p.read_text(encoding="utf-8", errors="replace").split("\n\n") if x.strip()])

    per_chapter, total_chars = [], 0
    for p in chapters:
        txt, np_ = read_out(p)
        per_chapter.append((p.name, len(txt), np_))
        total_chars += len(txt)
    for name, ln, np_ in per_chapter:
        print(f"    {name}: {ln} 字 / {np_} 段")
    check("每章成品都有正文（无空章）", all(ln > 0 for _, ln, _ in per_chapter), str(per_chapter))
    check("每章成品都有段落（document.py 没把内容吞掉）", all(np_ > 0 for _, _, np_ in per_chapter), str(per_chapter))
    check("成品总字数合理", total_chars > 200, str(total_chars))

    # 成品里必须含 mock 生成的重构标记，且**不能**残留机器分隔符
    body_txt = ""
    for p in chapters:
        body_txt += read_out(p)[0]
    check("成品包含模型产出的重构内容", "【重构第1段·其一】" in body_txt)
    check("成品不含机器分隔符 ===CHAPTER_BREAK===", "===CHAPTER_BREAK===" not in body_txt)
    check("成品不含 prompt 泄漏（待处理正文/全局设定）",
          "【待处理正文】" not in body_txt and "【全局设定与重构方向】" not in body_txt)
    check("成品不含模型寒暄", not body_txt.lstrip().startswith("好的"), body_txt[:60])
    # 六章各自都有独立的重构段，不能因为分段错位而全塞进第一章
    per_chapter_marks = []
    for p in chapters:
        t = read_out(p)[0]
        per_chapter_marks.append(sum(1 for i in range(1, 7) if f"【重构第{i}段·其一】" in t))
    check("每章恰好拿到自己那一段（无错位、无重复）",
          all(n == 1 for n in per_chapter_marks), str(per_chapter_marks))

    # ---------- mock 调用分布 ----------
    stats = put(f"{L}/", {"reset": False})["stats"]
    print("mock 调用统计:", stats)
    check("确实调用了 LLM", stats["total"] > 0)
    check("四个阶段都被调用到（诊断/蓝图/重构/缝合）",
          stats["diagnose"] > 0 and stats["blueprint"] > 0 and stats["refactor"] > 0, str(stats))

    # ---------- progress.json 一致性 ----------
    prog = json.loads((outd / "progress.json").read_text(encoding="utf-8"))
    check("current_phase 已推进到 phase4", prog.get("current_phase") == "phase4",
          str(prog.get("current_phase")))
    check("pipeline_running 已复位", prog.get("pipeline_running") is False)
    check("usage 已累计", prog.get("usage", {}).get("completion_tokens", 0) > 0, str(prog.get("usage")))
    check("chapters 数与输入一致", len(prog.get("chapters", [])) == 6, str(len(prog.get("chapters", []))))
    check("每个批次都是 phase3_done",
          all(b["status"] == "phase3_done" for b in prog.get("batches", [])), str(prog.get("batches")))

    # ---------- /api/status 与产物一致 ----------
    st = get(f"{B}/api/status?output_path={urllib.request.quote(str(outd))}")
    check("status.total_chapters == 6", st.get("total_chapters") == 6, str(st.get("total_chapters")))
    check("status 显示已完成", st.get("pipeline_running") is False)
    check("status.reconstructed_chapters == 6", st.get("reconstructed_chapters") == 6,
          str(st.get("reconstructed_chapters")))

    # ---------- 场景二：docx 输入，覆盖 document.py 的排版路径 ----------
    scene("场景二：docx 输入")
    from docx import Document as _D
    dinp = tmp / "in_docx"; dinp.mkdir(parents=True, exist_ok=True)
    cn = "零一二三四五六七八九十"
    for i in (1, 2, 3):
        d = _D()
        for p in range(3):
            d.add_paragraph(f"第{cn[i]}章 第{p+1}段。用于验证 docx 成品的排版。" + "填充。" * 40)
        d.save(str(dinp / f"第{cn[i]}章.docx"))
    doutd = tmp / "out_docx"
    ev2, th2 = sse_events(API_PORT, 240, stop_on="done")
    b2 = dict(body, input_path=str(dinp), output_path=str(doutd), input_format="docx",
              novel_name="docx测试", refactor_gates=NO_AUTO_REWRITE, compare_output=False,
              chapter_snapshots=False)
    r2 = post(f"{B}/api/start", b2)
    print(f"  第二次 start -> {r2['status']}（刻意在上一轮 done 后立即发起）")
    ok2, snap2 = wait_done(ev2, th2, api_port=API_PORT)
    wait_idle(API_PORT)
    # 闸门竞态回归：闸门 Event 必须在推送弹窗**之前** clear，
    # 否则「推送 → 用户确认 → clear」的顺序会吞掉确认，第二次运行必然卡在蓝图门。
    check("连续两次运行都能通过蓝图门（闸门无竞态）",
          ok2, str([e for e, _ in ev2][-6:]))
    check("第二次运行时 done 到达即空闲", snap2 is False, f"snapshot={snap2}")
    names2 = [e for e, _ in ev2]
    if not ok2:
        errs2 = [d for e, d in ev2 if e == "error"]
        print("  错误:", errs2[-1][:250] if errs2 else names2[-8:])
    check("docx 场景流水线跑通", ok2 and "done" in names2, str(names2[-6:]))
    douts = sorted(p for p in (doutd / "03_final").glob("重构_*.docx")) if (doutd / "03_final").is_dir() else []
    check("docx 场景产出 3 个 docx 成品", len(douts) == 3, str([p.name for p in douts]))
    for p in douts:
        dd = _D(str(p))
        texts = [par.text for par in dd.paragraphs]
        # document.py 的修复点：跳过全空段 + 段尾不拖空行
        check(f"docx {p.name} 无空段落", all(t.strip() for t in texts),
              str([t[:20] for t in texts if not t.strip()][:3]))
        check(f"docx {p.name} 无段尾多余换行",
              not any(t.endswith("\n") for t in texts),
              str([t[-12:] for t in texts if t.endswith("\n")][:2]))
        joined = "\n".join(texts)
        check(f"docx {p.name} 含本章重构内容", "【重构第" in joined)
        check(f"docx {p.name} 含段落换行", any("\n" in t for t in texts), str(texts[:2])[:120])
    # 章与章之间用 \n\n---\n\n 连接，不能粘连成一段
    if douts:
        dd = _D(str(douts[0]))
        check("docx 成品保留原章节段落结构", len(dd.paragraphs) >= 3,
              f"段落数={len(dd.paragraphs)}")

    # ---------- 场景三：模型多打分隔符，验证重试能自我纠正 ----------
    # 对应用户实际遇到的报错「需要 3 个，实际 5 个」。
    # 注入前重试也是**完全相同的 prompt**，模型无从收敛；修复后 on_retry
    # 把错因喂回去，第二次应当正确收敛。
    scene("场景三：分隔符数量不符 → 重试纠正")
    put(f"{L}/", {"fail": {"phase": None, "times": 0, "status": 500, "body": "", "extra_breaks": 1}})
    put(f"{L}/", {"reset": True, "log_clear": True})
    e3in = tmp / "in3"; make_novel(e3in, chapters=4)
    o3 = tmp / "out3"
    ev3, th3 = sse_events(API_PORT, 240, stop_on="done")
    r3 = post(f"{B}/api/start", dict(body, input_path=str(e3in), output_path=str(o3),
                                      novel_name="分隔符纠正", refactor_gates=NO_AUTO_REWRITE,
                                      compare_output=False, chapter_snapshots=False))
    ok3 = wait_done(ev3, th3, api_port=API_PORT)[0]
    st3 = put(f"{L}/", {})["stats"]
    for _e in put(f"{L}/", {})["log"]:
        if _e["phase"] == "refactor":
            print(f"    mock 重构调用: 消息数={_e['n_msgs']}（1=首发，2=带纠正的重试） 已收到纠正提示={_e['has_fix']}")
    if not ok3:
        errs3 = [d for e, d in ev3 if e == "error"]
        print("  错误:", errs3[-1][:200] if errs3 else [e for e, _ in ev3][-6:])
    check("多打分隔符时流水线仍能跑完（重试自我纠正）", ok3, str(st3))
    # 4 章 → 正常需要 3 次调用（1 次错 + 1 次纠正成功）；若重试是盲的就会更多或直接失败
    check("模型被调用了不止一次（说明确实发生了重试）", st3["refactor"] >= 2, str(st3))
    o3c = sorted((o3 / "02_workspace" / "reconstructed").glob("chapter_*.txt")) if (o3 / "02_workspace" / "reconstructed").is_dir() else []
    check("4 章全部产出且逐章独立", len(o3c) == 4, str([p.name for p in o3c]))
    if o3c:
        marks = []
        for p in o3c:
            t = p.read_text(encoding="utf-8", errors="replace")
            marks.append(sum(1 for i in range(1, 5) if f"【重构第{i}段·其一】" in t))
        check("纠正后每章恰好一段（无错位/无重复）", all(n == 1 for n in marks), str(marks))
        check("纠正后成品不含分隔符残留",
              all(BREAK_MARK not in p.read_text(encoding="utf-8", errors="replace") for p in o3c))
    put(f"{L}/", {"fail": {"phase": None, "times": 0, "status": 500, "body": "", "extra_breaks": 0}})

    # ---------- 场景四：上下文超限 → 自动拆批跑完 ----------
    # 对应用户真实报错：程序以为放得下（context_window 填得大），本地模型其实只有
    # 8448。要求：不许整轮报废，必须自动对半拆批直到放得下。
    scene("场景四：上下文超限 → 自动拆批")
    put(f"{L}/", {"fail": {"phase": None, "times": 0, "status": 500, "body": "", "extra_breaks": 0}})
    # 让 mock 只接受 ≤2 章的请求，而应用以为窗口很大（context_window=32768 → 一批全塞）
    put(f"{L}/", {"reset": True, "log_clear": True, "ctx_limit": {"max_chapters": 2}})
    e4in = tmp / "in4"; make_novel(e4in, chapters=6)
    o4 = tmp / "out4"
    ev4, th4 = sse_events(API_PORT, 300, stop_on="done")
    r4 = post(f"{B}/api/start", dict(body, input_path=str(e4in), output_path=str(o4),
                                      context_window=32768, novel_name="上下文超限自愈",
                                      refactor_gates=NO_AUTO_REWRITE, compare_output=False,
                                      chapter_snapshots=False))
    ok4 = wait_done(ev4, th4, api_port=API_PORT)[0]
    st4 = put(f"{L}/", {})["stats"]
    splits = [d for e, d in ev4 if e == "batch_split"]
    # batch_split 只用于即时提示，不在 SSE 重放集合内；慢速 runner 上订阅建立前
    # 事件可能已经发完。拆批是否真的发生以 progress.json 的持久状态为准。
    prog4_path = o4 / "progress.json"
    prog4 = json.loads(prog4_path.read_text(encoding="utf-8")) if prog4_path.exists() else {}
    split_recorded = bool(prog4.get("adaptive_max_chapters")) or len(prog4.get("batches", [])) > 1
    if not ok4:
        errs4 = [d for e, d in ev4 if e == "error"]
        print("  错误:", errs4[-1][:220] if errs4 else [e for e, _ in ev4][-8:])
    print(f"    超限次数={st4.get('ctx_overflow', 0)} 拆批事件={len(splits)} "
          f"批次={len(prog4.get('batches', []))} 自适应上限={prog4.get('adaptive_max_chapters')}")
    check("上下文超限时流水线仍能跑完（自动拆批自愈）", ok4, str(st4))
    check("确实发生了自动拆批", split_recorded, str(prog4.get("batches"))[:160])
    check("服务端确实拒绝过超限请求", st4.get("ctx_overflow", 0) >= 1, str(st4.get("ctx_overflow")))
    o4c = sorted((o4 / "02_workspace" / "reconstructed").glob("chapter_*.txt"))
    check("拆批后 6 章全部产出", len(o4c) == 6, str([p.name for p in o4c]))
    # 拆批事件必须带可读原因与批次号（前端要展示给用户）
    if splits:
        _d = json.loads(splits[0])
        check("拆批事件含原因与新批次号",
              _d.get("reason") == "overflow" and _d.get("new_batches"),
              str(_d)[:160])
    put(f"{L}/", {"reset": True, "ctx_limit": {"max_chapters": 0}})

    # ---------- 场景五：单文件章节识别 + 上下文窗口探测 ----------
    # 一个缩进风格、带序章、混入正文提及「第X章」的单文件，必须精确拆出 4 章 + 序章，
    # 且正文提及不得被误判成章节。
    scene("场景五：单文件章节识别（缩进/序章/正文提及）")
    e5in = tmp / "in5"; e5in.mkdir(parents=True, exist_ok=True)
    single = e5in / "single_input.txt"
    _pad = "填充正文内容，确保每章都超过空章阈值五十个字，避免被判为空章。" * 3
    single.write_text(
        "前言：这是一段序章内容，交代世界观背景，长度足够，不应被丢弃。" + _pad + "\n\n"
        "　　第一章 初入江湖\n正文甲。他翻开第一章，读到第三页便睡着了。" + _pad + "\n\n"
        "  第二章 再入风尘\n正文乙。他又想起第二章 的伏笔。" + _pad + "\n\n"
        "\t第三章 三入人间\n正文丙。" + _pad + "\n\n"
        "第四章 四入庙堂\n正文丁。" + _pad + "\n",
        encoding="utf-8")
    e5in = tmp / "in5"; o5 = tmp / "out5"
    wait_idle(B)                       # 等场景四完全释放，避免 409 静默吞掉启动
    ev5, th5 = sse_events(API_PORT, 240, stop_on="done")
    r5 = post(f"{B}/api/start", dict(body, input_mode="single_file", input_path=str(single),
                                      output_path=str(o5), novel_name="单文件识别",
                                      refactor_gates=NO_AUTO_REWRITE, compare_output=False,
                                      chapter_snapshots=False))
    print("    场景五 run_id:", r5.get("run_id", "?")[:8], "start status:", r5.get("status"))
    ok5 = wait_done(ev5, th5, api_port=API_PORT)[0]
    if not ok5:
        print("    场景五 事件:", [e for e, _ in ev5])
        try:
            print("    场景五 status:", get(f"{B}/api/status"))
        except Exception as _e:
            print("    场景五 status 取用失败:", _e)
    check("单文件输入也能跑完", ok5)
    # 章节数写入 progress：序章 + 4 章 = 5
    st5 = get(f"{B}/api/status")
    check("单文件拆出「序章 + 4 章」", st5.get("total_chapters") == 5,
          f"total_chapters={st5.get('total_chapters')}")
    o5s = sorted((o5 / "02_workspace" / "split").glob("*.txt"))
    check("拆分文件数为 5（含序章）", len(o5s) == 5, str([p.name for p in o5s]))
    # 正文提及「第一章」「第二章」不得各自多切一章
    joined = "".join(p.read_text(encoding="utf-8", errors="replace") for p in o5s)
    check("序章正文未被丢弃", "这是一段序章内容" in joined)

    # 上下文窗口探测：mock 上报 4096，应用配置 32768 → 必须提示「填大了」
    PROBE_N = 4096
    put(f"{L}/", {"context_report": {"n_ctx": PROBE_N}})
    probe = post(f"{B}/api/system/context_probe",
                 {"api_url": f"{L}/v1/chat/completions",
                  "model": "mock-model", "configured": 32768})
    check("探测到服务端真实上下文窗口", probe.get("detected") == PROBE_N, str(probe))
    check("配置大于实测时给出提示", bool(probe.get("mismatch")), str(probe)[:160])

    # ---------- 场景六：无编号标题 → AI 辅助拆章 ----------
    # 一本书全用「纯标题行」分章（没有任何「第X章」），规则必然识别不出。
    # 期望：置信度低 → AI 只在候选行里挑边界 → 仍需人工确认后才继续。
    scene("场景六：无编号标题 → AI 拆章")
    e6in = tmp / "in6"; e6in.mkdir(parents=True, exist_ok=True)
    _p6 = "正文内容，需要足够长以免被判为空章。" * 4
    (e6in / "ai_book.txt").write_text(
        "前言部分，交代背景。" + _p6 + "\n\n"
        "风起于青萍之末\n" + _p6 + "\n\n"
        "山雨欲来\n" + _p6 + "\n\n"
        "剑指长安\n" + _p6 + "\n",
        encoding="utf-8")
    o6 = tmp / "out6"
    ev6, th6 = sse_events(API_PORT, 240, stop_on="done")
    start_run(dict(body, input_mode="single_file", input_path=str(e6in / "ai_book.txt"),
                   output_path=str(o6), novel_name="AI拆章", refactor_gates=NO_AUTO_REWRITE,
                   compare_output=False, chapter_snapshots=False))
    ok6 = wait_done(ev6, th6, api_port=API_PORT)[0]
    st6 = get(f"{B}/api/status")
    # SSE 订阅可能在「新 run 起始的毫秒级窗口」漏掉终态事件；以磁盘状态兜底判定，
    # 否则会偶发把已完成的任务判成失败（假阴性）。
    if not ok6 and not st6.get("pipeline_running") and (o6 / "03_final").is_dir():
        ok6 = True
    if not ok6:
        errs6 = [d for e, d in ev6 if e == "error"]
        print("  错误:", errs6[-1][:240] if errs6 else [e for e, _ in ev6][-8:])
    det6 = st6.get("chapter_detection") or {}
    check("无编号标题的书也能拆章并跑完", ok6, f"total={st6.get('total_chapters')}")
    check("确实走了 AI 拆章（记录在 chapter_detection）", det6.get("mode") == "ai", str(det6))
    check("AI 拆出「序章 + 3 章」", st6.get("total_chapters") == 4,
          f"total_chapters={st6.get('total_chapters')}")
    o6s = sorted((o6 / "02_workspace" / "split").glob("*.txt"))
    joined6 = "".join(p.read_text(encoding="utf-8", errors="replace") for p in o6s)
    check("AI 拆章后正文一字未丢",
          "前言部分" in joined6 and "风起于青萍之末" in joined6 and "剑指长安" in joined6)

    # ---------- 场景七：规则与 AI 都搞不定 → 人工自定义标记重拆 ----------
    # 用「◆ 壹 ◆」这类创新排版，且关掉 AI（ai_split=False），只剩人工兜底。
    scene("场景七：自定义章节标记重拆")
    e7in = tmp / "in7"; e7in.mkdir(parents=True, exist_ok=True)
    _p7 = "正文内容，需要足够长以免被判为空章。" * 4
    (e7in / "odd_book.txt").write_text(
        "开场白。" + _p7 + "\n\n"
        "◆ 壹 ◆\n" + _p7 + "\n\n"
        "◆ 贰 ◆\n" + _p7 + "\n",
        encoding="utf-8")
    o7 = tmp / "out7"
    ev7, th7 = sse_events(API_PORT, 240, stop_on="done")
    # 拆书门由本场景手动处理（先按自定义标记重拆，再确认）。
    # 必须**在 run 起之前**就抑制：若全局驱动抢先确认了拆书预览，
    # 流水线会直接冲进 phase1，重拆断言必然失败。
    _GATE.suppress = {"split_ready"}
    start_run(dict(body, input_mode="single_file", input_path=str(e7in / "odd_book.txt"),
                   output_path=str(o7), novel_name="人工重拆", refactor_gates=NO_AUTO_REWRITE,
                   ai_split=False, compare_output=False, chapter_snapshots=False))

    def _wait_phase(phase, timeout=120):
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                st = get(f"{B}/api/status")
            except Exception:
                time.sleep(0.3); continue
            if st.get("current_phase") == phase:
                return st
            time.sleep(0.3)
        return None

    st7 = _wait_phase("phase0_confirm")
    check("创新排版自动停在拆书预览门等待人工", st7 is not None,
          str((_wait_phase("phase0_confirm", 1) or {}).get("current_phase")))
    rid7 = (st7 or {}).get("run_id")
    r7 = post(f"{B}/api/split/redo", {"run_id": rid7, "markers": ["◆ 壹 ◆", "◆ 贰 ◆"]})
    check("提交自定义章节标记被接受", r7.get("status") == "redoing", str(r7))
    # 重拆在流水线线程内异步执行，轮询等待生效
    ok_redo = False
    for _ in range(150):
        st = get(f"{B}/api/status")
        det = st.get("chapter_detection") or {}
        if det.get("mode") == "markers" and st.get("total_chapters") == 3:
            ok_redo = True
            break
        time.sleep(0.3)
    check("按标记重拆生效（序章 + 2 章）", ok_redo,
          str(get(f"{B}/api/status").get("chapter_detection")))
    # 手动确认拆书后解除抑制，后续闸门交回全局驱动
    post(f"{B}/api/split/confirm", {"run_id": rid7})
    _GATE.suppress = set()
    ok7 = wait_done(ev7, th7, api_port=API_PORT)[0]
    if not ok7:
        errs7 = [d for e, d in ev7 if e == "error"]
        print("  错误:", errs7[-1][:240] if errs7 else [e for e, _ in ev7][-8:])
    st7f = get(f"{B}/api/status")
    check("自定义标记重拆后能跑完", ok7, f"total={st7f.get('total_chapters')}")
    check("最终为「序章 + 2 章」", st7f.get("total_chapters") == 3,
          f"total_chapters={st7f.get('total_chapters')}")
    o7s = sorted((o7 / "02_workspace" / "split").glob("*.txt"))
    joined7 = "".join(p.read_text(encoding="utf-8", errors="replace") for p in o7s)
    check("重拆后正文完整", "开场白" in joined7 and "◆ 壹 ◆" in joined7 and "◆ 贰 ◆" in joined7)

    # ---------- 场景八：中途断裂 → 重跑必须断点续跑（不重做已完成批次）----------
    # 对应用户真实痛点：跑完第一批后因错误断裂，重开却发现前面的成果没了。
    # 这里构造「第一批成功、第二批 500 失败」的现场，再原样重跑，断言：
    #   1) 重跑识别为 resumed
    #   2) 已完成的批次**不再调用模型**（refactor 调用数 == 剩余批次数）
    #   3) 第一批的成品文件仍在，最终整本跑完
    scene("场景八：中途失败 → 断点续跑")
    e8in = tmp / "in8"; make_novel(e8in, chapters=8, paras=5)
    o8 = tmp / "out8"
    put(f"{L}/", {"reset": True, "log_clear": True,
                  # times 必须足够大：流水线对单次 API 失败会**自动重试**，
                  # 只注入一次的话大概率被重试救回来，整轮跑完、error_logs 为空
                  # （实测表现为随机 FAIL）。连打 5 次让重试耗尽，
                  # 批次才一定会真正失败。
                  "fail": {"phase": "refactor", "times": 5, "status": 500,
                           "body": "injected-fail", "skip": 0, "arm_after": 1,
                           "extra_breaks": 0},
                  "ctx_limit": {"max_chapters": 0}})
    ev8, th8 = sse_events(API_PORT, 300, stop_on="done")
    # context_window 给足、书做大，保证被拆成多个批次 —— 场景八要构造
    # 「第一批成功、之后失败」，前提是确实存在第二批可失败。
    # arm_after=1 即「第 1 次 refactor 调用放行，第 2 次起失败」。
    start_run(dict(body, input_path=str(e8in), output_path=str(o8), context_window=8000,
                   novel_name="断点续跑", refactor_gates=NO_AUTO_REWRITE, compare_output=False,
                   chapter_snapshots=False))

    # 第一轮应当因注入的失败而中断。以 progress.json 为准轮询（不依赖可能漏事件的 SSE）。
    prog8 = {}
    for _ in range(480):
        try:
            prog8 = json.loads((o8 / "progress.json").read_text(encoding="utf-8"))
        except Exception:
            time.sleep(0.5); continue
        if prog8.get("error_logs") and not prog8.get("pipeline_running"):
            break
        time.sleep(0.5)
    _mock8a = put(f"{L}/", {})
    st8a = _mock8a["stats"]
    # mock 的请求日志是这一场景唯一的「第 1 次调用到底有没有被打回」证据：
    # 场景八的判定完全建立在 arm_after=1（第 1 次放行、第 2 次起失败）上，
    # 一旦这一前提没成立，后面的 done/files 断言会一起崩，而报错里看不出原因。
    _log8a = [(x.get("phase"), x.get("has_fix")) for x in (_mock8a.get("log") or [])][-8:]
    check("注入的失败发生在第 2 次重构调用之后（第一批未被波及）",
          st8a.get("refactor", 0) >= 2, str(st8a))
    done8 = [b for b in prog8.get("batches", []) if b.get("status") == "phase3_done"]
    recon8a = sorted((o8 / "02_workspace" / "reconstructed").glob("chapter_*.txt"))
    check("第一批成功后因错误中断", bool(prog8.get("error_logs")),
          f"error_logs={len(prog8.get('error_logs') or [])}")
    check("中断时第一批已标记完成且有成品", len(done8) >= 1 and len(recon8a) >= 1,
          "done={0} files={1} statuses={2} errs={3} mock_log={4}".format(
              len(done8), len(recon8a),
              [(b.get("batch_id"), b.get("status")) for b in prog8.get("batches", [])],
              str([(e.get("code"), (e.get("message") or "")[:80])
                   for e in (prog8.get("error_logs") or []) if isinstance(e, dict)][:2])[:260],
              str(_log8a)[:200]))
    check("中断时进度停在 phase3", (prog8.get("current_phase") or "").startswith("phase3"),
          str(prog8.get("current_phase")))
    saved_names = {p.name for p in recon8a}

    # 第二次：清掉故障，原样重跑同一输出目录
    put(f"{L}/", {"reset": True, "log_clear": True,
                  "fail": {"phase": None, "times": 0, "status": 500, "body": "", "skip": 0,
                   "arm_after": 0},
                  "ctx_limit": {"max_chapters": 0}})
    # 先放一份占位成品：续跑收尾时它会被快照成版本 1（见下方「重跑前自动快照」断言）。
    # 不用第三次运行来制造「上一版成品」，见该断言处的说明。
    _seed = o8 / "03_final"
    _seed.mkdir(parents=True, exist_ok=True)
    (_seed / "上一版占位.txt").write_text("这是上一版成品，用于验证覆盖前会被快照。",
                                        encoding="utf-8")
    ev8b, th8b = sse_events(API_PORT, 300, stop_on="done")
    start_run(dict(body, input_path=str(e8in), output_path=str(o8), context_window=8000,
                   novel_name="断点续跑", refactor_gates=NO_AUTO_REWRITE, compare_output=False,
                   chapter_snapshots=False))
    ok8 = wait_done(ev8b, th8b, api_port=API_PORT)[0]
    st8b = put(f"{L}/", {})["stats"]
    prog8b = json.loads((o8 / "progress.json").read_text(encoding="utf-8"))
    check("重跑被识别为断点续跑", prog8b.get("resumed") is True, str(prog8b.get("resumed")))
    # 断言「没有重做」要比**整本书的总批次**少，而不是写死一个魔法数字：
    # 批次数会随 context_window 与自愈拆批变化，写死会在参数调整后悄悄失效。
    n_batches = len(prog8b.get("batches") or [])
    check("续跑没有再重做已完成批次（refactor 调用数少于总批次）",
          n_batches > 1 and st8b.get("refactor", 99) < n_batches,
          f"refactor={st8b.get('refactor')} 总批次={n_batches}")
    # 第一批成品必须在续跑后仍在（内容完整）。
    # 注意：不能断言字节完全不变 —— 缝合阶段本来就会改写**批次边界章**的尾/首，
    # 那是流水线的正常行为，不是「重做」。用「文件仍在且正文标记未丢」来证明未丢内容。
    after = {p.name: p.read_text(encoding="utf-8", errors="replace")
             for p in (o8 / "02_workspace" / "reconstructed").glob("chapter_*.txt")}
    check("中断前已生成的成品在续跑后仍完整保留",
          all(n in after and after[n].strip() and "【重构" in after[n] for n in saved_names),
          f"before={sorted(saved_names)} after={sorted(after)}")
    check("续跑后整本跑完", ok8 and (o8 / "03_final").is_dir(), str([e for e, _ in ev8b][-3:]))
    final8 = list((o8 / "03_final").glob("*")) if (o8 / "03_final").is_dir() else []
    check("续跑后有完整成品", len(final8) >= 1, str([p.name for p in final8]))

    # ---------- 场景九：并发波前 + 跨批连续性 + 硬伤自动重写 ----------
    scene("场景九：并发重构 + 跨批连续性 + 硬伤自动重写")
    put(f"{L}/", {"reset": True, "log_clear": True,
                  "fail": {"phase": None, "times": 0, "status": 500, "body": "",
                           "skip": 0, "arm_after": 0, "extra_breaks": 0},
                  "ctx_limit": {"max_chapters": 0},
                  "bad_content": {"times": 1}})
    e9in = tmp / "in9"; make_novel(e9in, chapters=8)
    o9 = tmp / "out9"
    ev9, th9 = sse_events(API_PORT, 300, stop_on="done")
    start_run(dict(body, input_path=str(e9in), output_path=str(o9), context_window=8000,
                   novel_name="并发连续性与自动重写", refactor_gates={},
                   compare_output=False, chapter_snapshots=False, max_concurrency=2))
    ok9 = wait_done(ev9, th9, api_port=API_PORT)[0]
    st9 = put(f"{L}/", {})["stats"]
    prog9 = json.loads((o9 / "progress.json").read_text(encoding="utf-8"))
    check("并发 + 连续性 + 自动重写场景跑完", ok9, str(st9))
    check("跨批连续性已注入提示词", st9.get("continuity_injected", 0) >= 1, str(st9))
    check("前情摘要已注入提示词", st9.get("story_memo_injected", 0) >= 1, str(st9))
    check("硬伤批次触发了自动重写", bool(prog9.get("auto_rewrite_log")),
          str(prog9.get("auto_rewrite_log"))[:200])
    _bad9 = [i for issues in (prog9.get("batch_qc") or {}).values() for i in issues
             if i.get("severity") == "bad"]
    check("自动重写后没有遗留硬伤", not _bad9, str(_bad9)[:200])
    check("并发度写入进度", prog9.get("max_concurrency") == 2, str(prog9.get("max_concurrency")))
    o9c = sorted((o9 / "02_workspace" / "reconstructed").glob("chapter_*.txt"))
    check("并发场景 8 章全部产出", len(o9c) == 8, str([p.name for p in o9c]))
    q9, q9_err = quality_report(B, o9)
    check("质量报告记录自动重写次数",
          (q9.get("summary") or {}).get("auto_rewrites", 0) >= 1,
          q9_err or str(q9.get("summary")))
    put(f"{L}/", {"bad_content": {"times": 0}})

    # ---------- 场景十：缝合全部失败 → 降级跳过，正文照常产出 ----------
    scene("场景十：缝合失败降级")
    put(f"{L}/", {"reset": True, "log_clear": True,
                  "fail": {"phase": "stitch", "times": 99, "status": 500,
                           "body": "injected stitch failure", "skip": 0,
                           "arm_after": 0, "extra_breaks": 0},
                  "ctx_limit": {"max_chapters": 0}})
    e10in = tmp / "in10"; make_novel(e10in, chapters=8)
    o10 = tmp / "out10"
    ev10, th10 = sse_events(API_PORT, 300, stop_on="done")
    start_run(dict(body, input_path=str(e10in), output_path=str(o10), context_window=8000,
                   novel_name="缝合降级", refactor_gates={}, compare_output=False,
                   chapter_snapshots=False, max_concurrency=1))
    ok10 = wait_done(ev10, th10, api_port=API_PORT)[0]
    st10 = put(f"{L}/", {})["stats"]
    prog10 = json.loads((o10 / "progress.json").read_text(encoding="utf-8"))
    check("缝合全部失败仍能跑完整轮", ok10, str(st10))
    check("缝合失败已记录进进度", bool(prog10.get("stitch_failures")),
          str(prog10.get("stitch_failures"))[:200])
    o10c = sorted((o10 / "02_workspace" / "reconstructed").glob("chapter_*.txt"))
    check("缝合失败时 8 章正文仍完整",
          len(o10c) == 8 and all(p.stat().st_size > 0 for p in o10c),
          str([p.name for p in o10c]))
    final10 = list((o10 / "03_final").glob("*")) if (o10 / "03_final").is_dir() else []
    check("缝合失败时仍产出成品", len(final10) >= 1, str([p.name for p in final10]))
    q10, q10_err = quality_report(B, o10)
    check("质量报告标出缝合跳过",
          (q10.get("summary") or {}).get("stitch_failures", 0) >= 1,
          q10_err or str(q10.get("summary")))
    put(f"{L}/", {"fail": {"phase": None, "times": 0, "status": 500, "body": "",
                           "skip": 0, "arm_after": 0, "extra_breaks": 0}})

    # ---------- 场景十一：docx 富文本输出 ----------
    scene("场景十一：docx 富文本")
    o11 = tmp / "out_rich"
    ev11, th11 = sse_events(API_PORT, 300, stop_on="done")
    start_run(dict(body, input_path=str(dinp), output_path=str(o11), input_format="docx",
                   novel_name="富文本docx", refactor_gates=NO_AUTO_REWRITE,
                   compare_output=False, chapter_snapshots=False, rich_text=True))
    ok11 = wait_done(ev11, th11, api_port=API_PORT)[0]
    check("富文本 docx 场景跑通", ok11, str(ev11[-1][:2] if ev11 else None))
    rich_merged = o11 / "03_final" / "富文本docx_重构终稿.docx"
    check("富文本成品已生成", rich_merged.exists(), str(rich_merged))
    if rich_merged.exists():
        import zipfile as _zf
        from docx import Document as _RD
        with _zf.ZipFile(rich_merged) as _z:
            _xml11 = _z.read("word/document.xml").decode("utf-8")
        check("章与章之间插入分页", _xml11.count('w:type="page"') >= 2,
              str(_xml11.count('w:type="page"')))
        check("富文本模式不再写字面分隔线",
              all(p.text.strip() != "---" for p in _RD(str(rich_merged)).paragraphs))
        check("文档标题写入元数据", _RD(str(rich_merged)).core_properties.title
              == "富文本docx_重构终稿", str(_RD(str(rich_merged)).core_properties.title))
    plain_merged = doutd / "03_final" / "docx测试_重构终稿.docx"
    if plain_merged.exists():
        from docx import Document as _PD
        check("未开富文本时保持旧排版（字面分隔线）",
              any(p.text.strip() == "---" for p in _PD(str(plain_merged)).paragraphs))

    # ---------- 场景十二：缝合整段重写 → 最小改动闸门拒绝并保留原文 ----------
    # 长度比护栏（0.5~3.0）对「等长但全换写法」完全不设防，只有改动率闸门能挡住。
    # 这里用 mock 注入这种形状的补丁，验证整条链路：校验 → 带错因重试 → 降级保留原文。
    scene("场景十二：缝合最小改动闸门")
    put(f"{L}/", {"reset": True, "log_clear": True,
                  "fail": {"phase": None, "times": 0, "status": 500, "body": "",
                           "skip": 0, "arm_after": 0, "extra_breaks": 0},
                  "ctx_limit": {"max_chapters": 0},
                  "stitch_bulk": {"times": 99}})
    e12in = tmp / "in12"; make_novel(e12in, chapters=8)
    o12 = tmp / "out12"
    ev12, th12 = sse_events(API_PORT, 300, stop_on="done")
    start_run(dict(body, input_path=str(e12in), output_path=str(o12), context_window=8000,
                   novel_name="最小改动闸门", refactor_gates={}, compare_output=False,
                   chapter_snapshots=False, max_concurrency=1))
    ok12 = wait_done(ev12, th12, api_port=API_PORT)[0]
    st12 = put(f"{L}/", {})["stats"]
    prog12 = json.loads((o12 / "progress.json").read_text(encoding="utf-8"))
    check("整段重写场景仍跑完整轮", ok12, str(st12))
    _f12 = prog12.get("stitch_failures") or []
    check("缝合确实被注入过整段重写补丁", len(_f12) >= 1, str(st12))
    check("拒绝原因是最小改动闸门",
          _f12 and all("改动过大" in (x.get("error") or "") for x in _f12),
          str([x.get("error") for x in _f12][:2])[:200])
    check("被拒后带错因重试过一次", st12.get("stitch_corrected", 0) >= 1, str(st12))
    _b12 = [p for p in list((o12 / "02_workspace" / "reconstructed").glob("chapter_*.txt"))
            + list((o12 / "03_final").glob("*.txt"))
            if "模拟整段重写" in p.read_text(encoding="utf-8")]
    check("整段重写补丁没有落进正文", not _b12, str([p.name for p in _b12]))
    o12c = sorted((o12 / "02_workspace" / "reconstructed").glob("chapter_*.txt"))
    check("闸门拒绝后 8 章正文仍完整",
          len(o12c) == 8 and all(p.stat().st_size > 0 for p in o12c),
          str([p.name for p in o12c]))
    q12, q12_err = quality_report(B, o12)
    check("质量报告说明闸门当时是开着的",
          "已开启" in ((q12.get("config") or {}).get("stitch_gate") or ""),
          q12_err or str(q12.get("config"))[:200])
    put(f"{L}/", {"stitch_bulk": {"times": 0}})

    # ---------- 场景十三：单章重生成必须刷新质检结论 ----------
    # /api/regen_chapter 覆盖重构稿后若 batch_qc 不刷新，会两头错：已经改好的章继续
    # 被报告指控（假警报），新引入的硬伤没人记录（假清白）。而且流水线紧接着会把内存
    # progress 整份写回磁盘 —— 刷新结果还必须被「并回内存」，否则 1 秒内被心跳抹掉
    # （与当年 fix_list 静默失效同一个坑）。
    scene("场景十三：单章重生成后刷新质检")
    put(f"{L}/", {"reset": True, "log_clear": True,
                  "fail": {"phase": None, "times": 0, "status": 500, "body": "",
                           "skip": 0, "arm_after": 0, "extra_breaks": 0},
                  "ctx_limit": {"max_chapters": 0},
                  # 前两批的首章各注入一个 TODO 占位：batch1 的第 1 章用来重写，
                  # batch2 的第 5 章必须原样保留（证明只清了被重写的那一章）
                  "bad_content": {"times": 2}})
    e13in = tmp / "in13"; make_novel(e13in, chapters=6, paras=3)
    o13 = tmp / "out13"
    _GATE.suppress = {"accept_ready"}      # 验收门留给本场景自己点
    ev13, th13 = sse_events(API_PORT, 300, stop_on="done")
    start_run(dict(body, input_path=str(e13in), output_path=str(o13), context_window=8000,
                   novel_name="单章重生成刷新质检",
                   refactor_gates={"require_chapter_accept": True,
                                   "auto_rewrite_bad_batch": False},
                   compare_output=False, chapter_snapshots=False, max_concurrency=1))
    t13 = time.time()
    acc13 = None
    while time.time() - t13 < 180:
        acc13 = next((json.loads(d) for _e, d in reversed(ev13) if _e == "accept_ready"), None)
        if acc13:
            break
        time.sleep(0.25)
    check("验收门已弹出（第一批待验收）", bool(acc13), str(acc13)[:120])
    _bid13 = str((acc13 or {}).get("batch_id"))

    def _qc_keys(prog, bids=None):
        pick = {str(k): v for k, v in (prog.get("batch_qc") or {}).items()
                if bids is None or str(k) in bids}
        return {(str(i.get("chapter_id")), i.get("code"))
                for issues in pick.values() for i in (issues or [])}

    p13 = json.loads((o13 / "progress.json").read_text(encoding="utf-8"))
    _before = _qc_keys(p13, {_bid13})
    check("注入的占位硬伤确实进了该批质检结论", ("1", "placeholder") in _before,
          str(_before)[:200])
    _file13 = o13 / "02_workspace" / "reconstructed" / "chapter_1.txt"
    _text_before = _file13.read_text(encoding="utf-8")
    try:
        reg13 = post(f"{B}/api/regen_chapter", {"chapter_id": "1"})
    except Exception as e:
        reg13 = {"error": str(e)}
    check("单章重生成接口应答成功", reg13.get("status") == "regenerated", str(reg13)[:200])
    _text_after = _file13.read_text(encoding="utf-8")
    check("重写后的正文替换了占位符",
          "TODO" not in _text_after and _text_after != _text_before,
          f"before={len(_text_before)} after={len(_text_after)}")
    p13b = json.loads((o13 / "progress.json").read_text(encoding="utf-8"))
    _after13 = _qc_keys(p13b, {_bid13})
    check("该章的旧硬伤结论已随正文刷新", ("1", "placeholder") not in _after13,
          str(_after13)[:200])
    check("同批其他章的结论原样保留", _after13 == _before - {("1", "placeholder")},
          f"before={_before} after={_after13}")
    # 放行验收门，跑完整轮
    _GATE.suppress = set()
    post(f"{B}/api/accept", {})
    ok13 = wait_done(ev13, th13, api_port=API_PORT)[0]
    check("重生成后整轮跑完", ok13, str([e for e, _ in ev13][-2:]))
    p13f = json.loads((o13 / "progress.json").read_text(encoding="utf-8"))
    _allf = _qc_keys(p13f)
    check("刷新结果没有被流水线写回覆盖", ("1", "placeholder") not in _allf, str(_allf)[:240])
    check("另一批的占位硬伤仍在册（只清被重写的那一章）", ("5", "placeholder") in _allf,
          str(_allf)[:240])
    q13, q13_err = quality_report(B, o13)
    check("质量报告里的占位硬伤只剩未重写的那一章",
          (q13.get("summary") or {}).get("placeholder_hits", 0) == 1,
          q13_err or str(q13.get("summary")))
    put(f"{L}/", {"bad_content": {"times": 0}})

    # 输出目录管理端点（GUI 用）：HTTP 层验证
    from urllib.parse import quote
    insp = get(f"{B}/api/output/inspect?output_path={quote(str(o8))}")
    check("输出检查端点报告已完成的成品", insp.get("exists") and insp.get("finished"), str(insp)[:200])
    # 清空一个一次性目录：只删自身产物，用户文件保留
    clr_dir = tmp / "clr"; (clr_dir / "02_workspace").mkdir(parents=True)
    (clr_dir / "progress.json").write_text("{}", encoding="utf-8")
    (clr_dir / "我的笔记.txt").write_text("保留我", encoding="utf-8")
    clr = post(f"{B}/api/output/clear", {"output_path": str(clr_dir)})
    check("清空端点只删自身产物",
          "02_workspace" in (clr.get("removed") or []) and (clr_dir / "我的笔记.txt").exists(),
          str(clr))
    # 任务历史：刚跑完的这本书应出现在列表里，且标记为已完成
    hist = get(f"{B}/api/history")
    _h8 = next((h for h in (hist.get("items") or []) if h.get("output_path") == str(o8)), None)
    check("任务历史记录本次任务", _h8 is not None, str([h.get("output_path") for h in (hist.get("items") or [])])[:200])
    if _h8:
        check("历史标记为已完成", _h8.get("finished") is True, str(_h8)[:160])
        check("历史含批次进度", _h8.get("batches_total", 0) >= 1, str(_h8.get("batches_done")))
    # 成品版本：phase4 收尾时若 03_final 已有内容，会先快照一份再覆盖。
    #
    # 这里不再额外跑第三次流水线。此前为「先有一版成品、再覆盖」而多跑一轮，
    # 但第三轮是整条流水线里最重的一步，且落在若干运行期竞态上（事件流改绑窗口、
    # 收尾阶段状态读数滞后），表现为间歇性跑不完 —— 概率性失败比没有断言更糟。
    # 改为在续跑那一轮之前先放一个占位成品：收尾时它会被快照成版本 1，
    # 被测机制完全相同（03_final 有内容 -> 快照），却不再依赖第三次运行。
    from urllib.parse import quote
    _vers = []
    for _ in range(60):
        _vers = get(f"{B}/api/versions?output_path={quote(str(o8))}").get("versions") or []
        if _vers:
            break
        time.sleep(0.25)
    check("重跑前自动快照上一版成品", len(_vers) >= 1,
          str([v.get("id") for v in _vers]))

finally:
    for p in (llm, api):
        p.terminate()
        try: p.wait(timeout=5)
        except Exception: p.kill()
    # stderr 已 DEVNULL，Traceback 排查统一从 backend.log 取。
    err = ""
    try:
        err = (BACKEND / "data" / "logs" / "backend.log").read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    if "Traceback" in err:
        print("\n=== 后端日志异常 ===\n", err[-2000:])
    for ev in _GATE.failed:
        FAILS.append(f"gate_confirm_failed:{ev}")
    shutil.rmtree(tmp, ignore_errors=True)

print()
print("=== 全部通过 ===" if not FAILS else f"=== {len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
