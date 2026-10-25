import asyncio, re, hashlib, unicodedata, logging, json, shutil
from collections import deque
from pathlib import Path
from docx import Document
from utils.atomic import atomic_write_text, atomic_write_json
from utils.sort import natural_sort_key
from utils.tokens import estimate_tokens
from core.batching import pack_groups
from core.budget import MAX_CHAPTERS_PER_BATCH, OVERLAP_RATIO, window_char_capacity
from routes import state
from routes.events import sse_emit
import chardet

logger = logging.getLogger("textforge")

# ---------------------------------------------------------------------------
# 章节识别
# ---------------------------------------------------------------------------
# 历史事故：旧模式是 r'^第[零一二三四五六七八九十百千万0-9两]+[章回节卷]...'，
# `^` 强制行首 —— 而中文网文/Word 导出的章节标题**绝大多数带缩进**（全角空格、
# 半角空格、Tab），于是 9 种常见排版里 7 种一个标题都识别不出来。
# 识别失败会静默走到「每 3000 字切一段」的兜底，凭空造出与原著不符的章节结构，
# 用户拿到的是结构被改写的书，却没有任何提示。53 章被拆成 28 章就是这么来的。
#
# 新规则：标题可位于行首，或紧跟句末标点（标题与正文同段）；容忍各种缩进、
# 引号包裹、全角数字与廿/卅/卌等异体数字。
_CJK_NUM = r'零一二三四五六七八九十百千万亿两廿卅卌〇壹贰叁肆伍陆柒捌玖拾佰仟'
_NUMCHR = rf'{_CJK_NUM}0-9\uff10-\uff19'
_NUM = f'[{_NUMCHR}]'
# 水平空白（不含换行）。**必须排除换行**：
#  - 行首缩进若含 \n，命中起点会落到上一行末尾的换行符上，行首判定失效；
#  - 标题尾部若用 \s，会把「===第一章===\n===第二章===」这类装饰线标题
#    跨行吞成一段（实测每两章只识别出一章）。
_SP_CH = ' \t\u3000\u00a0'
_SP = f'[{_SP_CH}]'
_INDENT = f'[{_SP_CH}\\f\\v\\ufeff\\u200b-\\u200d]*'
# 序号部分：数字之间/数字与单位之间允许夹水平空格（"第 三 章"、"第 一百 二十 章"）
_NUMSEQ = rf'(?:{_NUM}{_SP}*)+'
# 可选中英文/全角/方头括号包裹：`「」『』《》〈〉【】（）""''()[]［］〔〕`
_Q_OPEN_STR = '\u300c\u300e\u300a\u3008\u3010\uff08\uff3b\u3014\u201c\u2018"\'(['
_Q_CLOSE_STR = '\u300d\u300f\u300b\u3009\u3011\uff09\uff3d\u3015\u201d\u2019"\')]'
_Q_OPEN = '[' + re.escape(_Q_OPEN_STR) + ']'
_Q_CLOSE = '[' + re.escape(_Q_CLOSE_STR) + ']'
# 标题前后的装饰线（"===第三章===" / "***第三章***" / "### 第三章" / "──第三章──"）
_DECO_CH = r'=＝\-—–─*＊#＃~～·•'
_DECO = f'[{re.escape(_DECO_CH)}]'
_UNITS = r'章回节卷篇集折'
_MARK = rf'第{_SP}*{_NUMSEQ}[{_UNITS}]'
# 标题主体：允许「第N卷 第N章」这种分卷+章号写在同一行 —— 否则行中的「第N章」
# 不在行首会被漏掉，导致整本书只剩「第N卷」被识别（实测 4 章变 2 章）。
_BODY = rf'{_MARK}(?:{_SP}+{_MARK})?'
# 标题序号之后允许的分隔符（含装饰线，覆盖 "第三章===" 这种右装饰）
_SEP_CH = _SP_CH + r'\uFF1A:\-—–、·' + _DECO_CH
_SEP = f'[{re.escape(_SEP_CH)}]+'
# 标题序号之后的合法收尾，二选一：
#   a) 跟分隔符（"第一章：初入" / "第一章 初入" / "第三章==="）
#   b) 直接跟标题文字（"第一章初入江湖"），但**后面不能再是量词或数字** ——
#      这条负向断言是为了挡 "这是第一章节拍子，并非章节标题" 这类正文误报。
# 注意：两条分支必须**整体**包在一个 (?:...) 里再拼接。少写这层括号会让 `|`
# 提升到整个正则的最外层，负向断言那条变成能在任意位置空匹配的独立分支 ——
# 表现为每个标题匹配出一堆空串、且正文全被误判成标题。
_HEAD_END = rf'(?:{_Q_CLOSE}?{_SEP}|(?![{_NUMCHR}{_UNITS}]))'
_HEADING = rf'(?:{_DECO}{_SP}*)*{_Q_OPEN}?{_BODY}{_HEAD_END}'

CHAPTER_PATTERN = re.compile(
    # (1) 行首，或 (2) 紧跟句末标点 —— 后者覆盖「上一章正文末尾。下一章标题」挤在一行
    rf'(?:^|(?<=[\u3002\uff01\uff1f!?\uff1b;\u2026]))'
    rf'{_INDENT}{_HEADING}',
    re.MULTILINE,
)

