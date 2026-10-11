"""端到端回归：验证本轮修复的关键不变量。

覆盖：
  1. 闸门写入不再被流水线心跳覆盖（absorb_gate_result）
  2. 控制端点不再把 current_phase 回退到 phase2
  3. phase3_accept 在恢复时能重新推送验收预览
  4. 统一后台心跳在 LLM 调用期间也刷新 last_heartbeat
  5. /api/system/logs 的 offset 语义（首屏不能返回空）
  6. 批次/章节表一致性前置校验
  7. 缝合锚点去重、虚拟章 output_file 推导
  8. 自然排序、逐章成品去重、docx 段尾空行
"""
import asyncio, json, os, shutil, sys, tempfile, time
from pathlib import Path

# 控制台编码：本地 Windows 常是 GBK，GitHub runner 是 cp1252，
# 直接 print 中文会 UnicodeEncodeError 让整套测试「假失败」（在断言全过之后才崩）。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).parent))
# 绝不改 %APPDATA%：Windows 上它总是有值，改了等于污染开发者真实环境。
# 需要隔离目录的测试各自用 tempfile.mkdtemp，并断言真实 APPDATA 未被触碰。
_REAL_APPDATA = os.environ.get("APPDATA")

from routes import state
from utils.atomic import atomic_write_json

# 捕获 textforge logger 的告警文本（用于验证「只告警」类行为）
import logging
_records = []
class _Cap(logging.Handler):
    def emit(self, r): _records.append(r.getMessage())
logging.getLogger("textforge").addHandler(_Cap())
def caplog_text(): return " || ".join(_records)

_FAILS = []
def check(name, cond, extra=""):
    # 控制台是 GBK，遇到 ↔ 之类字符会 UnicodeEncodeError 直接中断整个套件
    try:
        line = f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  [{extra}]" if extra and not cond else "")
        print(line)
    except UnicodeEncodeError:
        print(f"{'PASS' if cond else 'FAIL'}  {name.encode('ascii','replace').decode()}")
    if not cond:
        _FAILS.append(name)

# ---------- 1. absorb_gate_result ----------
tmp = Path(tempfile.mkdtemp(prefix="tf_gate_"))
pp = tmp / "progress.json"
mem = {"fix_list": [], "accepted_batches": [], "current_phase": "phase1_fixreview"}
atomic_write_json(pp, mem)                      # 流水线内存版已落盘（心跳写）
disk = json.loads(pp.read_text(encoding="utf-8"))
disk["fix_list"] = ["第三幕时间线矛盾"]           # 模拟控制端点写入用户选择
atomic_write_json(pp, disk)
asyncio.run(state.absorb_gate_result(mem, pp, ["fix_list"]))
check("absorb_gate_result 能取回用户选择",
      mem["fix_list"] == ["第三幕时间线矛盾"], mem["fix_list"])
check("absorb_gate_result 是协程（退避不得阻塞事件循环）",
      asyncio.iscoroutinefunction(state.absorb_gate_result))
# 读盘失败必须抛，绝不能静默沿用内存旧值后又被写回覆盖用户的选择
try:
    asyncio.run(state.absorb_gate_result({"fix_list": []}, tmp / "不存在.json", ["fix_list"]))
    check("读盘失败时中止而非静默丢用户选择", False)
except RuntimeError:
    check("读盘失败时中止而非静默丢用户选择", True)

# 测试不得污染开发者真实环境
check("测试未改动 %APPDATA%", os.environ.get("APPDATA") == _REAL_APPDATA,
      f"{_REAL_APPDATA} -> {os.environ.get('APPDATA')}")

# ---------- 2. 控制端点不再写 current_phase ----------
src = (Path(__file__).parent / "routes" / "control.py").read_text(encoding="utf-8")
bad = [l.strip() for l in src.splitlines() if "current_phase'] =" in l]
check("control.py 不再写 current_phase", not bad, str(bad))

# ---------- 3. phase_passed 语义（决定 phase2 会不会被重跑） ----------
check("phase_passed('phase3-1','phase2_waiting') 成立",
      state.phase_passed({"current_phase": "phase3-1"}, "phase2_waiting"))
