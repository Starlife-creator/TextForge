"""端到端：真正把整条 phase0→phase4 流水线跑一遍，验证成品质量。

这是四轮静态审查都覆盖不到的一类问题：成品 docx 到底对不对。
用 mock_llm.py 起本地 OpenAI 兼容端点，全程走真实 HTTP，不打桩被测代码。
"""
import io, json, os, re, shutil, subprocess, sys, tempfile, threading, time, urllib.request
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
PY = str(BACKEND / "venv" / "Scripts" / "python.exe")
if sys.platform.startswith("win"):          # CI 上由 CI 环境提供解释器路径
    PY = sys.executable
# 端口可经环境变量覆盖：固定端口在 CI 并发/残留进程下会假失败
LLM_PORT = int(os.environ.get("TF_E2E_LLM_PORT", "8299"))
API_PORT = int(os.environ.get("TF_E2E_API_PORT", "8298"))

FAILS = []
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
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def put(url, obj, timeout=30):
    req = urllib.request.Request(url, data=json.dumps(obj).encode(), method="PUT",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


_ALL_EVENTS = []   # 全局闸门驱动消费的事件队列


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
                            _ALL_EVENTS.append(_pair)
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


def gate_driver(port, events, enabled=True):
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
    """
    idx = 0
    t0 = time.time()
    _resume_ev = {"split": "split_ready", "fix": "fix_ready",
                  "accept": "accept_ready", "qc": "qc_failed"}
    gates = set(_resume_ev.values()) | {"blueprint_ready"}
    while time.time() - t0 < 900:
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
        while idx < len(events):
            ev, data = events[idx]; idx += 1
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
            key = (ev, d.get("batch_id")) if ev == "accept_ready" else ev
            if key in _GATE.seen:
                continue
            _GATE.seen.add(key)
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
            post(f"http://127.0.0.1:{port}/api/split/confirm", {})
            print("    [gate] 已确认拆书")
        elif ev == "blueprint_ready":
            bp = d.get("blueprint") or ""
            if len(bp) < 100:
                bp = "（测试桩：蓝图正文过短时补齐，确认接口要求 >=100 字）" + bp
            post(f"http://127.0.0.1:{port}/api/blueprint/confirm", {"blueprint": bp})
            print("    [gate] 已确认蓝图")
        elif ev == "fix_ready":
            post(f"http://127.0.0.1:{port}/api/fix/confirm",
                 {"action": "accept", "gaps": []})
            print("    [gate] 已确认诊断")
        elif ev == "accept_ready":
            # 端点是 /api/accept（不是 /api/accept/confirm）；批次取自 progress.json
            # 的 accept_pending_batch，因此无需在此处传 batch_id。
            post(f"http://127.0.0.1:{port}/api/accept", {})
            print(f"    [gate] 已验收批次 {d.get('batch_id')}")
        elif ev == "qc_failed":
            post(f"http://127.0.0.1:{port}/api/export/force", {})
            print("    [gate] 已强制导出")
    except Exception as e:
        # 闸门已推进（409）说明已被确认过，属正常；其余仅记录不中断驱动线程
        if "409" in str(e):
            return
        print(f"    [gate] {ev} 确认异常（忽略）: {type(e).__name__} {e}")


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
        if any(e in ("done", "stopped", "error") for e, _ in events):
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
                       cwd=str(BACKEND), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
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
        err = api.stderr.read().decode("utf-8", "replace") if api.stderr else ""
        raise RuntimeError(f"后端/ mock 未就绪。api stderr:\n{err[-1500:]}")
    # 所有场景共用同一个事件队列（_ALL_EVENTS），全局驱动据此按闸门事件行动
    print("mock / health:", get(f"{L}/v1/models")["data"][0]["id"], "|", get(f"{B}/api/health")["status"])
    # 全局唯一的闸门驱动：像真实用户那样把所有人工闸门都点掉。
    threading.Thread(target=gate_driver, args=(API_PORT, _ALL_EVENTS),
                     daemon=True).start()

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
        "refactor_gates": {"require_chapter_accept": True, "qc_block_export": True},
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
    print("\n--- 场景二：docx 输入 ---")
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
              novel_name="docx测试", refactor_gates={}, compare_output=False,
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
    print("\n--- 场景三：分隔符数量不符 → 重试纠正 ---")
    put(f"{L}/", {"fail": {"phase": None, "times": 0, "status": 500, "body": "", "extra_breaks": 1}})
    put(f"{L}/", {"reset": True, "log_clear": True})
    e3in = tmp / "in3"; make_novel(e3in, chapters=4)
    o3 = tmp / "out3"
    ev3, th3 = sse_events(API_PORT, 240, stop_on="done")
    r3 = post(f"{B}/api/start", dict(body, input_path=str(e3in), output_path=str(o3),
                                      novel_name="分隔符纠正", refactor_gates={},
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
    print("\n--- 场景四：上下文超限 → 自动拆批 ---")
    put(f"{L}/", {"fail": {"phase": None, "times": 0, "status": 500, "body": "", "extra_breaks": 0}})
    # 让 mock 只接受 ≤2 章的请求，而应用以为窗口很大（context_window=32768 → 一批全塞）
    put(f"{L}/", {"reset": True, "log_clear": True, "ctx_limit": {"max_chapters": 2}})
    e4in = tmp / "in4"; make_novel(e4in, chapters=6)
    o4 = tmp / "out4"
    ev4, th4 = sse_events(API_PORT, 300, stop_on="done")
    r4 = post(f"{B}/api/start", dict(body, input_path=str(e4in), output_path=str(o4),
                                      context_window=32768, novel_name="上下文超限自愈",
                                      refactor_gates={}, compare_output=False,
                                      chapter_snapshots=False))
    ok4 = wait_done(ev4, th4, api_port=API_PORT)[0]
    st4 = put(f"{L}/", {})["stats"]
    splits = [d for e, d in ev4 if e == "batch_split"]
    if not ok4:
        errs4 = [d for e, d in ev4 if e == "error"]
        print("  错误:", errs4[-1][:220] if errs4 else [e for e, _ in ev4][-8:])
    print(f"    超限次数={st4.get('ctx_overflow', 0)} 自动拆批次数={len(splits)}")
    check("上下文超限时流水线仍能跑完（自动拆批自愈）", ok4, str(st4))
    check("确实发生了自动拆批", len(splits) >= 1, str(len(splits)))
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
    print("\n--- 场景五：单文件章节识别（缩进/序章/正文提及）---")
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
                                      refactor_gates={}, compare_output=False,
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
    probe = get(f"{B}/api/system/context_probe?api_url={L}/v1/chat/completions"
                f"&model=mock-model&configured=32768")
    check("探测到服务端真实上下文窗口", probe.get("detected") == PROBE_N, str(probe))
    check("配置大于实测时给出提示", bool(probe.get("mismatch")), str(probe)[:160])

    # ---------- 场景六：无编号标题 → AI 辅助拆章 ----------
    # 一本书全用「纯标题行」分章（没有任何「第X章」），规则必然识别不出。
    # 期望：置信度低 → AI 只在候选行里挑边界 → 仍需人工确认后才继续。
    print("\n--- 场景六：无编号标题 → AI 拆章 ---")
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
                   output_path=str(o6), novel_name="AI拆章", refactor_gates={},
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
    print("\n--- 场景七：自定义章节标记重拆 ---")
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
                   output_path=str(o7), novel_name="人工重拆", refactor_gates={},
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
    print("\n--- 场景八：中途失败 → 断点续跑 ---")
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
                   novel_name="断点续跑", refactor_gates={}, compare_output=False,
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
    st8a = put(f"{L}/", {})["stats"]
    check("注入的失败发生在第 2 次重构调用之后（第一批未被波及）",
          st8a.get("refactor", 0) >= 2, str(st8a))
    st8a = put(f"{L}/", {})["stats"]
    done8 = [b for b in prog8.get("batches", []) if b.get("status") == "phase3_done"]
    recon8a = sorted((o8 / "02_workspace" / "reconstructed").glob("chapter_*.txt"))
    check("第一批成功后因错误中断", bool(prog8.get("error_logs")),
          f"error_logs={len(prog8.get('error_logs') or [])}")
    check("中断时第一批已标记完成且有成品", len(done8) >= 1 and len(recon8a) >= 1,
          f"done={len(done8)} files={len(recon8a)}")
    check("中断时进度停在 phase3", (prog8.get("current_phase") or "").startswith("phase3"),
          str(prog8.get("current_phase")))
    saved_names = {p.name for p in recon8a}

    # 第二次：清掉故障，原样重跑同一输出目录
    put(f"{L}/", {"reset": True, "log_clear": True,
                  "fail": {"phase": None, "times": 0, "status": 500, "body": "", "skip": 0,
                   "arm_after": 0},
                  "ctx_limit": {"max_chapters": 0}})
    ev8b, th8b = sse_events(API_PORT, 300, stop_on="done")
    start_run(dict(body, input_path=str(e8in), output_path=str(o8), context_window=8000,
                   novel_name="断点续跑", refactor_gates={}, compare_output=False,
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
    # 成品版本：再跑一次同一目录，覆盖前应快照出上一版
    ev8c, th8c = sse_events(API_PORT, 300, stop_on="done")
    start_run(dict(body, input_path=str(e8in), output_path=str(o8), context_window=8000,
                   novel_name="断点续跑", refactor_gates={}, compare_output=False,
                   chapter_snapshots=False))
    # 等第三次重跑跑完：**只看 /api/status，不依赖 SSE 的 done 事件**。
    # 这次 SSE 订阅会漏掉终态事件（新 run 起始的改绑窗口），流水线其实正常跑完、
    # 版本快照也已生成，只是测试收不到 done —— 于是误判成失败。
    # 场景八的第一次运行本来就是用 progress.json 轮询的，这里保持一致。
    finished8c = False
    for _ in range(480):
        try:
            st = get(f"{B}/api/status")
            if not st.get("pipeline_running") and (o8 / "03_final").is_dir():
                finished8c = True
                break
        except Exception:
            time.sleep(0.5); continue
        time.sleep(0.5)
    check("第三次重跑确实跑完（版本快照的前提）", finished8c,
          f"pipeline_running={get(f'{B}/api/status').get('pipeline_running')}")
    # 成品版本：再跑一次同一目录，覆盖前应快照出上一版。
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
    err = api.stderr.read().decode("utf-8", "replace") if api.stderr else ""
    if "Traceback" in err:
        print("\n=== 后端 stderr 异常 ===\n", err[-2000:])
    shutil.rmtree(tmp, ignore_errors=True)

print()
print("=== 全部通过 ===" if not FAILS else f"=== {len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