# 英文/数字章节（次选，仅当中文模式几乎没命中时才启用，避免误抓正文里的 "Chapter"）
_EN_PATTERN = re.compile(
    rf'(?:^|(?<=[\u3002\uff01\uff1f!?\uff1b;\u2026]))'
    rf'{_INDENT}(?:{_DECO}{_SP}*)*{_Q_OPEN}?'
    rf'(?:Chapter|CHAPTER|Part|PART|Book|BOOK){_SP}*[0-9]+{_HEAD_END}',
    re.MULTILINE,
)

# 无编号的特殊章节名：楔子/序章/尾声/番外/间章… —— 这类写法完全不含「第X章」，
# 规则枚举永远列不全创新说法，但常见的一批必须先覆盖（零成本）。
_SPECIAL_NAMES = (r'序章|序言|自序|序|楔子|引子|引言|前言|序幕|尾声|终章|尾章|终篇|终幕'
                  r'|番外(?:篇|章)?|外传|间章|后记|跋|附录|完本感言|作者的话')
_SPECIAL_PATTERN = re.compile(
    rf'(?:^|(?<=[\u3002\uff01\uff1f!?\uff1b;\u2026]))'
    rf'{_INDENT}(?:{_DECO}{_SP}*)*{_Q_OPEN}?'
    rf'(?:{_SPECIAL_NAMES})(?:{_SP}*{_NUMSEQ})?{_Q_CLOSE}?'
    # 收尾：跟分隔符/装饰线，或后面不再是汉字/数字（避免把「序幕布」这类词当标题）。
    # 中间的可选序号让「番外一」「外传 2」「间章三」也能命中。
    rf'(?:[{re.escape(_SEP_CH)}]+|(?![{_NUMCHR}\u4e00-\u9fff]))',
    re.MULTILINE,
)

# 拆章的两条结构判据：开跑前预估（core/estimate）要按同一口径数章，所以写在这里，
# 预估侧 import 而不是另抄一份。
FALLBACK_SEGMENT_CHARS = 3000   # 识别不到标题时的兜底段长
EMPTY_CHAPTER_MIN_CHARS = 50    # 少于这么多字算空章，不入批

def _read_txt_auto_encoding(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ('utf-8-sig', 'utf-8', 'gbk'):
        try: return raw.decode(enc)
        except UnicodeDecodeError: continue
    detected = chardet.detect(raw[:102400])
    return raw.decode(detected.get('encoding') or 'utf-8', errors='replace')

def _chapter_number(m) -> int | None:
    """把命中文本里的章节序号解析成整数（"第一百零一章"/"第101章"/"第一章" → 101/101/1）。

    仅用于日志/诊断展示；**不**用于去重（见 _dedupe_by_text 的说明）。
    """
    raw = m.group(0)
    # 去掉数字与单位之间的水平空格后再解析（"第 三 章"）
    compact = re.sub(rf'{_SP}+', '', raw)
    mm = re.search(rf'第({_NUM}+)[{_UNITS}]', compact)
    if not mm:
        mm = re.search(r'[0-9]+', raw)     # 英文模式 Chapter 12
        return int(mm.group(0)) if mm else None
    num = mm.group(1).translate(str.maketrans('０１２３４５６７８９', '0123456789'))
    if num.isdigit():
        return int(num)
    try:
        from cn2an import cn2an
        return int(cn2an(num, "smart"))
    except Exception:
        return None

def _at_line_start(text: str, m) -> bool:
    """命中是否位于行首（前面只有缩进）。正文里提及「第X章」通常夹在一句话中间，
    真标题则独占行首 —— 这是区分两者最稳的信号。"""
    j = m.start()
    while j > 0 and text[j - 1] in ' \t\u3000\u00a0\ufeff\u200b':
        j -= 1
    return j == 0 or text[j - 1] == '\n'

def _heading_key(m) -> str:
    """命中的规范化文本，用作去重键。

    **按文本去重而不是按章号**：分卷小说里「第1卷 第1章」与「第2卷 第1章」章号
    都是 1，按号去重会把它们折叠成同一章（4 章变 2 章）。按规范化文本去重则只在
    完全相同的标题重复出现时才折叠 —— 那才是真正的重复（或行首的正文提及）。
    """
    return re.sub(rf'{_SP}+', '', m.group(0)).strip()

def _dedupe_by_text(matches) -> list:
    out, seen = [], set()
    for m in matches:
        k = _heading_key(m)
        if k not in seen:
            seen.add(k)
            out.append(m)
    return out

def _clean_heading(raw: str) -> str:
    """把命中的标题文本清理成可读文件名：去缩进/引号/装饰线/控制字符，限长防超长路径。"""
    s = raw.strip(_SP_CH + '\f\v\ufeff\u200b\u200c\u200d')
    s = s.strip(_Q_OPEN_STR + _Q_CLOSE_STR)
    s = s.strip(_DECO_CH + _SP_CH)
    s = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|]', '', s).strip(_SP_CH)
    return s[:60]


