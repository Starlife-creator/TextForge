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
import asyncio, json, os, shutil, sys, tempfile
from pathlib import Path

# 控制台编码：本地 Windows 常是 GBK，GitHub runner 是 cp1252，
# 直接 print 中文会 UnicodeEncodeError 让整套测试「假失败」（在断言全过之后才崩）。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).parent))
# 回归脚本大量用 TestClient 直接打 API；显式开发模式关闭鉴权。
# 鉴权专项测试会临时移除该变量，验证「未配置 token 默认拒绝」。
_REAL_DEV = os.environ.get("TEXTFORGE_DEV")
os.environ["TEXTFORGE_DEV"] = "1"
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
src_fin = (Path(__file__).parent / "core" / "finalize.py").read_text(encoding="utf-8")
check("finalize.py 含成品名去重逻辑", "used = set()" in src_fin)

# ---------- 12. 密钥只在「换了主机」时才丢弃，同主机绝不打断既有配置 ----------
class _Req:
    api_url = "http://127.0.0.1:1234/v1/chat/completions"
    api_key = "SECRET-CLOUD-KEY"
    model = "m"
from core.api_client import pick_endpoint

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
# 心跳现在**只写内存**：progress.json 在缝合阶段可达数十 MB（锚点内嵌全文），
# 每 15s 全量重写 + fsync 会造成大量写放大；而 last_heartbeat 的唯一用途是前端
# 判断僵死，不参与恢复决策。因此这里断言「心跳刷新了内存值、且不动 progress.json」。
from routes.pipeline_runner import _heartbeat_loop
hb_dir = Path(tempfile.mkdtemp(prefix="tf_hb_")) / "progress.json"
atomic_write_json(hb_dir, {"current_phase": "phase3-1", "stitch_anchors": ["x"]})
_before_mtime = hb_dir.stat().st_mtime_ns


async def _hb_test():
    state.last_heartbeat = None
    task = asyncio.create_task(_heartbeat_loop(hb_dir, interval=0.2))
    await asyncio.sleep(0.5)                      # 模拟一次长时间 LLM await
    seen = state.last_heartbeat
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    return seen