check("phase_passed('phase2','phase2_waiting') 不成立（这才是旧 bug 的根因）",
      not state.phase_passed({"current_phase": "phase2"}, "phase2_waiting"))
# 停在任意 phase3 闸门时都必须「已越过 phase2_waiting」，否则蓝图会被重新生成
for p in ("phase3_accept", "phase3_qc", "phase3-1"):
    check(f"停在 {p} 时不会重新生成蓝图",
          state.phase_passed({"current_phase": p}, "phase2_waiting"),
          f"index={state.phase_index(p)}")
# PHASE_ORDER 必须严格单调，否则「已越过」判定不可靠
seen = []
for p in ("phase0", "phase0_confirm", "phase1", "phase1_fixreview", "phase2", "phase2_waiting",
          "phase3_accept", "phase3_qc", "phase3-1", "phase4"):
    seen.append(state.phase_index(p))
check("PHASE_ORDER 覆盖全部阶段", all(i >= 0 for i in seen), str(seen))
check("PHASE_ORDER 严格递增", all(seen[i] < seen[i + 1] for i in range(len(seen) - 1)), str(seen))
check("phase0_confirm 在 phase0 之后（早前缺失会返回 -1）",
      state.phase_index("phase0_confirm") > state.phase_index("phase0"))
check("phase_passed('phase1_fixreview','phase1') 在 phase2 成立",
      state.phase_passed({"current_phase": "phase2"}, "phase1"))
check("phase_passed('phase0_confirm','phase1') 不成立（确实还没诊断）",
      not state.phase_passed({"current_phase": "phase0_confirm"}, "phase1"))

# ---------- 4. phase0_confirm 可恢复；且所有会无限等待的闸门都有 resume 分支 ----------
from routes.status import _build_resume
_GATES = {
    "phase0_confirm": "split",
    "phase1_fixreview": "fix",
    "phase3_accept": "accept",
    "phase3_qc": "qc",
}
_base_progress = {
    "chapters": [{"id": "1", "filename": "a.txt"}],
    "batches": [{"batch_id": 0, "chapter_ids": ["1"]}],
    "accept_pending_batch": 0,
    "qc_issues": ["空章"],
}
for _ph, _want in _GATES.items():
    _pg = dict(_base_progress, current_phase=_ph)
    _r = _build_resume(_pg, tmp)
    # 任何会 while-not-set 无限等待的阶段，_build_resume 必须返回非 None，
    # 否则刷新页面 / SSE 重连后弹窗再也开不出来，任务 0% 死转
    check(f"_build_resume 覆盖 {_ph}", _r is not None and _r.get("key") == _want, str(_r))
for _ph in ("phase3-1", "phase4"):
    check(f"{_ph} 不应弹闸门", _build_resume(dict(_base_progress, current_phase=_ph), tmp) is None)

# ---------- 5. 自然排序 ----------
from utils.sort import natural_sort_key
_SORT_CASES = [
    # (文件名, 期望章号, 说明)
    ("第10章.txt", 10, "标准命名"),
    ("第2章.txt", 2, "标准命名"),
    ("1.txt", 1, "纯数字文件名（多文件模式常见）"),
    ("001.txt", 1, "补零数字"),
    ("我的书-第3章.txt", 3, "带前缀的 第N章"),
    ("正文 第一章.txt", 1, "带前缀且 第 与数字间有空格"),
    ("【第一章】初入江湖.txt", 1, "方括号包裹"),
    ("作者的话2024.txt", 0, "散文里的年份不是章号"),
    ("完结于2023年.txt", 0, "同上"),
    ("楔子.txt", 0, "无章号"),
    ("楔子1.txt", 1, "带序号的楔子"),
    ("正文-1.txt", 1, "带前缀的裸数字 —— 只锚定会退化成 0 导致顺序错乱"),
    ("Chapter 1.txt", 1, "英文命名"),
    ("卷二.txt", 2, "卷号"),
]
for _fn, _want, _why in _SORT_CASES:
    _got = natural_sort_key(_fn)[0]
    check(f"自然排序 {_fn} == {_want}", _got == _want, f"got {_got} （{_why}）")