def _dedupe_overlapping(matches) -> list:
    """按位置排序并丢掉与已接受项重叠的匹配。「第1卷 第1章」这类会让不同模式
    在同一位置附近都命中，重叠保留先出现的（按 start 排序 + 长优先）。"""
    ordered = sorted(matches, key=lambda m: (m.start(), -(m.end() - m.start())))
    out, last_end = [], -1
    for m in ordered:
        if m.start() < last_end:
            continue
        out.append(m)
        last_end = m.end()
    return out

def _pick_pattern_set(text, patterns, tags):
    """对一组模式跑一条龙判定：合并 → 去重叠 → 行首优先 → 宽松去重。"""
    allms = []
    for pat in patterns:
        allms.extend(pat.finditer(text))
    if not allms:
        return [], "none"
    merged = _dedupe_overlapping(allms)
    line = [m for m in merged if _at_line_start(text, m)]
    if len(line) >= 2:
        # 行首模式**不去重**：行首本身已排除「他翻开第一章，读到…」这类行中提及；
        # 再去重会把合法的重号标题折叠掉 —— 分卷小说里「第一卷」「第二卷」下
        # 各有一个「第一章」，按文本去重会让 6 章只剩 4 章。
        return line, f"{tags}-line"
    if len(merged) >= 2:
        # 宽松模式（整本无换行 / 标题与正文同段）：没有行首信号，只能靠去重
        # 挡掉同一行里重复出现的「第X章」提及。
        return _dedupe_by_text(merged), f"{tags}-loose"
    # 只有一个候选：短篇/单章也应被识别，返回它并标 single。
    return merged, f"{tags}-single"

def detect_chapters(text: str) -> tuple[list, str]:
    """识别章节标题。返回 (matches, mode)。

    mode: <tag>-line / <tag>-loose / <tag>-single / none
      tag = cjk（第X章 等编号标题）/ special（楔子、番外等无编号标题）/ en（Chapter N）

    「编号标题」和「特殊章节名」**合并**后再判定，而不是二选一：一本正常的书
    可能既有「第一章…第五十章」，末尾又有「尾声」「番外」。分开判定会丢掉其中一类。
    英文模式只在中文两类都识别不出时才启用，避免中文书正文里的 "Chapter" 误命中。
    """
    best, best_mode = _pick_pattern_set(text, (CHAPTER_PATTERN, _SPECIAL_PATTERN), "cjk")
    if len(best) >= 2:
        return best, best_mode
    en, en_mode = _pick_pattern_set(text, (_EN_PATTERN,), "en")
    return (en, en_mode) if len(en) > len(best) else (best, best_mode)

def _read_docx(path: Path) -> str:
    doc = Document(str(path))
    return "\n\n".join(p.text if p.text.strip() else "" for p in doc.paragraphs)

def _normalize_newlines(text: str) -> str:
    """统一换行为 LF：Windows 源文件常为 CRLF，若保留 \r 会导致后续
    split('\n\n') 段落切分失效（尤其是超长章的虚拟化分段）。"""
    return text.replace('\r\n', '\n').replace('\r', '\n')

def _read_file(path: Path, fmt: str) -> str:
    return _normalize_newlines(_read_docx(path) if fmt == "docx" else _read_txt_auto_encoding(path))

def _sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode('utf-8')).hexdigest()

def _split_by_paragraph(text: str, max_chars: int) -> list[str]:
    paras = text.split("\n\n")
    result = []
    current = ""
    for p in paras:
        if len(current) + len(p) + 2 > max_chars and current:
            result.append(current.strip())
            current = p
        else:
            current = (current + "\n\n" + p).strip() if current else p
    if current:
        result.append(current.strip())
    return result

def _split_virtual_chapters(chapters, split_dir: Path, chars: float) -> tuple[list, list]:
    """被虚拟化的原始章保留在 new_chapters，标记 is_virtual_parent=True。"""
    trigger = chars * 0.40 * 0.9
    max_virtual = chars * 0.20
    new_chapters = []
    virtual_map = []
    for ch in chapters:
        text = (split_dir / f"{ch['id']}.txt").read_text(encoding='utf-8')
        if len(text) >= trigger:
            segments = _split_by_paragraph(text, int(max_virtual))
            for i, seg in enumerate(segments):
                # 数字后缀而非字母：同一章切出>26段时 chr(ord('a')+i) 会生成非法 id
                vid = f"{ch['id']}_{i}"
                atomic_write_text(split_dir / f"{vid}.txt", seg)
                new_chapters.append({
                    "id": vid, "filename": f"{ch['filename']}#{i+1}",
                    "is_empty": False, "hash": _sha256(seg),
                    "split_file": f"02_workspace/split/{vid}.txt",
                    "is_virtual": True, "original_id": ch['id'], "order": i,
                })
                virtual_map.append({
                    "virtual_id": vid, "original_id": ch['id'],
                    "original_filename": ch['filename'], "order": i,
                    "batch_id": None,
                })
            ch['is_virtual_parent'] = True
            new_chapters.append(ch)
        else:
            new_chapters.append(ch)
    return new_chapters, virtual_map

