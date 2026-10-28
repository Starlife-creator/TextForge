"""AI 辅助拆章：规则识别不可靠时，用 LLM 只在「候选行」里挑章节边界。

═══ 安全不变量（务必保持）═══
LLM **只返回行号**，真正的切割由本模块在**原文**上执行。LLM 永远不接触、
不重写正文 —— 因此它最多把边界定错位置（用户能在预览里看到并纠正），
**不可能丢字、改字或编造内容**。本地小模型尤其会改写正文，这条是底线。

═══ 为什么可行（成本有界）═══
整本《五十万字》直接喂模型不现实（本地 8448 窗口 + 1.63 t/s）。所以先用纯代码
抽出「样式上像标题」的**候选行**（短行 / 独立成段 / 命中标题正则），通常只占全文
1~2%。只把这一小撮（带行号）交给模型判定，输出也被限制成行号数组。

═══ 三层配合 ═══
① rules（splitter.detect_chapters）打底 → 高置信直接用
② 本模块：低置信时才做候选提取 + LLM 判定（结果仍要用锚点在原文里定位）
③ 人工确认：低置信 / 用过 AI / 用户开了预览 → 走拆书预览闸门
"""

import logging
import re

from core.api_client import call_with_retry, make_httpx_client, pick_endpoint

logger = logging.getLogger("textforge")

# 候选行判定参数（#19：与 quality.py 一样集中维护，取值依据写在注释里）
# 中文标题一般 ≤20 字；48 字以上更像正文段落，不再当候选标题。
CAND_MAX_LEN = 48
# 候选行总数硬上限：超长小说候选爆炸时兜底，防止把上下文撑爆；超出部分规则侧丢弃。
MAX_CANDIDATES = 3000
# 单次送模型的候选文本块预算（字符）；配合 AI_MAX_TOKENS 控制往返体量。
CAND_PAYLOAD_CHARS = 2400
# 模型只需回一串行号：3000 候选每行号约 3~4 token，700 token 留足余量。
AI_MAX_TOKENS = 700

# 候选行里「看起来像标题」的正则（与 splitter 的检测规则一致）
from core.splitter import CHAPTER_PATTERN, _EN_PATTERN, _SPECIAL_PATTERN

_END_PUNCT = "。！？；，、,.!?;:：…—）)】」』”\"'"
_HEAD_PATTERNS = (CHAPTER_PATTERN, _SPECIAL_PATTERN, _EN_PATTERN)


def _looks_like_heading(s: str) -> bool:
    """单行是否命中任一标题规则（用于给候选行排序：命中规则的优先送模型）。"""
    for pat in _HEAD_PATTERNS:
        try:
            if pat.match(s):
                return True
        except Exception:
            continue
    return False


def extract_candidate_lines(text: str, *, max_len: int = CAND_MAX_LEN,
                            max_total: int = MAX_CANDIDATES):
    """抽出「样式上像标题」的行。返回 [(行号(1-based), 文本), ...]，按行号升序。

    三类（优先级从高到低）：
      0 命中标题正则（第X章 / 楔子 / Chapter N）—— 几乎必然是标题
      1 独立成段（前后有空行）的短行
      2 紧跟在段末标点之后、且自身不以标点结尾的短行（单 \n 排版的书）
    命中数超过 max_total 时优先保留高优先级的。
    """
    lines = text.split("\n")
    scored = []
    for idx, raw in enumerate(lines):
        s = raw.strip()
        if not s or len(s) > max_len:
            continue
        head = _looks_like_heading(s)
        prev_blank = idx == 0 or lines[idx - 1].strip() == ""
        next_blank = idx == len(lines) - 1 or lines[idx + 1].strip() == ""
        prev_tail = lines[idx - 1].rstrip()[-1:] if idx > 0 else "。"
        if head:
            prio = 0
        elif prev_blank or next_blank:
            prio = 1
        elif prev_tail in "。！？”\"":
            prio = 2
        else:
            continue
        # 普通短句（以标点结尾且不像标题）直接排除，避免候选集被正文塞满
        if not head and s[-1] in _END_PUNCT:
            continue
        scored.append((prio, idx + 1, s))
    scored.sort(key=lambda x: (x[0], x[1]))
    chosen = scored[:max_total]
    chosen.sort(key=lambda x: x[1])
    return [(ln, s) for _, ln, s in chosen]