# 多文件场景：顺序必须正确（旧实现的 sort key 同样被 splitter/finalize 使用）
for _grp in (["正文-1.txt", "正文-2.txt", "正文-10.txt"],
             ["Chapter 1.txt", "Chapter 2.txt", "Chapter 10.txt"],
             ["我的书-第3章.txt", "第1章.txt", "第2章.txt", "第10章.txt"],
             ["1.txt", "2.txt", "10.txt", "001.txt"]):
    _ordered = sorted(_grp, key=natural_sort_key)
    check(f"混合命名排序 {[natural_sort_key(n)[0] for n in _ordered]}",
          [natural_sort_key(n)[0] for n in _ordered]
          == sorted(natural_sort_key(n)[0] for n in _grp), str(_ordered))
check("年份文件排在前部且不影响其它章",
      natural_sort_key("作者的话2024.txt")[0] == 0)

# ---------- 6. 缝合锚点去重 ----------
from core.stitch import _record_anchors
p = {"stitch_anchors": []}
patch = {"file": "02_workspace/reconstructed/chapter_1.txt", "original": "原文A", "replacement": "替换B"}
_record_anchors(p, [patch]); _record_anchors(p, [dict(patch)]); _record_anchors(p, [patch])
check("缝合锚点按指纹去重", len(p["stitch_anchors"]) == 1, str(len(p["stitch_anchors"])))

# ---------- 7. 虚拟章 output_file 推导 ----------
from core.stitch import _virtual_output_rel
check("虚拟章 output_file 由 virtual_id 推导",
      _virtual_output_rel({"virtual_id": "7-2"}) == "02_workspace/reconstructed/chapter_7-2.txt")

# ---------- 8. _accum_usage 兼容旧 progress ----------
from core.splitter import _accum_usage
legacy = {}                       # 8.8 之前、没有 usage 键的 progress.json
_accum_usage(legacy, {"prompt_tokens": 10, "completion_tokens": 3})
_accum_usage(legacy, {"prompt_tokens": 5, "completion_tokens": 1})
check("_accum_usage 兼容缺 usage 键的旧 progress",
      legacy["usage"] == {"prompt_tokens": 15, "completion_tokens": 4}, str(legacy.get("usage")))

# ---------- 9. 章节分隔符数量校验 ----------
from core.refactor import _split_refactored_by_chapters, ChapterBreakMissingError
check("单章不分隔", _split_refactored_by_chapters("全文", ["1"]) == {"1": "全文"})
try:
    # 3 章只给了 1 个标记（少 1 个）→ 必须报错，而不是错位塞进第 1/2 章
    _split_refactored_by_chapters("甲===CHAPTER_BREAK===乙===CHAPTER_BREAK===丙".replace("===CHAPTER_BREAK===丙", ""), ["1", "2", "3"])
    check("标记数不足时抛 ChapterBreakMissingError", False)
except ChapterBreakMissingError:
    check("标记数不足时抛 ChapterBreakMissingError", True)
got = _split_refactored_by_chapters("甲===CHAPTER_BREAK===乙", ["1", "2"])
check("分隔符按位对齐", got == {"1": "甲", "2": "乙"}, str(got))
_M = "===CHAPTER_BREAK==="
check("容忍结尾多余标记（模型常犯）",
      _split_refactored_by_chapters(f"甲{_M}乙{_M}丙{_M}", ["1", "2", "3"]) ==
      {"1": "甲", "2": "乙", "3": "丙"})
check("容忍结尾多个多余标记",
      _split_refactored_by_chapters(f"甲{_M}乙{_M}丙{_M}{_M}", ["1", "2", "3"]) ==
      {"1": "甲", "2": "乙", "3": "丙"})
# 单章批次（每本书最后一个批次都是）也必须拦空章，
# 否则会静默写出一个空 chapter_*.txt，再被 document.py 的跳过空段吞掉
for _empty in ("", "   \n\t "):
    try:
        _split_refactored_by_chapters(_empty, ["7"])
        check(f"单章空输出被拦截（{_empty!r}）", False)
    except ChapterBreakMissingError:
        check(f"单章空输出被拦截（{_empty!r}）", True)