def _segments_from_rule_matches(text: str, matches) -> list:
    """把规则命中的标题位置切成 [(标题, 正文)]。首标题之前的正文归入「序章」。"""
    segs: list[tuple[str, str]] = []
    if not matches:
        return segs
    starts = [m.start() for m in matches]
    if starts[0] > 0 and text[:starts[0]].strip():
        segs.append(("序章", text[:starts[0]]))
    for i, m in enumerate(matches):
        end = starts[i + 1] if i + 1 < len(matches) else len(text)
        _nl = text.find("\n", m.start())
        title = text[m.start(): _nl if _nl != -1 else len(text)].strip()
        segs.append((title, text[starts[i]:end]))
    return segs


def _chapters_from_segments(segs) -> list:
    """[(标题, 正文)] → chapters。filename 只用清理后的标题（短、可排序）。"""
    chapters: list[dict] = []
    for title, body in segs:
        cleaned = _clean_heading(title)
        chapters.append({"id": str(len(chapters) + 1),
                         "filename": f"{cleaned or f'第{len(chapters)+1:03d}章'}.txt",
                         "title": (title or cleaned).strip(),
                         "text": body})
    return chapters


async def _split_single_file(req, full_text: str, run_id) -> tuple[list, dict]:
    """单文件拆章：规则打底 → 低置信时 AI 兜底 → 人工标记优先 → 最后才兜底切段。

    返回 (chapters, detection)。detection 记录识别方式/置信度，供界面提示与恢复查询。
    """
    from core.chapter_ai import (assess_confidence, extract_candidate_lines,
                                 llm_detect_starts, split_by_line_anchors, split_by_markers)

    markers = [m for m in (getattr(req, "chapter_markers", None) or []) if m and m.strip()]
    matches, mode = detect_chapters(full_text)
    conf = assess_confidence(full_text, matches, mode)
    segs, method = None, "rules"

    if markers:
        # 人工标记优先级最高：用户已经明确告诉我们要怎么切
        segs = split_by_markers(full_text, markers)
        method = "markers"
        conf = {"level": "high", "reason": f"按自定义章节标记切分出 {len(segs)} 段",
                "count": len(segs), "stats": {}}
        if len(segs) < 2:
            raise RuntimeError(
                f"按自定义章节标记只切出 {len(segs)} 段。标记必须与原文的标题行**完全一致**"
                f"（含标点与空格），请检查后重试。")
    elif conf["level"] == "low" and getattr(req, "ai_split", True):
        cands = extract_candidate_lines(full_text)
        logger.info(f"[phase0] 规则置信度低（{conf['reason']}），候选行 {len(cands)} 条，尝试 AI 拆章")
        sse_emit("split_ai_start", {"reason": conf["reason"], "candidates": len(cands)}, run_id)
        starts = await llm_detect_starts(req, cands, run_id=run_id, emit=sse_emit)
        if starts and len(starts) >= 2:
            candidate = split_by_line_anchors(full_text, starts)
            if len(candidate) >= 2:
                segs, method = candidate, "ai"
                conf = {"level": "medium",
                        "reason": f"AI 依据候选行切分出 {len(segs)} 章", "count": len(segs), "stats": {}}
                logger.info(f"[phase0] AI 拆章成功，切出 {len(segs)} 章")
        if segs is None:
            logger.warning("[phase0] AI 拆章未产出可用结果，退回规则识别")

    if segs is None:
        segs = _segments_from_rule_matches(full_text, matches)

    if segs:
        chapters = _chapters_from_segments(segs)
        detection = {"mode": method, "detected": len(chapters),
                     "confidence": conf.get("level"), "reason": conf.get("reason"),
                     # 非高置信一律建议人工确认（配合 review_split / 前端提示）
                     "needs_review": conf.get("level") != "high"}
        logger.info(f"[phase0] 识别到 {len(chapters)} 章（方式={method}，置信={conf.get('level')}）")
        return chapters, detection

    # 全都失败：明确上报后按兜底段长切分（绝不静默丢结构）
    parts = max(1, (len(full_text) + FALLBACK_SEGMENT_CHARS - 1) // FALLBACK_SEGMENT_CHARS)
    msg = (f"未能识别到章节标题（已尝试编号/特殊章节名/缩进/引号/全角数字，"
           f"AI 拆章也未成功），将退回按每 {FALLBACK_SEGMENT_CHARS} 字切分成 {parts} 段。"
           f"若这不是你期望的结构，可在拆书预览里填写「自定义章节标记」重新拆分。")
    logger.warning(f"[phase0] {msg}")
    sse_emit("error", {"kind": "warning", "code": "chapter_detect_fallback",
                       "message": msg, "detected": 0, "fallback_parts": parts}, run_id)
    chapters = [{"id": str(i + 1), "filename": f"段{i+1:03d}.txt",
                 "text": full_text[i:i + FALLBACK_SEGMENT_CHARS]}
                for i in range(0, len(full_text), FALLBACK_SEGMENT_CHARS)]
    return chapters, {"mode": "fallback", "detected": 0, "confidence": "low",
                      "reason": "未识别到章节标题", "needs_review": True, "fallback_parts": parts}


async def phase0_split(req, progress, progress_path, run_id):
    output_path = Path(req.output_path)
    split_dir = output_path / "02_workspace/split"
    split_dir.mkdir(parents=True, exist_ok=True)

    # v8.8 修正：恢复时若 phase0 已完成，跳过重拆
    if progress.get('current_phase') != 'phase0' and progress.get('chapters'):
        sse_emit("phase_done", {"phase": "phase0", "total_chapters": len(progress['chapters'])}, run_id)
        return

    input_path = Path(req.input_path)
    chapters = []
    detection = {"mode": "multi_file", "detected": 0}
    if req.input_mode == "single_file":
        full_text = unicodedata.normalize('NFC', _read_file(input_path, req.input_format))
        chapters, detection = await _split_single_file(req, full_text, run_id)
    else:
        files = sorted(
            [f for f in input_path.iterdir() if f.is_file() and f.suffix[1:].lower() in ("txt", "md", "docx")],
            key=lambda f: natural_sort_key(f.name),
        )
        detection = {"mode": "multi_file", "detected": len(files)}
        for i, f in enumerate(files):
            text = _read_file(f, f.suffix[1:].lower())
            chapters.append({"id": str(i + 1), "filename": f.name, "text": unicodedata.normalize('NFC', text)})
    # 记录识别方式与章数：恢复查询/界面据此提示「是否按预期拆分」
    progress['chapter_detection'] = detection
    progress['total_chapters'] = len(chapters)

    chapters, virtual_map = _materialize_chapters(chapters, split_dir, req)
    if not chapters:
        raise RuntimeError("所有章节均为空章，请检查原始文档编码或内容")

    progress['chapters'] = chapters
    progress['virtual_chapter_map'] = virtual_map
    progress['current_phase'] = 'phase1'

    async with state.progress_lock:
        atomic_write_json(progress_path, progress)
    sse_emit("phase_done", {"phase": "phase0", "total_chapters": len(chapters)}, run_id)


def _materialize_chapters(chapters, split_dir: Path, req):
    """把内存里的 [(chapters)] 落到 split/*.txt、算 hash、虚拟化超长章。

    抽成公共函数是为了让 phase0 与「带自定义标记重新拆分」共用同一套落盘逻辑，
    避免两处实现漂移（例如漏算 hash 或忘记空章检查）。
    空章检查放在调用方（推进 current_phase 之前），失败时不留 phase 标记。
    """
    if split_dir.exists():
        for old in split_dir.glob("*.txt"):
            old.unlink()
    for ch in chapters:
        atomic_write_text(split_dir / f"{ch['id']}.txt", ch['text'])
        ch['is_empty'] = len(ch['text'].strip()) < EMPTY_CHAPTER_MIN_CHARS
        ch['hash'] = _sha256(ch['text'])
        ch['split_file'] = f"02_workspace/split/{ch['id']}.txt"
        ch.pop('text', None)
    chars = window_char_capacity(req.context_window)
    chapters, virtual_map = _split_virtual_chapters(chapters, split_dir, chars)
    if not [c for c in chapters if not c['is_empty']]:
        return [], virtual_map
    return chapters, virtual_map


async def resplit_single_file(req, progress, progress_path, run_id):
    """按（新的）req.chapter_markers 重新拆分单文件，原地更新 progress。

    由拆书预览闸门在用户点「重新拆分」时调用；只做 phase0 的工作，不推进阶段。

    **必须同时清掉下游产物**：拆书结果一变，之前按旧章节划分算出来的批次、
    重构稿、缝合锚点全部与新章节对不上号。留着会导致「按新章节跑、却去改
    旧章节文件」甚至缝合错位。这里清的是可再生的中间产物，**不动 03_final**
    （用户可能只想重跑一次而保留上一版成品；finalize 覆写时会自动备份）。
    """
    output_path = Path(req.output_path)
    split_dir = output_path / "02_workspace" / "split"
    split_dir.mkdir(parents=True, exist_ok=True)
    full_text = unicodedata.normalize('NFC', _read_file(Path(req.input_path), req.input_format))
    chapters, detection = await _split_single_file(req, full_text, run_id)
    chapters, virtual_map = _materialize_chapters(chapters, split_dir, req)
    if not chapters:
        raise RuntimeError("重新拆分后所有章节均为空章，请检查自定义标记是否正确")
    # 清理与旧章节划分绑定的中间产物（03_final / _diagnostics 保留）
    for _d in ("02_workspace/reconstructed", "02_workspace/snapshots", "01_summaries"):
        _t = output_path / _d
        if _t.is_dir():
            shutil.rmtree(_t, ignore_errors=True)
    # 批次/锚点按旧章节 id 建立，全部作废，交给 phase1 重建
    progress['batches'] = []
    progress['completed_files'] = []
    progress['processed_batches'] = []
    progress['cross_batch_virtual'] = []
    progress['stitch_anchors'] = []
    progress['stitch_virtual_done'] = []
    progress['stitch_batch_done'] = []
    progress['chapters'] = chapters
    progress['virtual_chapter_map'] = virtual_map
    progress['chapter_detection'] = detection
    progress['total_chapters'] = len(chapters)
    async with state.progress_lock:
        atomic_write_json(progress_path, progress)
    logger.info(f"[phase0] 按自定义标记重新拆分：{len(chapters)} 章")

async def phase1_diagnose(req, progress, progress_path, run_id):
    # v8.8 修正：恢复时若阶段已越过 phase1，直接跳过（避免重跑并把阶段标记打回 phase2）
    if state.phase_passed(progress, 'phase1'):
        sse_emit("phase_done", {"phase": "phase1", "resumed_skip": True}, run_id)
        logger.info(f"[phase1] 阶段已完成（current={progress.get('current_phase')}），恢复跳过")
        return

    output_path = Path(req.output_path)
    summaries_dir = output_path / "01_summaries"
    summaries_dir.mkdir(parents=True, exist_ok=True)
    
    # 单批正文上限按 token 算，并先扣掉蓝图/风格/规则等随行脚手架（见 core.budget）。
    # 旧实现 batch_limit = chars*0.40 只给正文定额，漏算脚手架，实测会发出
    # 远超模型窗口的请求（16038 token vs 本地 8448）→ 直接 400 整批报废。
    from core.budget import (
        body_token_budget,
        continuity_token_allowance,
        diagnose_max_tokens,
        story_memo_token_allowance,
    )
    body_limit = body_token_budget(
        req.context_window, author_style=req.author_style,
        reserve_tokens=(
            continuity_token_allowance(req.context_window)
            + story_memo_token_allowance(req.context_window)
        ))
    # 诊断阶段的「上一批尾巴」重叠上下文，按正文预算的比例取（比例与预估同源）
    overlap_limit = int(body_limit * OVERLAP_RATIO)

    batches = _load_batches(progress['chapters'], output_path, body_limit)
    # v8.8：恢复时若 batches 已存在，保留已有 output_files/status，不重建
    if not progress['batches']:
        progress['batches'] = [{"batch_id": i + 1, "chapter_ids": b, "status": "pending",
                                 "output_files": []} for i, b in enumerate(batches)]
    
    # 重建 cross_batch_virtual（幂等）
    progress['cross_batch_virtual'] = _build_cross_batch_virtual(
        progress['chapters'], progress['batches'], progress['virtual_chapter_map'])
    # 批次构建结果立即持久化：首批失败/崩溃后 status 查询与恢复都能看到批次划分
    async with state.progress_lock:
        atomic_write_json(progress_path, progress)
    
    from core.api_client import call_with_retry, make_httpx_client, pick_endpoint, ContextOverflowError
    from core.batching import split_batch_in_progress
    ep = pick_endpoint(req, "diagnose")
    async with make_httpx_client(req, api_url=ep["api_url"], api_key=ep["api_key"]) as client:
        prev_context = ""
        # 用 deque 而非 enumerate：诊断阶段同样可能撞上「上下文超限」（用户把
        # context_window 填大于模型真实窗口时，第一批就会 400）。拆批后要把新批次
        # 重新入队，否则 phase1 直接终止、根本走不到有保护的重构阶段。
        queue = deque(b for b in progress['batches'])
        while queue:
            batch = queue.popleft()
            # v8.8 修正：恢复时跳过已 phase1_done 的批次
            if batch['status'] != 'pending':
                prev_context = _build_overlap(progress['chapters'], batch['chapter_ids'], output_path, overlap_limit)
                continue
            chapter_ids = batch['chapter_ids']
            if state.stop_requested: raise asyncio.CancelledError()
            await _wait_pause()

            batch_content = "\n\n".join(
                (output_path / progress['chapters'][_find_idx(progress['chapters'], cid)]['split_file']).read_text(encoding='utf-8')
                for cid in chapter_ids
            )
            # P1.4：仅当设置了 diagnose_json 路径时才追加结构化 JSON 契约（缺省关）
            with_json = bool(getattr(req, "diagnose_json", None))
            prompt = _build_diagnose_prompt(prev_context, batch_content, with_json=with_json)
            payload = {"model": ep["model"], "messages": [{"role": "user", "content": prompt}],
                "temperature": req.temperatures.diagnose,
                "max_tokens": diagnose_max_tokens(len(chapter_ids), with_json),
                "stream": True}
            logger.info(f"[phase1] batch {batch['batch_id']} 开始 章节={chapter_ids}")
            sse_emit("batch_start", {"batch_id": batch['batch_id'], "chapter_ids": chapter_ids}, run_id)
            try:
                result, usage, _ = await call_with_retry(client, ep["api_url"], payload, sse_emit, run_id)
            except ContextOverflowError as e:
                parts = split_batch_in_progress(
                    progress, batch, reason="overflow", status="pending",
                    detail=(f"模型上限 {e.available} token" if e.available else "超出模型上下文窗口"),
                    run_id=run_id)
                async with state.progress_lock:
                    atomic_write_json(progress_path, progress)
                if not parts:
                    raise RuntimeError(
                        f"单章仍超出模型上下文：批次 {batch['batch_id']} 章节={batch['chapter_ids']}，"
                        + (f"模型上限约 {e.available} token。" if e.available else "模型上下文不足。")
                        + "请调大模型上下文长度，或在导入时把该章拆成更小的文件。") from e
                queue.extendleft(reversed(parts))
                # 新批次要重新计算 cross_batch_virtual（跨批次虚拟章分布变了）
                progress['cross_batch_virtual'] = _build_cross_batch_virtual(
                    progress['chapters'], progress['batches'], progress['virtual_chapter_map'])
                prev_context = ""
                continue
            summary_file = f"01_summaries/batch_{batch['batch_id']:02d}.txt"
            atomic_write_text(summaries_dir / f"batch_{batch['batch_id']:02d}.txt", result)
            # P1.4：可选结构化诊断落盘（与 batch_XX.txt 并存），解析失败不阻断流水线
            if with_json:
                diag = _parse_diag_json(result, batch['batch_id'])
                if diag is not None:
                    diag_dir = Path(req.diagnose_json)
                    diag_dir.mkdir(parents=True, exist_ok=True)
                    atomic_write_json(diag_dir / f"diagnose_{batch['batch_id']:02d}.json", diag)
                    logger.info(f"[phase1] batch {batch['batch_id']} 诊断JSON -> {diag_dir / f'diagnose_{batch['batch_id']:02d}.json'}")
                else:
                    _record_diag_json_miss(progress, batch['batch_id'], run_id)
            
            prev_context = _build_overlap(progress['chapters'], chapter_ids, output_path, overlap_limit)
            
            # v8.8 修正：累计 usage
            _accum_usage(progress, usage)
            batch['status'] = 'phase1_done'
            if batch['batch_id'] not in progress.setdefault('processed_batches', []):
                progress['processed_batches'].append(batch['batch_id'])
            async with state.progress_lock:
                atomic_write_json(progress_path, progress)
            logger.info(f"[phase1] batch {batch['batch_id']} 完成 -> {summary_file}")
            sse_emit("batch_done", {"batch_id": batch['batch_id'], "output_file": summary_file}, run_id)
            # 铁律8：批次间保持串行间隔，避免连续请求触发云端限流（间隔可配置，缺省 2s）
            if queue:
                await asyncio.sleep(getattr(req, "batch_interval_sec", 2.0))
    
    progress['current_phase'] = 'phase2'
    async with state.progress_lock:
        atomic_write_json(progress_path, progress)

def _record_diag_json_miss(progress, batch_id, run_id=None):
    """结构化诊断解析不出来：记进进度并推给用户，而不是只留一行日志。

    这条路径原本是静默的 —— 用户勾了「修断层」却拿到空清单，会以为工具在偷懒，
    实际是模型没按契约输出（或被输出长度截掉）。仍然**不阻断**：正文与摘要都已落盘，
    要不要继续由用户判断。
    """
    missed = progress.setdefault('diag_json_missing', [])
    if batch_id not in missed:
        missed.append(batch_id)
    logger.warning(f"[phase1] batch {batch_id} 未解析出诊断 JSON，跳过落盘")
    sse_emit("error", {
        "kind": "warning",
        "code": "diag_json_missing",
        "message": (f"批次 {batch_id} 的诊断输出里没有可解析的结构化 JSON（可能被输出长度截掉，"
                    f"或模型没按契约输出）。该批断层不会进入勾选清单；"
                    f"若要用「修断层」，建议重跑该批或减小批次规模。"),
    }, run_id)


def _accum_usage(progress, usage):
    """v8.8 新增：累计 token 用量。

    用 setdefault 而非直接下标：init_progress 明确承诺兼容旧 run
    （"旧 run 无字段即按此恢复"），但这里的 progress['usage'] 对 8.8 之前的
    progress.json 会直接 KeyError，把整轮已完成的诊断结果打回。
    """
    if not usage: return
    acc = progress.setdefault('usage', {"prompt_tokens": 0, "completion_tokens": 0})
    acc.setdefault('prompt_tokens', 0)
    acc.setdefault('completion_tokens', 0)
    acc['prompt_tokens'] += usage.get('prompt_tokens', 0) or 0
    acc['completion_tokens'] += usage.get('completion_tokens', 0) or 0

def _find_idx(chapters, cid):
    for i, c in enumerate(chapters):
        if c['id'] == cid: return i
    raise KeyError(cid)

def _load_batches(chapters, output_path, body_token_limit,
                  max_chapters=MAX_CHAPTERS_PER_BATCH):
    """按 original_id 分组；跳过 is_virtual_parent 父章；同一组不拆散。

    body_token_limit 是**单批正文的 token 上限**（不是字数）—— 由 core.budget
    按上下文窗口先扣掉蓝图/风格/规则等脚手架后算得。用 token 而非字数的原因见
    core/budget.py 的模块文档。
    """
    groups = []  # list of (group_key, [chapter_dict])
    for ch in chapters:
        if ch.get('is_empty'):
            continue
        if ch.get('is_virtual_parent'):
            continue  # 父章不进批次
        if ch.get('is_virtual'):
            orig = ch.get('original_id')
            placed = False
            for gk, glist in groups:
                if gk == ('virtual', orig):
                    glist.append(ch)
                    placed = True
                    break
            if not placed:
                groups.append((('virtual', orig), [ch]))
        else:
            groups.append((('normal', ch['id']), [ch]))

    return pack_groups(
        [
            (
                gk,
                [ch['id'] for ch in glist],
                sum(estimate_tokens((output_path / ch['split_file'])
                                    .read_text(encoding='utf-8')) for ch in glist),
            )
            for gk, glist in groups
        ],
        body_token_limit,
        max_chapters,
    )

def _build_cross_batch_virtual(chapters, batches, virtual_map):
    """扫描虚拟章跨批次分布。返回 [{"original_id": "7", "batch_pairs": [[2,3]]}, ...]"""
    if not virtual_map:
        return []
    vid_to_batch = {}
    for b in batches:
        for cid in b['chapter_ids']:
            vid_to_batch[cid] = b['batch_id']
    orig_to_batches = {}
    for v in virtual_map:
        orig = v['original_id']
        bid = vid_to_batch.get(v['virtual_id'])
        if bid is not None:
            orig_to_batches.setdefault(orig, set()).add(bid)
    result = []
    for orig, bset in orig_to_batches.items():
        if len(bset) > 1:
            sorted_batches = sorted(bset)
            pairs = [[sorted_batches[i], sorted_batches[i+1]] for i in range(len(sorted_batches) - 1)]
            result.append({"original_id": orig, "batch_pairs": pairs})
    return result

def _build_overlap(chapters, prev_chapter_ids, output_path, overlap_limit):
    """回溯时按 original_id 合并虚拟章。overlap_limit 单位为 token（与单批预算一致）。"""
    logical = {}  # original_id -> list[(order, text)]
    for cid in prev_chapter_ids:
        ch = chapters[_find_idx(chapters, cid)]
        if ch.get('is_empty'):
            continue
        text = (output_path / ch['split_file']).read_text(encoding='utf-8')
        if ch.get('is_virtual'):
            orig = ch.get('original_id')
            order = ch.get('order', 0)
            logical.setdefault(orig, []).append((order, text))
        else:
            logical.setdefault(cid, []).append((0, text))
    
    merged = []
    for key, parts in logical.items():
        parts.sort(key=lambda x: x[0])
        merged.append((key, "".join(t for _, t in parts)))
    
    result_parts, total = [], 0
    for _key, text in reversed(merged):
        if total + estimate_tokens(text) > overlap_limit and result_parts:
            break
        result_parts.insert(0, text)
        total += estimate_tokens(text)
        if len(result_parts) >= 3:
            break
    return "\n\n".join(result_parts)

def _build_diagnose_prompt(prev_context, batch_content, with_json=False):
    text = f"""你是一位拥有二十年经验的资深小说主编。你的任务是对输入的章节进行精准的"局部诊断"。
请阅读用户提供的【当前章节正文】，结合【前情提要】，输出以下三个维度的结构化诊断报告。
1. 章节摘要（逐章列出，每章150字以内）
2. 人物状态（主角及关键配角在本批次中的状态变化）
3. 断层诊断（指出这批章节之间，以及开头与上一段前情之间，是否存在情节跳跃、逻辑不连贯或动机突兀的问题。如果有，给出一句话的修改建议。如果没问题，直接写"逻辑连贯"。）

【前情提要】
{prev_context}

【当前章节正文】
{batch_content}
"""
    if not with_json:
        return text
    # P1.4：追加结构化 JSON 契约，供 fix_gaps 消费；流式返回纯文本，故要求独立 ```json 代码块
    text += """

【结构化诊断（本批次必须输出）】
请在报告末尾另起一段，输出一个可直接解析的 JSON 代码块（用 ```json 包裹，只保留一个），字段契约：
{
  "summary": "本批次章节整体摘要（150字内）",
  "gaps": [
    {
      "type": "plot_jump 或 logic_inconsistency 或 motivation_abrupt",
      "severity": "high 或 medium 或 low",
      "location": "断层所在位置（第几章 / 开头 / 结尾）",
      "description": "一句话描述断层",
      "suggestion": "一句话修改建议"
    }
  ]
}
若本批次无断层，gaps 为空数组 []。
"""
    return text


def _parse_diag_json(result: str, batch_id: int) -> dict | None:
    """P1.4：从流式纯文本中抽取 ```json 代码块并解析；失败回退查找顶层 {...}。
    返回结构化诊断对象；解析失败返回 None（可选能力，不阻断流水线）。
    同时为每个断层补稳定的 id（B{batch_id}G{i}），供 fix_gaps 的 fix_list 引用。"""
    candidate = result
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", result, re.IGNORECASE)
    if m:
        candidate = m.group(1).strip()
    try:
        start = candidate.index("{")
        end = candidate.rindex("}")
        obj = json.loads(candidate[start:end + 1])
    except (ValueError, json.JSONDecodeError):
        logger.warning(f"[phase1] batch {batch_id} 诊断 JSON 解析失败")
        return None
    if not isinstance(obj, dict):
        return None
    gaps = obj.get("gaps")
    if not isinstance(gaps, list):
        gaps = []
    normalized = []
    for i, g in enumerate(gaps):
        if not isinstance(g, dict):
            continue
        g["id"] = f"B{batch_id}G{i + 1}"
        normalized.append(g)
    obj["batch_id"] = batch_id
    obj["gaps"] = normalized
    return obj

async def _wait_pause():
    """等待用户恢复。心跳由 pipeline_runner._heartbeat_loop 统一负责。"""
    while not state.pause_event.is_set():
        if state.stop_requested: raise asyncio.CancelledError()
        await asyncio.sleep(1.0)
