"""端到端：真正把整条 phase0→phase4 流水线跑一遍，验证成品质量。

这是四轮静态审查都覆盖不到的一类问题：成品 docx 到底对不对。
用 mock_llm.py 起本地 OpenAI 兼容端点，全程走真实 HTTP，不打桩被测代码。
"""
import io, json, os, re, shutil, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path

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
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def get(url, timeout=30):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def put(url, obj, timeout=30):
    req = urllib.request.Request(url, data=json.dumps(obj).encode(), method="PUT",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


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
                            out.append((ev.group(1), da.group(1).strip()))
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


def gate_driver(port, events, seen, enabled=True):
    """消费事件流并自动确认所有人工闸门。
    流水线的几个门是**无限等待**的（while not Event），不驱动就只能停在原地 ——
    这本身是正确行为，测试必须像真实用户那样点确认。"""
    idx = 0
    t0 = time.time()
    while time.time() - t0 < 300:
        while idx < len(events):
            ev, data = events[idx]; idx += 1
            if ev in seen:
                continue
            seen.add(ev)
            if not enabled:
                continue
            try:
                d = json.loads(data)
            except json.JSONDecodeError:
                d = {}
            run_id = d.get("run_id")
            try:
                if ev == "split_ready":
                    post(f"http://127.0.0.1:{port}/api/split/confirm", {"run_id": run_id})
                    print("    [gate] 已确认拆书")
                elif ev == "blueprint_ready":
                    bp = d.get("blueprint") or ""
                    if len(bp) < 100:
                        bp = (bp + "\n（补充说明：保持人物弧光完整，补充过渡。）") * 4
                    post(f"http://127.0.0.1:{port}/api/blueprint/confirm",
                         {"run_id": run_id, "blueprint": bp[:4000], "user_edited": False})
                    print("    [gate] 已确认蓝图")
                elif ev == "fix_ready":
                    post(f"http://127.0.0.1:{port}/api/fix/confirm",
                         {"run_id": run_id, "fix_list": [g.get("id") for g in d.get("gaps", [])]})
                    print("    [gate] 已确认断层")
                elif ev == "accept_ready":
                    post(f"http://127.0.0.1:{port}/api/accept", {"run_id": run_id})
                    print(f"    [gate] 已验收批次 {d.get('batch_id')}")
                elif ev == "qc_failed":
                    post(f"http://127.0.0.1:{port}/api/export/force", {"run_id": run_id})
                    print("    [gate] 已强制导出")
            except Exception as e:
                detail = ""
                if isinstance(e, urllib.error.HTTPError):
                    try: detail = e.read().decode("utf-8", "replace")[:200]
                    except Exception: detail = ""
                snap = ""
                try:
                    snap = json.dumps(get(f"http://127.0.0.1:{port}/api/status"))[:220]
                except Exception:
                    pass
                print(f"    [gate] {ev} 确认失败: {e} {detail}")
                print(f"    [gate] 此刻 status: {snap}")
        time.sleep(0.3)


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
    """等终态事件。返回 (是否到达, 终态瞬间的 pipeline_running 快照)。

    必须在 done 出现的**同一刻**采样 pipeline_running：
    UI 收到 done 就认为结束，若此刻后端仍报 running，用户会撞上 409。
    """
    t0 = time.time()
    while time.time() - t0 < timeout:
        if any(e in ("done", "stopped", "error") for e, _ in events):
            snap = None
            if api_port:
                try:
                    snap = get(f"http://127.0.0.1:{api_port}/api/status")["pipeline_running"]
                except Exception:
                    snap = None
            return True, snap
        time.sleep(0.1)
    return False, None


llm = subprocess.Popen([PY, str(BACKEND / "mock_llm.py"), str(LLM_PORT)],
                       cwd=str(BACKEND), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
api = subprocess.Popen([PY, "-m", "uvicorn", "app:app", "--host", "127.0.0.1",
                        "--port", str(API_PORT), "--log-level", "warning"],
                       cwd=str(BACKEND), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
tmp = Path(tempfile.mkdtemp(prefix="tf_e2e_"))
try:
    time.sleep(6)
    B = f"http://127.0.0.1:{API_PORT}"
    L = f"http://127.0.0.1:{LLM_PORT}"
    print("mock / health:", get(f"{L}/v1/models")["data"][0]["id"], "|", get(f"{B}/api/health")["status"])

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

    import threading
    seen = set()
    driver = threading.Thread(target=gate_driver, args=(API_PORT, events, seen), daemon=True)
    driver.start()

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
    seen2 = set()
    threading.Thread(target=gate_driver, args=(API_PORT, ev2, seen2), daemon=True).start()
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