check("单章正常输出走快速路径",
      _split_refactored_by_chapters(" 正文甲 ", ["7"]) == {"7": "正文甲"})
# 旧实现会先过滤空段再比对：某一章重构为纯空白时被丢弃 → 数量对不上 → 整轮白跑
# 但按位对齐后也不能把空白章写进成品，必须作为可重试错误抛出
try:
    _split_refactored_by_chapters("甲===CHAPTER_BREAK===  ===CHAPTER_BREAK===丙", ["1", "2", "3"])
    check("空白章作为可重试错误抛出（不会静默进成品）", False)
except ChapterBreakMissingError:
    check("空白章作为可重试错误抛出（不会静默进成品）", True)
# 模型在首段前加寒暄：应被 strip 进第 1 章而不是错位（对齐正确即可，不应抛错）
try:
    got_pre = _split_refactored_by_chapters("好的，以下是重构结果：\n正文甲===CHAPTER_BREAK===正文乙", ["1", "2"])
    check("首段前言不导致错位", got_pre["2"] == "正文乙", str(got_pre))
except ChapterBreakMissingError:
    check("首段前言不导致错位", False)

# ---------- 10. docx 段尾不再拖空行 ----------
from docx import Document
from core.document import write_text_docx
docx_path = tmp / "t.docx"
write_text_docx("第一段第一行\n第一段第二行\n\n第二段", str(docx_path))
d = Document(str(docx_path))
texts = [p.text for p in d.paragraphs]
check("docx 段尾无多余换行", texts == ["第一段第一行\n第一段第二行", "第二段"], str(texts))
check("docx 跳过空段", all(t.strip() for t in texts), str(texts))

# ---------- 11. 逐章成品去重 ----------
import core.finalize as F
src_fin = (Path(__file__).parent / "core" / "finalize.py").read_text(encoding="utf-8")
check("finalize.py 含成品名去重逻辑", "used = set()" in src_fin)

# ---------- 12. 密钥只在「换了主机」时才丢弃，同主机绝不打断既有配置 ----------
class _Req:
    api_url = "http://127.0.0.1:1234/v1/chat/completions"
    api_key = "SECRET-CLOUD-KEY"
    model = "m"
from core.api_client import pick_endpoint, _same_provider

_SAME_HOST_CASES = [
    ("http://127.0.0.1:1234/v1/chat/completions", "http://127.0.0.1:1234/api/v1/chat/completions", "同 host 不同路径"),
    ("http://127.0.0.1:1234/v1/chat/completions", "http://localhost:1234/v1/chat/completions", "127.0.0.1 ↔ localhost"),
    ("https://api.x.com/v1/chat/completions", "http://api.x.com/v1/chat/completions", "http ↔ https"),
    ("http://127.0.0.1:8000/v1/chat/completions", "http://127.0.0.1:8080/v1/chat/completions", "本机不同端口的反代"),
]
for _g, _ph, _why in _SAME_HOST_CASES:
    _r = _Req(); _r.api_url = _g; _r.models = {"diagnose": {"model": "m2", "api_url": _ph}}
    _got = pick_endpoint(_r, "diagnose")["api_key"]
    check(f"同主机保留全局 Key（{_why}）", _got == "SECRET-CLOUD-KEY", _got)

_DIFF_HOST_CASES = [
    ("http://127.0.0.1:1234/v1/chat/completions", "https://third-party.example/v1/chat/completions"),
    ("https://api.openai.com/v1/chat/completions", "https://api.anthropic.com/v1/chat/completions"),
]
for _g, _ph in _DIFF_HOST_CASES:
    _r = _Req(); _r.api_url = _g; _r.models = {"diagnose": {"model": "m2", "api_url": _ph}}
    _got = pick_endpoint(_r, "diagnose")["api_key"]
    check(f"换主机时丢弃全局 Key（防泄露 {_ph}）", _got == "", _got)

# 显式提供的分阶段 key 永远优先
_r = _Req(); _r.models = {"diagnose": {"model": "m2", "api_url": "https://third-party.example/v1/chat/completions", "api_key": "PHASE-KEY"}}
check("显式分阶段 api_key 优先", pick_endpoint(_r, "diagnose")["api_key"] == "PHASE-KEY")