_hb_seen = asyncio.run(_hb_test())
from datetime import datetime, timezone
check("后台心跳在 await 期间刷新", bool(_hb_seen), "心跳未更新内存值")
if _hb_seen:
    _age = (datetime.now(timezone.utc)
            - datetime.strptime(_hb_seen, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            ).total_seconds()
    check("心跳时间足够新鲜（供前端僵死告警）", _age < 5, f"age={_age:.1f}s")
check("心跳不再重写 progress.json（避免每 15s 全量写放大）",
      hb_dir.stat().st_mtime_ns == _before_mtime, "progress.json 被心跳改动了")
data = json.loads(hb_dir.read_text(encoding="utf-8"))
check("心跳不破坏其它字段", data["stitch_anchors"] == ["x"], str(data))
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
    b = _load_batches(chs, over_dir, body_token_limit=4000)
    check("超长章组放行而非硬失败（不误杀可用配置）", b == [["1"]], str(b))
except RuntimeError as e:
    check("超长章组放行而非硬失败（不误杀可用配置）", False, str(e))
_log = caplog_text()
check("超长章组仍有告警", "超过单批上限" in _log, _log[:160])

# ---------- 14b. token 预算：必须扣脚手架，且中文按 1 字≈1 token ----------
from utils.tokens import estimate_tokens
from core.budget import body_token_budget, output_token_reserve, blueprint_token_allowance

_ctx = 16384
_b = body_token_budget(_ctx, author_style="冷峻克制")
check("正文预算为正且小于上下文", 0 < _b < _ctx, str(_b))
# 关键回归：正文 + 脚手架 + 输出预留 不得越过上下文
_scaffold = blueprint_token_allowance(_ctx) + 600 + estimate_tokens("冷峻克制")
check("正文+脚手架+输出预留 ≤ 上下文",
      _scaffold + _b + output_token_reserve(_b) <= _ctx,
      f"scaffold={_scaffold} body={_b} out={output_token_reserve(_b)}")
# 旧模型直接对「字号上限」发请求；这里验证：上下文越小，正文预算越小（自适应）
check("上下文越小正文预算越小",
      body_token_budget(8000) < body_token_budget(65536),
      f"{body_token_budget(8000)} vs {body_token_budget(65536)}")
# 已知蓝图时按实测扣减：蓝图越长，正文预算越小
check("蓝图越长正文预算越小",
      body_token_budget(_ctx, blueprint_text="蓝" * 4000) < body_token_budget(_ctx, blueprint_text="蓝" * 500))
# 下限保护：极小上下文也不会算出 0/负数
check("极小上下文仍有正预算", body_token_budget(100) >= 200, str(body_token_budget(100)))

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

# ---------- 分隔符契约：提示词必须写明章数，且重试必须带纠正信息 ----------
from core.refactor import _build_refactor_prompt, _break_feedback

_p4 = _build_refactor_prompt("full_rewrite", "BP", "风格", "正文", n_chapters=4)
check("提示词写明本批共 4 章", "共 **4 章**" in _p4, _p4[-400:])
check("提示词写明需要 3 个分隔符", "**3 个**" in _p4, _p4[-400:])
check("提示词要求保留原章节标题", "原样保留在正文里" in _p4)
_p1 = _build_refactor_prompt("full_rewrite", "BP", "风格", "正文", n_chapters=1)
check("单章提示词明确禁止输出分隔符", "不要输出 `===CHAPTER_BREAK===`" in _p1, _p1[-300:])
for _m in ("fidelity", "fix_gaps", "reskin"):
    _pk = _build_refactor_prompt(_m, "BP", "风格", "正文", n_chapters=5)
    check(f"{_m} 档位也写明章数与分隔符数", "共 **5 章**" in _pk and "**4 个**" in _pk)

_exc = ChapterBreakMissingError(
    "重构输出 ===CHAPTER_BREAK=== 数量不符：需要 3 个，实际 5 个（切出 6 段，期望 4 段）。请重试。")
_pl = {"model": "m", "temperature": 0.8,
       "messages": [{"role": "user", "content": "原始提示词"}]}
_break_feedback(4)(_pl, _exc)
check("重试注入了纠正提示", len(_pl["messages"]) == 2, str(len(_pl["messages"])))
_fb = _pl["messages"][-1]["content"]
check("纠正提示指出需要的个数", "只需要 3 个" in _fb, _fb[:160])
check("纠正提示指出多了几个", "多了 2 个" in _fb, _fb[:160])
check("纠正提示给出方向（删除）", "删除多余" in _fb, _fb[:160])
check("纠正提示提醒保留原标题", "原样保留在正文里" in _fb, _fb[:200])
check("纠错轮降低了温度", _pl["temperature"] == 0.5, str(_pl["temperature"]))
check("原始提示词未被破坏", _pl["messages"][0]["content"] == "原始提示词")

_pl2 = {"model": "m", "temperature": 0.3, "messages": [{"role": "user", "content": "p"}]}
_break_feedback(4)(_pl2, ChapterBreakMissingError("需要 3 个，实际 1 个"))
check("补齐方向的纠正提示", "补上缺少的 2 个" in _pl2["messages"][-1]["content"],
      _pl2["messages"][-1]["content"][:160])
check("低温时纠错轮不再升高", _pl2["temperature"] == 0.3, str(_pl2["temperature"]))

# validate 钩子必须真的把「解析」包进重试循环。
# 历史缺陷：拆分发生在 call_with_retry **之后**，ChapterBreakMissingError
# 进不了 except 分支 → 分隔符数量错一次就直接整批报废，从不重试。
import core.api_client as AC

_calls = {"n": 0}
_real_stream = AC._stream_call


async def _fake_stream(client, api_url, payload, sse_emit_fn, run_id):
    _calls["n"] += 1
    # 3 章 → 正常需 2 个分隔符。首次返回 3 个（中间多一段），第二次收敛为 2 个。
    if _calls["n"] == 1:
        return ("A\n\n===CHAPTER_BREAK===\n\nB\n\n===CHAPTER_BREAK===\n\n多出来的段\n\n===CHAPTER_BREAK===\n\nC",
                {"total_tokens": 1})
    return ("A\n\n===CHAPTER_BREAK===\n\nB\n\n===CHAPTER_BREAK===\n\nC", {"total_tokens": 1})


AC._stream_call = _fake_stream
_fired = []
_pl3 = {"model": "m", "temperature": 0.8, "messages": [{"role": "user", "content": "p"}]}
_res = asyncio.run(AC.call_with_retry(
    None, "http://x", _pl3, None, "run",
    on_retry=_break_feedback(2),
    validate=lambda t: _split_refactored_by_chapters(t, ["a.txt", "b.txt", "c.txt"])))
check("validate 失败会触发重试", _calls["n"] == 2, f"调用 {_calls['n']} 次")
check("重试后拿到合规结果", _res[0] == "A\n\n===CHAPTER_BREAK===\n\nB\n\n===CHAPTER_BREAK===\n\nC", repr(_res[0][:60]))
check("重试期间注入了纠正提示", len(_pl3["messages"]) == 2, str(len(_pl3["messages"])))

# validate 连续失败 → 抛出契约错误，不静默放行
_calls["n"] = 0


async def _bad2(client, api_url, payload, sse_emit_fn, run_id):
    _calls["n"] += 1
    return "总共零个分隔符但硬塞了四段内容在这里", {"total_tokens": 1}


AC._stream_call = _bad2
try:
    asyncio.run(AC.call_with_retry(
        None, "http://x", {"messages": []}, None, "run",
        validate=lambda t: _split_refactored_by_chapters(t, ["a.txt", "b.txt"])))
    check("validate 持续失败时报错", False, "未抛异常")
except ChapterBreakMissingError:
    check("validate 持续失败时报错", True)
check("契约错误最多重试 1 次后放弃", _calls["n"] == 2, f"调用 {_calls['n']} 次")

AC._stream_call = _real_stream

# ---------- 章节识别：9 种真实排版必须命中，正文提及不得误报 ----------
from core.splitter import detect_chapters

_CN = ['一', '二', '三', '四']
_detect_cases = {
    "行首标准":        "\n".join(f"第{n}章 初入江湖" for n in _CN),
    "全角空格缩进":    "\n".join(f"\u3000\u3000第{n}章 初入" for n in _CN),
    "半角空格缩进":    "\n".join(f"  第{n}章 初入" for n in _CN),
    "Tab 缩进":        "\n".join(f"\t第{n}章 初入" for n in _CN),
    "全角数字":        "\n".join(f"第{n}章 初入" for n in ["\uff11", "\uff12", "\uff13", "\uff14"]),
    "廿卅异体数字":    "\n".join(f"第{n}章 初入" for n in ["廿一", "廿二", "廿三", "廿四"]),
    "标点同段分隔":    "".join(f"第{n}章初入江湖。" for n in _CN),
    "引号「」":        "\n".join(f"\u300c第{n}章\u300d初入" for n in _CN),
    "方头括号【】":    "\n".join(f"\u3010第{n}章\u3011初入" for n in _CN),
    "全角括号（）":    "\n".join(f"\uff08第{n}章\uff09初入" for n in _CN),
    "半角括号()":      "\n".join(f"(第{n}章)初入" for n in _CN),
    "数字与单位夹空格": "\n".join(f"第 {n} 章 初入" for n in _CN),
    "多空格":          "\n".join(f"第  {n}  章 初入" for n in _CN),
    "无标题":          "\n".join(f"第{n}章" for n in _CN),
    "===装饰线===":    "\n".join(f"===第{n}章===" for n in _CN),
    "***装饰线***":    "\n".join(f"***第{n}章***" for n in _CN),
    "### 井号标题":    "\n".join(f"### 第{n}章" for n in _CN),
    "──破折线──":     "\n".join(f"\u2500\u2500第{n}章\u2500\u2500" for n in _CN),
    "破折号分隔":      "\n".join(f"第{n}章——初入" for n in _CN),
    "无分隔紧接":      "\n".join(f"第{n}章初入" for n in _CN),
    "回卷篇折多单位":  "\n".join(f"第{n}{u} 初入" for n, u in zip(_CN, ["回", "卷", "篇", "折"], strict=False)),
    "英文 Chapter":    "\n".join(f"Chapter {i} Title" for i in range(1, 5)),
}
for _name, _text in _detect_cases.items():
    _ms, _mode = detect_chapters(_text)
    check(f"章节识别·{_name}（{_mode}）", len(_ms) == 4, f"命中 {len(_ms)} 个")

# 分卷同号：第一卷/第二卷 下各有一个「第一章」——**不能**被去重折叠（会在丢章）
_vol = ("第一卷 风起\n第一章 初入\n第二章 再入\n"
        "第二卷 云涌\n第一章 归来\n第二章 重逢\n")
_ms, _mode = detect_chapters(_vol)
check("分卷同号不折叠（6 章）", len(_ms) == 6 and _mode == "cjk-line",
      f"{len(_ms)} mode={_mode}")
# 分卷+章号写在同一行：「第1卷 第1章」整体算一个标题
_vol2 = "\n".join(f"第{v}卷 第{c}章 初入" for v, c in [("一", "一"), ("一", "二"), ("二", "一"), ("二", "二")])
check("同行分卷+章号识别为 4 章", len(detect_chapters(_vol2)[0]) == 4,
      str(len(detect_chapters(_vol2)[0])))

# 正文里提及「第X章」不得被当成标题（行首优先）
_noise_cases = [
    "他翻开第一章，读到第三页便睡着了。",
    "本书分为第一卷与第二卷两部分内容。",
    "所谓的第三节运动会已经过去了。",
    "这是第一章节拍子，并非章节标题。",
    "章节一共三十七节，每节三千字左右。",
    "我见过第二章 写的这段话，但这不是标题。",
    "他们聊起第五回 合区的旧事。",
    "按第二卷 的设定，这里应当有转折。",
]
for _t in _noise_cases:
    _ms, _ = detect_chapters(_t)
    check(f"正文提及不误报：{_t[:14]}…", len(_ms) == 0, f"误报 {len(_ms)} 个")

# 行中提及 + 行首真标题混排：只能认出真标题
_mixed = ("他翻开第一章，读到第三页便睡着了。\n"
          "第一章 初入江湖\n正文……\n"
          "他又想起第二章 的伏笔。\n"
          "第二章 再入风尘\n正文……\n")
_ms, _mode = detect_chapters(_mixed)
check("正文提及与真标题混排只认真标题", len(_ms) == 2 and _mode.endswith("-line"),
      f"命中 {len(_ms)} mode={_mode}")

# 宽松模式（整本无换行）仍按文本去重：同一行重复的「第X章」提及只算一次
_loose = "第一章初入江湖。第二章初入江湖。第三章初入江湖。"
check("无换行时按文本去重", len(detect_chapters(_loose)[0]) == 3,
      str(len(detect_chapters(_loose)[0])))

# 识别不出时返回 none，调用方据此显式告警（不再静默兜底）
_ms, _mode = detect_chapters("这是一段没有任何章节标题的连续文本，只有正文内容。")
check("无标题文本返回 none", _ms == [] and _mode == "none", _mode)

# ---------- 特殊章节名（无编号）也要能单独识别 ----------
_special = {
    "编号+尾声+番外": ("第一章 初\n正文\n第二章 再\n正文\n尾声\n正文\n番外一\n正文\n番外二\n正文\n", 5),
    "楔子+编号+尾声": ("楔子\n正文\n第一章 初\n正文\n尾声\n正文\n", 3),
    "纯特殊名":       ("楔子\n正文\n正文\n尾声\n正文\n", 2),
}
for _name, (_t, _exp) in _special.items():
    _ms, _mode = detect_chapters(_t)
    check(f"特殊章节名·{_name}", len(_ms) == _exp and _mode.endswith("-line"),
          f"命中 {len(_ms)}/{_exp} mode={_mode}")
# 「序幕布」这类含特殊名的普通词不得被当标题
check("含特殊名的普通词不误报", len(detect_chapters("序幕布在舞台上缓缓拉开。\n")[0]) == 0,
      str(len(detect_chapters("序幕布在舞台上缓缓拉开。\n")[0])))

# ---------- AI 辅助拆章：候选行 / 置信度 / 锚点切割 / 人工标记 ----------
from core.chapter_ai import (extract_candidate_lines, assess_confidence,
                             split_by_line_anchors, split_by_markers)

_ai_book = ("楔子\n楔子正文，交代背景。\n\n"
            "风起于青萍之末\n这一章没有编号。\n正文继续。\n\n"
            "第二章 兵临城下\n正文内容。\n\n"
            "尾声\n一切归于平静。\n")
_cands = extract_candidate_lines(_ai_book)
_cand_texts = [s for _, s in _cands]
check("候选行包含无编号标题", "风起于青萍之末" in _cand_texts, str(_cand_texts))
check("候选行包含编号/特殊名", "楔子" in _cand_texts and "尾声" in _cand_texts, str(_cand_texts))
check("候选行排除长正文", all(len(s) <= 48 for s in _cand_texts), str(_cand_texts))

# 置信度：无标题 / 长度极不均匀 → low；正常 → high
_ms0, _ = detect_chapters("只有正文，没有标题。")
check("无标题→低置信", assess_confidence("只有正文，没有标题。", _ms0, "none")["level"] == "low")
_long = "第一章 初\n" + "字" * 30000 + "\n第二章 再\n短\n"
_msL, _mL = detect_chapters(_long)
check("章节长度极不均匀→低置信", assess_confidence(_long, _msL, _mL)["level"] == "low",
      assess_confidence(_long, _msL, _mL)["reason"])
_normal = "\n".join("第%s章 %s" % (n, "字" * 3000) for n in "一二三四")
_msN, _mN = detect_chapters(_normal)
check("正常书→高置信", assess_confidence(_normal, _msN, _mN)["level"] == "high")
# 编号回退（正文误命中）→ 低置信
_back = "第一章 初\n" + "字" * 300 + "\n第五章 跳\n" + "字" * 300 + "\n第三章 回\n" + "字" * 300 + "\n"
_msB, _mB = detect_chapters(_back)
check("编号非单调→低置信", assess_confidence(_back, _msB, _mB)["level"] == "low",
      assess_confidence(_back, _msB, _mB)["reason"])

# 锚点切割：首个锚点之前的正文归入「序章」，一字不丢
_segs = split_by_line_anchors(_ai_book, [1, 4, 8, 11])
check("锚点切割含序章+3章", len(_segs) == 4, str([t for t, _ in _segs]))
check("锚点切割不丢正文",
      all(k in "".join(b for _, b in _segs) for k in ("楔子正文", "风起于青萍之末", "兵临城下", "一切归于平静")),
      str([t for t, _ in _segs]))
# 越界/非法行号被忽略，不崩
check("非法锚点被忽略", split_by_line_anchors(_ai_book, [0, 99999]) == [],
      str(split_by_line_anchors(_ai_book, [0, 99999])))

# 人工标记：精确匹配（不能用前缀，否则「楔子正文…」会被误当章起点）
_mseg = split_by_markers(_ai_book, ["楔子", "第二章 兵临城下", "尾声"])
check("人工标记精确匹配切出 3 段", len(_mseg) == 3, str([t for t, _ in _mseg]))
check("人工标记不因前缀误切",
      all("楔子正文" not in t for t, _ in _mseg), str([t for t, _ in _mseg]))
check("人工标记为空返回空", split_by_markers(_ai_book, []) == [])

# ---------- 输出目录管理：inspect / clear（白名单，绝不误删用户文件）----------
from routes.output import output_inspect, output_clear, ARTIFACTS

state.pipeline_running = False
_od = Path(tempfile.mkdtemp(prefix="tf_out_"))
for _n in ("01_summaries", "02_workspace", "03_final", "_diagnostics", "_versions", "_backup"):
    (_od / _n).mkdir()
(_od / "progress.json").write_text(json.dumps({
    "current_phase": "phase3-1", "novel_name": "测试书",
    "chapters": [1, 2, 3],
    "batches": [{"status": "phase3_done"}, {"status": "phase1_done"}],
    "error_logs": [{"kind": "api_error", "code": "500", "message": "boom-injected"}],
}, ensure_ascii=False), encoding="utf-8")
(_od / "Story_Bible.md").write_text("bp", encoding="utf-8")
(_od / "我的私人笔记.txt").write_text("千万别删我", encoding="utf-8")  # 用户放在同目录的文件

_info = asyncio.run(output_inspect(str(_od)))
check("inspect 识别为可续跑", _info["resumable"] is True, str(_info))
check("inspect 报告章节与批次进度",
      _info["total_chapters"] == 3 and _info["batches_done"] == 1 and _info["batches_total"] == 2,
      str(_info))
check("inspect 带出失败原因",
      bool(_info["error_logs"]) and "boom-injected" in _info["error_logs"][-1]["message"],
      str(_info["error_logs"]))

_res = asyncio.run(output_clear({"output_path": str(_od)}))
check("clear 删除自身全部产物", set(ARTIFACTS).issubset(set(_res["removed"])), str(_res))
check("clear 绝不误删用户的无关文件", (_od / "我的私人笔记.txt").exists(), "用户文件被误删")
check("clear 后不再可续跑", asyncio.run(output_inspect(str(_od)))["resumable"] is False)
# 清空不存在的目录不报错
check("clear 空目录安全",
      asyncio.run(output_clear({"output_path": str(_od / "nope")}))["status"] == "nothing")
# 危险目录必须被拒绝（文件系统根 / 主目录 / 桌面）
from fastapi import HTTPException as _HTTPExc
for _name, _danger in (("文件系统根", Path(Path.home().anchor)),
                       ("用户主目录", Path.home())):
    try:
        asyncio.run(output_clear({"output_path": str(_danger)}))
        check(f"clear 拒绝危险目录：{_name}", False, "未拒绝")
    except _HTTPExc:
        check(f"clear 拒绝危险目录：{_name}", True)
# 运行中不可清空
state.pipeline_running = True
try:
    asyncio.run(output_clear({"output_path": str(_od)}))
    check("运行中拒绝清空", False, "未拒绝")
except _HTTPExc:
    check("运行中拒绝清空", True)
state.pipeline_running = False
shutil.rmtree(_od, ignore_errors=True)

# 续跑配置漂移检测 ----------
from routes.pipeline_runner import _config_drift

class _Req:
    def __init__(s, **kw): s.__dict__.update(kw)

_base = {"api_config": {"model": "m1", "api_url": "http://a/v1/chat/completions",
                        "context_window": 8000},
         "input_path": "/in/a.txt"}
check("配置一致时无漂移", _config_drift(_base, _Req(model="m1", api_url="http://a/v1/chat/completions",
                                                context_window=8000, input_path="/in/a.txt")) == [])
_d1 = _config_drift(_base, _Req(model="m2", api_url="http://a/v1/chat/completions",
                                context_window=8000, input_path="/in/a.txt"))
check("换模型被检出", len(_d1) == 1 and "模型" in _d1[0], str(_d1))
_d2 = _config_drift(_base, _Req(model="m1", api_url="http://a/v1/chat/completions",
                                context_window=32768, input_path="/in/a.txt"))
check("换上下文窗口被检出", len(_d2) == 1 and "上下文" in _d2[0], str(_d2))
_d3 = _config_drift(_base, _Req(model="m1", api_url="http://a/v1/chat/completions",
                                context_window=8000, input_path="/in/b.txt"))
check("换输入路径被检出", len(_d3) == 1 and "输入" in _d3[0], str(_d3))
_d4 = _config_drift(_base, _Req(model="m9", api_url="http://b/v1/chat/completions",
                                context_window=32768, input_path="/in/b.txt"))
check("多项漂移全部检出", len(_d4) == 4, str(_d4))

# ---------- 重新拆分必须清掉按旧章节绑定的下游产物 ----------
# 场景：跑了一半（有批次/重构稿/缝合锚点）→ 用户在预览门改用自定义标记重拆。
# 若不清，下游会拿着旧章节 id 去改新章节文件（缝合错位）。
from core.splitter import resplit_single_file

class _FakeReq:
    input_mode = "single_file"; input_format = "txt"
    def __init__(s, inp, outp):
        s.input_path = inp; s.output_path = outp
        s.context_window = 8000; s.chapter_markers = ["◆ 甲 ◆", "◆ 乙 ◆"]
        s.ai_split = False; s.author_style = ""; s.refactor_mode = "full_rewrite"

_rd = Path(tempfile.mkdtemp(prefix="tf_rs_"))
(_rd / "in").mkdir(); (_rd / "out").mkdir()
_src = _rd / "in" / "book.txt"
_pad = "正文内容，需要足够长以免被判为空章。" * 4
_src.write_text("开场白。" + _pad + "\n\n◆ 甲 ◆\n" + _pad + "\n\n◆ 乙 ◆\n" + _pad + "\n", encoding="utf-8")
_out = _rd / "out"
# 造出「上一次跑到一半」的遗留产物
(_out / "02_workspace" / "reconstructed").mkdir(parents=True)
(_out / "02_workspace" / "reconstructed" / "chapter_99.txt").write_text("旧章", encoding="utf-8")
(_out / "02_workspace" / "snapshots" / "drafts").mkdir(parents=True)
(_out / "01_summaries").mkdir(parents=True)
(_out / "01_summaries" / "batch_01.txt").write_text("旧摘要", encoding="utf-8")
(_out / "03_final").mkdir(parents=True)
(_out / "03_final" / "成品.docx").write_text("旧成品", encoding="utf-8")
_prog = {"chapters": [{"id": "99"}], "batches": [{"batch_id": 1, "chapter_ids": ["99"], "status": "phase3_done"}],
         "completed_files": ["x"], "processed_batches": [1], "cross_batch_virtual": [{"a": 1}],
         "stitch_anchors": [{"b": 2}], "stitch_virtual_done": ["9"], "stitch_batch_done": ["k"],
         "virtual_chapter_map": [], "current_phase": "phase0_confirm"}
_pp = _rd / "out" / "progress.json"
_pp.write_text(json.dumps(_prog, ensure_ascii=False), encoding="utf-8")

asyncio.run(resplit_single_file(_FakeReq(str(_src), str(_out)), _prog, _pp, "run-test"))
_pr2 = json.loads(_pp.read_text(encoding="utf-8"))
check("重拆后章节数正确", len(_prog["chapters"]) == 3, str(len(_prog["chapters"])))
check("重拆作废旧批次", _prog["batches"] == [], str(_prog["batches"]))
check("重拆作废已完成文件记录", _prog["completed_files"] == [] and _prog["processed_batches"] == [])
check("重拆作废缝合锚点/游标",
      _prog["stitch_anchors"] == [] and _prog["stitch_virtual_done"] == [] and _prog["stitch_batch_done"] == [])
check("重拆清掉旧重构稿（避免缝合改错文件）",
      not (_out / "02_workspace" / "reconstructed" / "chapter_99.txt").exists(),
      "旧 chapter_99.txt 仍在")
check("重拆清掉旧摘要/快照",
      not (_out / "01_summaries" / "batch_01.txt").exists()
      and not (_out / "02_workspace" / "snapshots" / "drafts").exists())
check("重拆保留上一版成品（03_final 不动）", (_out / "03_final" / "成品.docx").exists())
shutil.rmtree(_rd, ignore_errors=True)

# ---------- progress.json 读取：BOM 容忍 + 读失败绝不静默重建 ----------
from utils.progress_io import read_progress, try_read_progress, ProgressUnreadable

_bd = Path(tempfile.mkdtemp(prefix="tf_bom_"))
_ok = '{"current_phase": "phase3-1", "chapters": [1,2]}'
(_bd / "plain.json").write_text(_ok, encoding="utf-8")
(_bd / "bom.json").write_text(_ok, encoding="utf-8-sig")
(_bd / "broken.json").write_text("{not json", encoding="utf-8")
check("读取无 BOM 正常", read_progress(_bd / "plain.json")["current_phase"] == "phase3-1")
check("读取带 BOM 正常（utf-8-sig）", read_progress(_bd / "bom.json")["current_phase"] == "phase3-1")
check("文件不存在返回 None", try_read_progress(_bd / "none.json") is None)
try:
    try_read_progress(_bd / "broken.json")
    check("损坏文件抛错而非返回空", False, "未抛错")
except ProgressUnreadable:
    check("损坏文件抛错而非返回空", True)

# _load_or_init_progress：读失败必须停下，绝不能覆盖用户进度
from routes.pipeline_runner import _load_or_init_progress as _loadp
_od2 = Path(tempfile.mkdtemp(prefix="tf_ld_"))
(_od2 / "progress.json").write_text("{corrupt", encoding="utf-8")
_before = (_od2 / "progress.json").read_text(encoding="utf-8")
class _Rq:
    input_mode = "single_file"; input_path = "x"; output_path = str(_od2)
    context_window = 8000; model = "m"; api_url = "http://a/v1/chat/completions"
try:
    _loadp(_Rq(), "rid", _od2 / "progress.json")
    check("读失败时拒绝启动（不覆盖进度）", False, "竟然继续了")
except RuntimeError as e:
    check("读失败时拒绝启动（不覆盖进度）", "progress.json" in str(e), str(e)[:100])
check("读失败后原文件未被覆盖", (_od2 / "progress.json").read_text(encoding="utf-8") == _before)
shutil.rmtree(_bd, ignore_errors=True); shutil.rmtree(_od2, ignore_errors=True)

# ---------- 成品版本：快照 / 列举 / 恢复 / 路径穿越防护 ----------
from core.versions import snapshot as _snap, list_versions as _lv, restore as _rs

_vd = Path(tempfile.mkdtemp(prefix="tf_ver_"))
(_vd / "03_final").mkdir()
(_vd / "03_final" / "book.txt").write_text("v1", encoding="utf-8")
_v1 = _snap(_vd, "我的书")
check("成品快照成功", bool(_v1) and (_vd / "_versions" / _v1).is_dir(), str(_v1))
(_vd / "03_final" / "book.txt").write_text("v2", encoding="utf-8")
check("空目录不产生快照", _snap(Path(tempfile.mkdtemp()) / "none", "x") is None)
_v2 = _snap(_vd, "我的书")
_vl = _lv(_vd)
check("版本列表按时间倒序", len(_vl) == 2 and _vl[0]["id"] >= _vl[1]["id"], str([v["id"] for v in _vl]))
_rs(_vd, _v1)
check("恢复到指定版本", (_vd / "03_final" / "book.txt").read_text(encoding="utf-8") == "v1",
      (_vd / "03_final" / "book.txt").read_text(encoding="utf-8")[:20])
check("恢复前先给当前版本留快照（可再回退）", len(_lv(_vd)) == 3, str(len(_lv(_vd))))
for _bad in ("../../etc", "a/b", "..", ""):
    try:
        _rs(_vd, _bad)
        check(f"拒绝非法版本号 {_bad!r}", False, "被接受")
    except (ValueError, FileNotFoundError):
        check(f"拒绝非法版本号 {_bad!r}", True)
shutil.rmtree(_vd, ignore_errors=True)

# ---------- 任务历史注册表 ----------
import routes.history as _H
_real_appdata = os.environ.get("APPDATA")
_hist_dir = tempfile.mkdtemp(prefix="tf_hreg_")
os.environ["APPDATA"] = _hist_dir          # 绝不污染开发者真实 %APPDATA%
try:
    _H.upsert(r"C:\books\a", {"novel_name": "书A", "current_phase": "phase3-1",
                              "total_chapters": 10, "batches_done": 3, "batches_total": 4,
                              "last_error": "炸了"})
    _H.upsert(r"C:\books\b", {"novel_name": "书B", "finished": True, "current_phase": "phase4"})
    _items = _H._load()["entries"]
    check("历史按输出路径登记两条", len(_items) == 2, str(list(_items)))
    _a = _items[os.path.normcase(os.path.abspath(r"C:\books\a"))]
    check("历史记录批次进度", _a and _a["batches_done"] == 3 and _a["batches_total"] == 4, str(_a))
    check("历史记录失败原因", _a and _a.get("last_error") == "炸了", str(_a))
    # 同一路径再次 upsert 是更新而非新增
    _H.upsert(r"C:\books\a", {"batches_done": 4, "clear_error": True})
    _items2 = _H._load()["entries"]
    _a2 = _items2[os.path.normcase(os.path.abspath(r"C:\books\a"))]
    check("重复登记为更新不新增", len(_items2) == 2 and _a2["batches_done"] == 4, str(len(_items2)))
    check("clear_error 清掉旧失败原因", "last_error" not in _a2, str(_a2.get("last_error")))
finally:
    if _real_appdata is not None:
        os.environ["APPDATA"] = _real_appdata
    else:
        # Linux/macOS 上本来就没有 APPDATA 键，必须删掉临时注入的值，
        # 否则后面的「未注入时退回默认目录」用例读到的是测试目录。
        os.environ.pop("APPDATA", None)
    shutil.rmtree(_hist_dir, ignore_errors=True)

# ---------- 本地 API 调用方鉴权 ----------
# 后端绑 127.0.0.1 随机端口，但读一下 port.json 就能调接口：
# /api/start 能指定任意读写路径，/api/system/open 能 os.startfile 任意路径。
# 外壳用 CSPRNG 生成 token 注入 sidecar（从不落盘），请求必须带 X-TextForge-Token。
import utils.auth as _AUTH
from fastapi.testclient import TestClient as _TC2
from app import app as _app2

_real_tok = os.environ.get(_AUTH.ENV_KEY)
try:
    # 1) 无 token 且非开发模式 = 生产默认拒绝。
    # 这是安全边界：backend.exe 被手动启动、又没有外壳注入 token 时，
    # 任意本地进程/网页都不能匿名调用 /api/start 读写任意路径。
    os.environ.pop(_AUTH.ENV_KEY, None)
    os.environ.pop("TEXTFORGE_DEV", None)
    check("未配置 token 时默认拒绝", _AUTH.is_enforced() is True, "")
    check("未配置 token 时比对必然失败", _AUTH.check({}) is False, "")
    with _TC2(_app2) as _c:
        check("未配置 token 时 /api/health 仍可探活",
              _c.get("/api/health").status_code == 200,
              str(_c.get("/api/health").status_code))
        check("未配置 token 时业务接口返回 401",
              _c.get("/api/status").status_code == 401,
              str(_c.get("/api/status").status_code))

    # 2) 显式开发模式放行（run_dev.ps1 / e2e 使用）
    os.environ["TEXTFORGE_DEV"] = "1"
    check("开发模式关闭鉴权", _AUTH.is_enforced() is False, "")
    with _TC2(_app2) as _c:
        check("开发模式下正常放行", _c.get("/api/status").status_code == 200,
              str(_c.get("/api/status").status_code))
    os.environ.pop("TEXTFORGE_DEV", None)

    # 3) 注入 token 后必须校验
    os.environ[_AUTH.ENV_KEY] = "s3cret-token-for-test"
    check("配置 token 后鉴权启用", _AUTH.is_enforced() is True, "")
    check("比对逻辑：正确 token 通过", _AUTH.check({_AUTH.HEADER: "s3cret-token-for-test"}))
    check("比对逻辑：头名大小写不敏感（HTTP 头本身不区分大小写）",
          _AUTH.check({"x-textforge-token": "s3cret-token-for-test"}), "")
    check("比对逻辑：错误 token 拒绝", not _AUTH.check({_AUTH.HEADER: "wrong"}))
    check("比对逻辑：缺 token 拒绝", not _AUTH.check({}))
    check("比对逻辑：空 token 拒绝", not _AUTH.check({_AUTH.HEADER: ""}))
    # 前缀相同但整体不同 —— 必须拒绝（防 startswith 式实现漏判）
    check("比对逻辑：token 前缀不得通过", not _AUTH.check({_AUTH.HEADER: "s3cret"}), "")

    with _TC2(_app2) as _c:
        check("无 token 访问业务接口返回 401",
              _c.get("/api/status").status_code == 401,
              str(_c.get("/api/status").status_code))
        check("错误 token 返回 401",
              _c.get("/api/status", headers={_AUTH.HEADER: "nope"}).status_code == 401, "")
        check("/api/health 豁免鉴权（供探活）",
              _c.get("/api/health").status_code == 200, "")
        check("带正确 token 放行",
              _c.get("/api/status", headers={_AUTH.HEADER: "s3cret-token-for-test"}).status_code
              == 200, "")
        # 写操作才是真正的攻击面，必须同样受保护
        check("无 token 的写操作被拒（POST /api/stop）",
              _c.post("/api/stop", json={}).status_code == 401, "")
finally:
    if _real_tok is None:
        os.environ.pop(_AUTH.ENV_KEY, None)
    else:
        os.environ[_AUTH.ENV_KEY] = _real_tok
    if _REAL_DEV is None:
        # 本脚本其余用例仍要通过 TestClient 调 API，继续用开发模式。
        os.environ["TEXTFORGE_DEV"] = "1"
    else:
        os.environ["TEXTFORGE_DEV"] = _REAL_DEV

# ---------- 优雅退出钩子（/api/shutdown） ----------
# 这条路由必须在**随 app 注册的路由表**里，而不是 main.py 的 __main__ 块里：
# 写在那里的版本只在冻结 exe 里存在，源码运行与单测都看不到，重建一旦遗漏就
# 静默 404 —— 表现为关窗后残留孤儿进程，几乎无法定位。
from fastapi.testclient import TestClient as _TC
from app import app as _app

# 关窗清理依赖这个端点：PyInstaller onefile 是父子双进程，外壳 child.kill() 只杀得掉
# 父引导进程，子进程会变孤儿常驻约 70MB 内存且 %TEMP%\_MEI 永不删除。
# 因此必须由后端自己退出（/api/system/shutdown），让引导进程有机会清理临时目录。
#
# 断言**完整路径**：这条路由带 /api/system 前缀，路径写错（比如写成 /api/shutdown）
# 就是 404，表现为「关窗后残留孤儿进程」，根因完全看不出来。
_spec = _app.openapi()["paths"]
check("注册了 /api/system/shutdown（供外壳让后端优雅退出）",
      "/api/system/shutdown" in _spec,
      str([p for p in _spec if "shutdown" in p]))
with _TC(_app) as _c:
    # 流水线未运行时可直接停机
    _r = _c.post("/api/system/shutdown")
    check("POST /api/system/shutdown 返回 stopping",
          _r.status_code == 200 and _r.json().get("status") == "stopping",
          f"{_r.status_code} {_r.text[:80]}")
    check("GET /api/system/shutdown 返回 405（确认命中本路由而非兜底 404）",
          _c.get("/api/system/shutdown").status_code == 405,
          str(_c.get("/api/system/shutdown").status_code))
    # 流水线运行中必须拒绝，避免静默打断正在跑的批次
    _running = state.pipeline_running
    state.pipeline_running = True
    _busy = _c.post("/api/system/shutdown")
    check("流水线运行中拒绝停机（防静默打断批次）",
          _busy.status_code == 409, f"{_busy.status_code} {_busy.text[:60]}")
    _forced = _c.post("/api/system/shutdown", json={"force": True})
    check("force=true 可在运行中强制停机",
          _forced.status_code == 200, f"{_forced.status_code} {_forced.text[:60]}")
    state.pipeline_running = _running

# ---------- 开跑前预估 /api/estimate ----------
# 界面点「预估」走的是 HTTP：pytest 只测了纯函数 estimate_run()，路由这一层
# （StartRequest 反序列化、路径校验、鉴权中间件）必须在这里钉住。
import tempfile as _tf
from pathlib import Path as _P

check("注册了 /api/estimate", "/api/estimate" in _app.openapi()["paths"],
      str([p for p in _app.openapi()["paths"] if "estimate" in p]))

_est_sent = ("他推开门，屋里静得能听见自己的心跳声。" * 40)
with _tf.TemporaryDirectory() as _td, _tf.TemporaryDirectory() as _td2:
    _book = _P(_td) / "book.txt"
    _book.write_text(
        "\n\n".join(f"第{i}章 起事\n{_est_sent}" for i in range(1, 4)), encoding="utf-8")
    _body = {
        "input_mode": "single_file", "input_path": str(_book),
        # 输出目录必须是输入目录的**兄弟**：paths.validate 禁止输出落在输入内部
        "output_path": str(_P(_td2) / "out"), "input_format": "txt",
        "api_url": "http://127.0.0.1:9/v1/chat/completions", "model": "mock-model",
        "context_window": 8000,
    }
    with _TC(_app) as _c:
        _er = _c.post("/api/estimate", json=_body)
        _ej = _er.json() if _er.status_code == 200 else {}
        check("POST /api/estimate 返回批数与调用数",
              _er.status_code == 200 and _ej.get("chapters") == 3
              and _ej.get("calls", {}).get("total", 0) > 0,
              f"{_er.status_code} {_er.text[:200]}")
        check("预估里缝合观测字段齐全",
              {"stitch_boundaries", "stitch_window_chars", "scaffold_share"} <= set(_ej),
              str(sorted(_ej)[:12]))
        # 与 /api/start 同一套校验：格式与扩展名不符必须 422，不能让它跑起来才炸
        _bad = _c.post("/api/estimate", json={**_body, "input_format": "docx"})
        check("预估沿用 start 的路径校验（格式不符 422）",
              _bad.status_code == 422, f"{_bad.status_code} {_bad.text[:80]}")

# ---------- 心跳不再重写 progress.json ----------
# 前端据此判断僵死（pipeline_running 且心跳超 90s 报警），因此心跳必须新鲜；
# 但原实现是每 15s 把整个 progress.json 读出来、改一个字段、再全量写回 + fsync，
# 而 progress.json 在缝合阶段可达数十 MB（锚点内嵌全文）—— 长时段运行下是
# 实打实的写放大。现在心跳只维护内存值，这条断言把这个不变量钉住。
import routes.state as _ST
import routes.status as _S2
import routes.pipeline_runner as _PR
import inspect as _inspect

_hb_src = _inspect.getsource(_PR._heartbeat_loop)
check("心跳循环不再全量重写 progress.json（避免写放大）",
      "atomic_write_json" not in _hb_src, "心跳里又出现了 progress.json 全量写入")
check("心跳不再重读 progress.json（改用内存引用登记历史）",
      "read_text" not in _hb_src and "json.loads" not in _hb_src,
      "心跳里又出现了 progress.json 全量读取")
check("心跳维护内存心跳时间", "state.last_heartbeat" in _hb_src, "")

# status 优先返回内存心跳，其次回落到 progress.json 里的旧值
_status_src = _inspect.getsource(_S2)
check("/api/status 的心跳优先取内存值（界面不会读到陈旧时间）",
      "state.last_heartbeat or progress.get" in _status_src,
      "status 未按「内存优先」返回心跳")
check("内存心跳字段存在且默认 None", hasattr(_ST, "last_heartbeat"), "")

# ---------- 闸门标志的 clear 必须早于阶段落盘 ----------
# 这两个门原先都是「先写 progress.json 再 clear 标志」。于是存在一个窗口：
# progress 已是 phase2_waiting / phase3_accept（界面弹框、/api/status 的 resume
# 都已就绪），而标志还没 clear —— 此刻到达的确认会被紧接着的 clear() 静默抹掉，
# 流水线随后永远等不到，用户界面却显示「已确认」，**无任何报错**。
# 表现为端到端测试偶发卡死在 phase2_waiting / phase3_accept。
import inspect as _insp
import core.pipeline as _PL
import core.refactor as _RF


def _check_order(src, clear_stmt, phase_literal, label):
    """clear 必须出现在「阶段落盘」之前。

    只看 clear 之后那一段：文件里还散布着同名阶段字面量（断点恢复分支会先判断
    current_phase == 'phase2_waiting'），拿第一次出现的位置比较会误判。
    """
    i = src.find(clear_stmt)
    if i < 0:
        check(label, False, "找不到 clear 语句")
        return
    j = src.find(phase_literal, i)
    ok = 0 < i < j
    check(label, ok, f"clear@{i} 其后未找到阶段赋值@{j}")


_check_order(_insp.getsource(_PL), "state.blueprint_confirmed.clear()",
             "progress['current_phase'] = 'phase2_waiting'",
             "蓝图门：clear 早于 phase2_waiting 落盘")
_check_order(_insp.getsource(_RF), "state.accept_confirm.clear()",
             "progress['current_phase'] = 'phase3_accept'",
             "验收门：clear 早于 phase3_accept 落盘")

# ---------- 便携包数据目录（TEXTFORGE_DATA_DIR） ----------
import utils.appdir as _AD
_real_data_dir = os.environ.get(_AD.ENV_KEY)
_portable = tempfile.mkdtemp(prefix="tf_portable_")
try:
    os.environ[_AD.ENV_KEY] = str(Path(_portable) / "data")
    check("TEXTFORGE_DATA_DIR 覆盖数据目录",
          _AD.data_dir() == Path(_portable) / "data", str(_AD.data_dir()))
    # 必须**按调用时**解析：测试要在运行期换值，模块级缓存会把结果钉死
    _second = Path(_portable) / "data2"
    os.environ[_AD.ENV_KEY] = str(_second)
    check("数据目录按调用重新解析（非 import 期缓存）",
          _AD.data_dir() == _second, str(_AD.data_dir()))
    check("ensure_data_dir 会建目录",
          _AD.ensure_data_dir().is_dir(), str(_AD.ensure_data_dir()))
    # 便携包核心不变量：历史 / 日志 / 端口文件必须落在**同一个**目录树下。
    # 三者各算一套路径时，界面会指向一个永远不存在的日志文件、前端等不到端口。
    # 上面日志用例给 S.LOG_PATH 赋过值；模块属性一旦存在就优先于 __getattr__，
    # 必须清掉才能验到「按 appdir 解析」的真实行为。
    for _n in ("APP_DIR", "LOG_DIR", "LOG_PATH", "PORT_FILE"):
        S.__dict__.pop(_n, None)
    check("历史注册表随包迁移",
          _H._registry_path() == _second / "runs.json", str(_H._registry_path()))
    check("日志路径随包迁移",
          S.LOG_PATH == _second / "logs" / "backend.log", str(S.LOG_PATH))
    check("端口文件随包迁移",
          S.PORT_FILE == _second / "port.json", str(S.PORT_FILE))
    del os.environ[_AD.ENV_KEY]
    expected = (Path(_REAL_APPDATA) / "TextForge") if _REAL_APPDATA else (Path.home() / ".textforge")
    check("未注入时退回 %APPDATA%（安装版行为不变）",
          _AD.data_dir() == expected, str(_AD.data_dir()))
finally:
    if _real_data_dir is not None:
        os.environ[_AD.ENV_KEY] = _real_data_dir
    else:
        os.environ.pop(_AD.ENV_KEY, None)
    shutil.rmtree(_portable, ignore_errors=True)

# ---------- 续跑配置漂移检测 ----------

# ---------- token 估算：CJK 启发式 ----------
from utils.tokens import estimate_tokens, estimate_messages_tokens

check("空串估算为 0", estimate_tokens("") == 0)
check("纯中文 1 字≈1 token", estimate_tokens("中文测试") == 4, str(estimate_tokens("中文测试")))
# 100 个汉字 ≈ 100 token
_zh100 = "汉" * 100
check("100 汉字估算≈100", 90 <= estimate_tokens(_zh100) <= 110, str(estimate_tokens(_zh100)))
# 100 个 ASCII 字母 ≈ 25 token
_en = "a" * 100
check("100 英文字母估算≈25", 20 <= estimate_tokens(_en) <= 30, str(estimate_tokens(_en)))
# 关键回归：中文必须显著高于旧模型（旧模型 1.5 字/token，会低估中文字符）
check("中文估算不低于字数（旧模型会低估）", estimate_tokens(_zh100) >= 100, str(estimate_tokens(_zh100)))
_msg = estimate_messages_tokens([{"role": "user", "content": "你好世界"}])
check("messages 估算含模板开销", _msg >= estimate_tokens("你好世界"), str(_msg))

# ---------- 上下文溢出识别：跨厂商报错文案 ----------
from core.api_client import parse_context_overflow

_ov_cases = {
    "LM Studio": ("request (16038 tokens) exceeds the available context size (8448 tokens)", 16038, 8448),
    "OpenAI":    ("This model's maximum context length is 4096 tokens. However, you requested 5000", 5000, 4096),
    "Anthropic": ("prompt is too long: 250000 tokens > 200000 maximum", 250000, 200000),
    "vLLM":      ("This model's maximum context length is 8192 tokens. However, you requested 16038", 16038, 8192),
    "中文网关":  ("请求上下文超出模型上限 8192", None, 8192),
}
for _name, (_txt, _req, _avail) in _ov_cases.items():
    _r = parse_context_overflow(_txt)
    check(f"溢出识别·{_name}", _r is not None and _r[1] == _avail, str(_r))

# 非溢出错误不得误判（误判会导致毫无意义的拆批）
for _name, _txt in {
    "无效模型": "model gpt-foo not found",
    "鉴权失败": "Incorrect API key provided",
    "参数错误": "max_tokens must be positive",
    "流式字段": "unknown parameter stream_options",
}.items():
    check(f"非溢出不误判·{_name}", parse_context_overflow(_txt) is None,
          str(parse_context_overflow(_txt)))

# ---------- 拆批：对半拆、虚拟章不拆散、拆不动返回 None ----------
from core.batching import split_batch_in_progress, chapter_groups

def _mk_progress(chapter_ids):
    chs = [{"id": c, "split_file": f"split/{c}.txt"} for c in chapter_ids]
    return {"chapters": chs, "batches": [
        {"batch_id": 1, "chapter_ids": list(chapter_ids), "status": "phase1_done", "output_files": []}]}

_pg = _mk_progress(["1", "2", "3", "4"])
_pb = _pg["batches"][0]
_new = split_batch_in_progress(_pg, _pb, reason="overflow", detail="x")
check("拆批返回两个新批次", _new is not None and len(_new) == 2, str(_new))
check("拆批后 batches 数变为 2", len(_pg["batches"]) == 2, str(len(_pg["batches"])))
check("首批 2 章、次批 2 章",
      len(_new[0]["chapter_ids"]) == 2 and len(_new[1]["chapter_ids"]) == 2,
      str([b["chapter_ids"] for b in _new]))
check("拆批记录了自适应上限", _pg.get("adaptive_max_chapters") == 2, str(_pg.get("adaptive_max_chapters")))
check("原批次已被替换（batch_id=1 不复存在）",
      all(b["batch_id"] != 1 for b in _pg["batches"]), str([b["batch_id"] for b in _pg["batches"]]))

# 虚拟章（同 original_id）必须留在同一侧，否则缝合还原不回整章
_vpg = {
    "chapters": [
        {"id": "1", "split_file": "split/1.txt"},
        {"id": "2_0", "split_file": "split/2_0.txt", "is_virtual": True, "original_id": "2"},
        {"id": "2_1", "split_file": "split/2_1.txt", "is_virtual": True, "original_id": "2"},
        {"id": "3", "split_file": "split/3.txt"},
    ],
    "batches": [{"batch_id": 1, "chapter_ids": ["1", "2_0", "2_1", "3"],
                 "status": "phase1_done", "output_files": []}],
}
_vnew = split_batch_in_progress(_vpg, _vpg["batches"][0], reason="contract")
_sides = [set(b["chapter_ids"]) for b in _vnew]
check("虚拟章对不拆散（2_0/2_1 在同一批）",
      any({"2_0", "2_1"} <= s for s in _sides), str(_sides))
check("分组函数把虚拟章聚成一组",
      chapter_groups(_vpg["chapters"], ["1", "2_0", "2_1", "3"]) == [["1"], ["2_0", "2_1"], ["3"]],
      str(chapter_groups(_vpg["chapters"], ["1", "2_0", "2_1", "3"])))

# 只剩一个不可拆分组 → 返回 None（调用方据此报错而非死循环）
_sp = _mk_progress(["1"])
check("单章批次拆不动返回 None",
      split_batch_in_progress(_sp, _sp["batches"][0], reason="overflow") is None)

# ---------- 上下文窗口探测：URL 归一 + 深度取值 ----------
from core.context_probe import base_url, _dig

check("base_url 去掉 /v1/chat/completions",
      base_url("http://127.0.0.1:1234/v1/chat/completions") == "http://127.0.0.1:1234",
      base_url("http://127.0.0.1:1234/v1/chat/completions"))
check("base_url 保留端口与 scheme",
      base_url("https://api.x.com/v1/chat/completions") == "https://api.x.com")
check("_dig 取 llama.cpp n_ctx",
      _dig({"default_generation_settings": {"n_ctx": 8192}}) == 8192)
check("_dig 取 LM Studio max_context_length",
      _dig({"data": [{"id": "m", "max_context_length": 4096}]}) == 4096)
check("_dig 取 Ollama model_info",
      _dig({"model_info": {"llama.context_length": 32768}}) == 32768)
check("_dig 忽略过小值（噪声）", _dig({"max_tokens": 8}) is None, str(_dig({"max_tokens": 8})))

print()
if _FAILS:
    print(f"=== {len(_FAILS)} FAILED: {_FAILS}")
    sys.exit(1)
print("=== 全部通过 ===")