def assess_confidence(text: str, matches: list, mode: str) -> dict:
    """判断规则识别的可信度。返回 {level: high|low, reason, count, stats}。

    low 的信号（任一命中即触发 AI 兜底 / 人工确认）：
      - 一个标题都没识别出（mode=none）
      - 只识别出 1 个（single）
      - 章节长度极不均匀（最长 >> 中位）—— 典型的「有标题被漏切」
      - 编号整体非单调递增 —— 典型的误识别或漏识别
    """
    n = len(matches)
    if not n or mode == "none":
        return {"level": "low", "reason": "未识别到任何章节标题", "count": 0, "stats": {}}
    if "single" in mode:
        return {"level": "low", "reason": "只识别到 1 个章节标题", "count": n, "stats": {}}

    positions = [m.start() for m in matches] + [len(text)]
    lens = [positions[i + 1] - positions[i] for i in range(n)]
    lens_sorted = sorted(lens)
    median = lens_sorted[len(lens_sorted) // 2] or 1
    longest = max(lens)
    # 用「其余章的平均长度」而不是中位做基准：中位在章数很少时会退化成 max，
    # 反而把「一章 3 万字、另一章几十字」这种明显漏切判成正常。
    total = sum(lens)
    avg_others = (total - longest) / max(1, n - 1)
    stats = {"count": n, "median_len": median, "longest_len": longest,
             "avg_other_len": int(avg_others)}
    if longest > max(15000, 4 * avg_others):
        return {"level": "low",
                "reason": f"章节长度极不均匀（最长 {longest} 字 / 其余章均 {int(avg_others)} 字），疑似漏切",
                "count": n, "stats": stats}

    # 编号单调性：把能解析出数字的标题按序比较。
    # 只把「回退到非 1 的编号」算作异常 —— 分卷小说会在新卷处**重置回第 1 章**，
    # 那是合法结构，不能因此触发人工确认（否则每本分卷书都要白跑一次 AI）。
    from core.splitter import _chapter_number
    nums = [x for x in (_chapter_number(m) for m in matches) if x is not None]
    if len(nums) >= 3:
        odd = sum(1 for a, b in zip(nums, nums[1:], strict=False) if b < a and b != 1)
        if odd >= 1:
            return {"level": "low",
                    "reason": f"章节编号出现 {odd} 处异常回退，疑似误识别/漏识别",
                    "count": n, "stats": stats}
    return {"level": "high", "reason": f"识别到 {n} 章且长度分布正常", "count": n, "stats": stats}


_AI_PROMPT = (
    "你是拆书助手。下面是从一本小说里挑出的「样式上像小标题」的候选行，格式为 `行号|内容`。\n"
    "请判断哪些行是**新章节（或新分节）的起点**。\n"
    "判据：章节编号行、卷名、楔子/序/尾声/番外/间章等特殊章节名、以及明显独立成段的标题行，都算起点；\n"
    "被误收进来的正文短句不算。\n"
    "只输出 JSON，不要任何解释：{\"starts\":[行号,...]}\n\n候选行：\n"
)


def _parse_starts(text: str):
    """从模型输出里稳健地抠出 starts 行号数组。解析不出返回 None。"""
    if not text:
        return None
    m = re.search(r'"starts"\s*:\s*\[([^\]]*)\]', text)
    if not m:
        m = re.search(r'\[\s*([0-9][0-9,\s]*)\]', text)
    if not m:
        return None
    return [int(x) for x in re.findall(r'\d+', m.group(1))]


def _chunks(cands, payload_chars=CAND_PAYLOAD_CHARS):
    """把候选行按体积切成若干块，避免单次提示词撑爆小窗口。"""
    cur: list = []
    size = 0
    for ln, s in cands:
        line_len = len(s) + len(str(ln)) + 2
        if cur and size + line_len > payload_chars:
            yield cur
            cur, size = [], 0
        cur.append((ln, s))
        size += line_len
    if cur:
        yield cur


async def llm_detect_starts(req, candidates, *, run_id=None, emit=None) -> list | None:
    """让 LLM 从候选行里挑章节起点，返回**已在原文里校验过**的行号列表。

    失败（网络/解析不出/无候选）返回 None，由调用方退回规则或人工确认。
    """
    if not candidates:
        return None
    ep = pick_endpoint(req, "diagnose")
    found = set()
    any_ok = False
    async with make_httpx_client(req, api_url=ep["api_url"], api_key=ep["api_key"]) as client:
        for chunk in _chunks(candidates):
            valid = {ln for ln, _ in chunk}
            body = "\n".join(f"{ln}|{s}" for ln, s in chunk)
            payload = {
                "model": ep["model"],
                "messages": [{"role": "user", "content": _AI_PROMPT + body}],
                "temperature": 0.0,
                "max_tokens": AI_MAX_TOKENS,
                "stream": True,
            }
            try:
                text, _, _ = await call_with_retry(client, ep["api_url"], payload, emit, run_id)
            except Exception as e:
                logger.warning(f"[ai-split] 调用失败（忽略该块）: {str(e)[:160]}")
                continue
            starts = _parse_starts(text)
            if starts is None:
                logger.warning(f"[ai-split] 无法解析模型输出: {str(text)[:160]}")
                continue
            any_ok = True
            # 只接受确实是「候选行」的行号 —— 模型给的行号必须在候选集合里，
            # 否则丢弃（防止模型凭空编号，把边界定到正文中间）。
            found |= {n for n in starts if n in valid}
    if not any_ok:
        return None
    return sorted(found)


def split_by_line_anchors(text: str, starts):
    """按 1-based 行号锚点切分原文。返回 [(标题, 正文), ...]。

    首个锚点之前的正文作为「序章」保留（不能丢）。
    """
    lines = text.split("\n")
    starts = sorted({s for s in starts if 1 <= s <= len(lines)})
    if not starts:
        return []
    out = []
    if starts[0] > 1:
        preface = "\n".join(lines[:starts[0] - 1]).strip()
        if preface:
            out.append(("序章", preface))
    for i, ln in enumerate(starts):
        end = starts[i + 1] - 1 if i + 1 < len(starts) else len(lines)
        seg = "\n".join(lines[ln - 1:end]).strip()
        if seg:
            title = lines[ln - 1].strip()[:60]
            out.append((title, seg))
    return out


def split_by_markers(text: str, markers):
    """人工指定的章节标记：某行 strip 后**等于**标记，即为章起点。

    刻意用**精确相等**而不是 startswith：否则标记「楔子」会连「楔子正文，交代背景。」
    这一整行正文都当成章起点（实测把 4 章切成 5 段）。用户要标的是标题行本身。
    比 AI 更可控，是遇到「创新排版」时的最终兜底手段。
    """
    ms = {m.strip() for m in (markers or []) if m and m.strip()}
    if not ms:
        return []
    lines = text.split("\n")
    starts = [i + 1 for i, raw in enumerate(lines) if raw.strip() in ms]
    return split_by_line_anchors(text, starts)