# 解析不出主机名时保守保留 Key：这种 URL 本来也发不出请求，
# 误丢 Key 只会把本来能跑的配置变成 401
for _bad in ("", "not-a-url", "localhost:1234/v1/chat/completions"):
    _r = _Req(); _r.models = {"diagnose": {"model": "m2", "api_url": _bad}}
    _got = pick_endpoint(_r, "diagnose")["api_key"]
    check(f"无法解析主机名时保留 Key（{_bad or '<空>'}）", _got == "SECRET-CLOUD-KEY", _got)

# ---------- 13. 后台心跳覆盖 LLM 调用期 ----------
from routes.pipeline_runner import _heartbeat_loop
hb_dir = Path(tempfile.mkdtemp(prefix="tf_hb_")) / "progress.json"
atomic_write_json(hb_dir, {"current_phase": "phase3-1", "stitch_anchors": ["x"]})

async def _hb_test():
    task = asyncio.create_task(_heartbeat_loop(hb_dir, interval=0.2))
    await asyncio.sleep(0.5)                      # 模拟一次长时间 LLM await
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    data = json.loads(hb_dir.read_text(encoding="utf-8"))
    return data

data = asyncio.run(_hb_test())
from datetime import datetime, timezone
age = (datetime.now(timezone.utc) - datetime.strptime(data["last_heartbeat"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)).total_seconds()
check("后台心跳在 await 期间刷新", age < 5, f"age={age}")
check("后台心跳不破坏其它字段", data["stitch_anchors"] == ["x"])

# ---------- 14. 超长章组：只告警不报错 ----------
# 硬报错会误杀「context_window 填得小、但模型窗口大」这类本来能跑完的配置
from core.splitter import _load_batches
over_dir = Path(tempfile.mkdtemp(prefix="tf_over_"))
big = over_dir / "split" / "1.txt"
big.parent.mkdir(parents=True, exist_ok=True)
big.write_text("字" * 9000, encoding="utf-8")
chs = [{"id": "1", "filename": "第1章.txt", "split_file": "split/1.txt"}]
try:
    b = _load_batches(chs, over_dir, batch_limit=4000)
    check("超长章组放行而非硬失败（不误杀可用配置）", b == [["1"]], str(b))
except RuntimeError as e:
    check("超长章组放行而非硬失败（不误杀可用配置）", False, str(e))
_log = caplog_text()
check("超长章组仍有告警", "超过单批上限" in _log, _log[:160])

# ---------- 15. 日志 offset 语义 ----------
# 注意：绝不能用 os.environ["APPDATA"] 定位日志路径 —— Windows 上它总是有值，
# setdefault 是空操作，会把开发者真实的 %APPDATA%\TextForge\logs\backend.log 覆盖掉。
import routes.system as S
_logdir = Path(tempfile.mkdtemp(prefix="tf_logs_"))
S.APP_DIR = _logdir
S.LOG_DIR = _logdir / "logs"
S.LOG_PATH = S.LOG_DIR / "backend.log"
S.LOG_DIR.mkdir(parents=True, exist_ok=True)
S.LOG_PATH.write_text("\n".join(f"line{i}" for i in range(500)), encoding="utf-8")
full = asyncio.run(S.system_logs(lines=50, offset=0))
check("logs offset=0 返回首屏而非空", len(full["lines"]) == 50 and full["lines"][0] == "line0",
      f"got {len(full['lines'])}")
check("logs offset 返回游标", full["offset"] == 50, str(full["offset"]))
inc = asyncio.run(S.system_logs(lines=50, offset=50))
check("logs offset=N 返回增量", inc["lines"][0] == "line50", inc["lines"][:1])
beyond = asyncio.run(S.system_logs(lines=50, offset=99999))
check("logs offset 越界不炸", beyond["lines"] == [] and beyond["offset"] == 500, str(beyond["offset"]))

for d in (tmp, _logdir):
    shutil.rmtree(d, ignore_errors=True)
print()
if _FAILS:
    print(f"=== {len(_FAILS)} FAILED: {_FAILS}")
    sys.exit(1)
print("=== 全部通过 ===")
