"""开跑前预估：章数 / 批数 / 调用数 / token 量级，**不接模型、零网络调用**。

为什么要它：批大小由 `context_window` 推导，而这个值一改变，批数、边界数、
脚手架重复次数全都跟着变。用户界面里只填了一个数字，代价却藏在后面三个阶段里
（实测口径见 core/budget.py 模块文档）。跑一次几十万字的书可能是几小时，
所以「开跑前先把代价说出来」比「跑完再告诉你用了多少 token」有用得多。

怎么保证不漂移：
  - 单批正文上限用 `budget.body_token_budget`（与 phase1 同一个函数）；
  - 装批用 `batching.pack_groups`（与 phase1 同一个算法）；
  - 诊断输出上限用 `budget.diagnose_max_tokens`（与 phase1 同一个函数）；
  - 窗口派生的一切系数（章数上限、重叠比例、窗口换算 1.5、缝合窗口及其封顶、
    蓝图总预算、蓝图预留、缝合蓝图切片）一律 import `core.budget` 的常量与函数，
    不复制数字；
  - 分卷压缩**直接调用真跑的分块函数** `blueprint.chunk_by_budget`，块预算 /
    输出目标 / 轮数上限按属性读 `core.blueprint` 的 `MAP_*` 常数（按属性读而不是
    import 值 —— 真跑那边改了常数，预估立刻跟着变）。
  防再抄的用例是 `tests/test_budget_source.py`；数值对照见 `tests/test_estimate.py`。

只剩提示词骨架的粗估 token（`DIAG/BLUEPRINT/STITCH_FIXED_TOKENS`、
`MAP_PROMPT_SKELETON_TOKENS`）是本模块自定的：真跑不会给自己的提示词算 token，
只有预估需要，因此不存在第二份可漂的拷贝。它们也没有不变量可断言 —— 少算一项
只会让预估更保守（仍是下界），不会让真跑出错，故刻意不给它们写门禁用例。

明确**不计入**的部分（都写进 notes，界面上照原样显示）：自动重写、拆批重跑、
上下文超限重试、逐章验收里的单章重生成。因此这是下界，不是上界。
"""

from __future__ import annotations

import copy
import hashlib
import json
import unicodedata
from collections import OrderedDict
from pathlib import Path

from core import blueprint
from core.batching import pack_groups
from core.budget import (
    CONTEXT_SAFETY,
    MAX_CHAPTERS_PER_BATCH,
    OUTPUT_RATIO,
    OVERLAP_RATIO,
    PROMPT_FIXED_TOKENS,
    STITCH_MAX_WINDOW_CHARS,
    STITCH_WINDOW_RATIO,
    blueprint_context_chars,
    blueprint_for_stitch_chars,
    blueprint_token_allowance,
    body_token_budget,
    continuity_token_allowance,
    diagnose_max_tokens,
    stitch_window_chars,
    story_memo_token_allowance,
    window_char_capacity,
)
from utils.sort import natural_sort_key
from utils.tokens import estimate_tokens

# 诊断提示词的固定骨架（角色设定 + 三段要求），粗估 token
DIAG_FIXED_TOKENS = 600
# 蓝图 final 调用的规则文本骨架
BLUEPRINT_FIXED_TOKENS = 300
# 缝合提示词的规则与标记说明骨架
STITCH_FIXED_TOKENS = 400
# 分卷压缩提示词（角色设定 + 三条保留要求）的骨架，粗估 token。
# 预估独有的加项：真跑不给自己的提示词算 token，只有预估需要。
MAP_PROMPT_SKELETON_TOKENS = 150

_DOCX_EXTS = ("txt", "md", "docx")


def _chars_to_tokens(n: float) -> int:
    """中文字数 → token 的粗算。

    这里直接用「1 字 ≈ 1 token」，与 utils.tokens 对中文的口径一致，
    而且本模块的输入长度都来自 estimate_tokens，不会再引入第二个比例。
    """
    return max(0, int(n))


def chapter_token_sizes(req) -> list[int]:
    """按 phase0 的规则路径估算每章 token（只读文件，不调模型、不做 AI 拆章）。

    与真跑的差别：真跑在规则置信度低时可能用 AI/自定义标记重新拆章。章的**切分位置**
    可能不同，但正文总量不变，因此批数误差在一两个批次以内（notes 里已声明）。
    """
    from core.splitter import (
        EMPTY_CHAPTER_MIN_CHARS,
        FALLBACK_SEGMENT_CHARS,
        _read_file,
        _segments_from_rule_matches,
        detect_chapters,
    )

    input_path = Path(req.input_path)
    bodies: list[str] = []
    if req.input_mode == "single_file":
        text = unicodedata.normalize("NFC", _read_file(input_path, req.input_format))
        matches, _mode = detect_chapters(text)
        segs = _segments_from_rule_matches(text, matches)
        if len(segs) >= 2:
            bodies = [body for _title, body in segs]
        else:
            # 兜底：与 phase0 同一个 FALLBACK_SEGMENT_CHARS
            bodies = [
                text[i : i + FALLBACK_SEGMENT_CHARS]
                for i in range(0, len(text), FALLBACK_SEGMENT_CHARS)
            ]
    else:
        files = sorted(
            [
                f
                for f in input_path.iterdir()
                if f.is_file() and f.suffix[1:].lower() in _DOCX_EXTS
            ],
            key=lambda f: natural_sort_key(f.name),
        )
        bodies = [_read_file(f, f.suffix[1:].lower()) for f in files]
    # 少于 EMPTY_CHAPTER_MIN_CHARS 的段在 phase0 会被标成空章并跳过，这里同判据
    return [
        estimate_tokens(b) for b in bodies if len(b.strip()) >= EMPTY_CHAPTER_MIN_CHARS
    ]


def _blueprint_calls(context_window: int, summary_sizes: list[int]) -> tuple[int, int, int]:
    """分卷压缩的 (调用数, 输入 token, 输出 token)。

    **直接复用真跑的分块函数** `blueprint.chunk_by_budget`，并照抄它的三条规则：
    单条块的批次原样保留、不调模型；块预算与输出目标由 `blueprint.MAP_*` 常数决定；
    压不完 `MAX_COMPRESS_ROUNDS` 轮就转等距抽样（抽样不花调用）。
    每条摘要按 `diagnose_max_tokens()` 的**预留长度**代入 —— 预估一贯按预留算钱。

    旧实现按「总量 ÷ 平均块长」估块数，会把真跑原样保留的单条块也算成一次调用，
    于是预估比真跑**多**算，与本模块「这是下界」的声明相反。
    """
    total_budget = blueprint_context_chars(window_char_capacity(context_window))
    chunk_budget = max(
        blueprint.MAP_CHUNK_MIN_CHARS,
        _chars_to_tokens(total_budget * blueprint.MAP_CHUNK_RATIO),
    )
    items = ["摘" * max(0, int(n)) for n in summary_sizes]
    calls = in_tokens = out_tokens = 0
    for _round in range(blueprint.MAX_COMPRESS_ROUNDS):
        if blueprint.joined_len(items) <= total_budget or len(items) <= 1:
            break
        chunks = blueprint.chunk_by_budget(items, chunk_budget)
        if len(chunks) <= 1:
            break
        new_items: list[str] = []
        for chunk in chunks:
            if len(chunk) == 1:
                new_items.append(chunk[0])
                continue
            text_len = blueprint.joined_len(chunk)
            target = max(
                blueprint.MAP_TARGET_MIN_CHARS,
                min(
                    blueprint.MAP_TARGET_MAX_CHARS,
                    int(text_len * blueprint.MAP_TARGET_RATIO),
                ),
            )
            calls += 1
            in_tokens += text_len + MAP_PROMPT_SKELETON_TOKENS
            out_tokens += target
            new_items.append("压" * target)
        if blueprint.joined_len(new_items) >= blueprint.joined_len(items):
            break
        items = new_items
    return calls, in_tokens, out_tokens


# #15：预估结果内存 LRU 缓存。同一份输入（路径 + mtime/size + 影响算式的配置）
# 重复请求不重扫原文；大对象高频状态拆分属架构级改动，本轮只落 estimate 缓存。
# 用 OrderedDict：命中 move_to_end，淘汰 popitem(last=False)，真正 LRU。
_ESTIMATE_CACHE: OrderedDict[str, dict] = OrderedDict()
_ESTIMATE_CACHE_MAX = 16


def _input_signature(req) -> tuple:
    """组成预估结果的输入指纹：原文文件指纹 + 影响算式的配置项。"""
    p = Path(req.input_path)
    sig: list = [
        str(p), req.input_mode, req.input_format, int(req.context_window),
        getattr(req, "author_style", "") or "",
        bool(getattr(req, "diagnose_json", None)),
        bool((getattr(req, "refactor_gates", {}) or {}).get("skip_stitch_if_smooth")),
    ]
    try:
        if req.input_mode == "single_file":
            st = p.stat()
            sig.extend([st.st_mtime_ns, st.st_size])
        elif p.is_dir():
            sig.append(sorted(
                (f.name, f.stat().st_mtime_ns, f.stat().st_size)
                for f in p.iterdir() if f.is_file()
            ))
        else:
            sig.append(None)
    except OSError:
        sig.append(None)
    return tuple(sig)


def estimate_run(req) -> dict:
    """带内存 LRU 缓存的规模预估入口；返回深拷贝，避免调用方改写污染缓存。"""
    key = hashlib.sha1(
        json.dumps(_input_signature(req), ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()
    cached = _ESTIMATE_CACHE.get(key)
    if cached is not None:
        _ESTIMATE_CACHE.move_to_end(key)  # 命中即 touch，真 LRU
        return copy.deepcopy(cached)
    result = _estimate_run_uncached(req)
    _ESTIMATE_CACHE[key] = result
    _ESTIMATE_CACHE.move_to_end(key)
    while len(_ESTIMATE_CACHE) > _ESTIMATE_CACHE_MAX:
        _ESTIMATE_CACHE.popitem(last=False)  # 淘汰最久未用
    return copy.deepcopy(result)


def _estimate_run_uncached(req) -> dict:
    """返回一次运行的规模与 token 预估（下界）。"""
    sizes = chapter_token_sizes(req)
    if not sizes:
        raise ValueError("没有可处理的章节（全是空章）")

    cw = int(req.context_window)
    style = getattr(req, "author_style", "") or ""
    style_tokens = estimate_tokens(style)
    # 与 splitter.phase1_diagnose 同源：只有设了结构化诊断路径才追加 JSON 契约
    with_json = bool(getattr(req, "diagnose_json", None))
    cont = continuity_token_allowance(cw)
    memo = story_memo_token_allowance(cw)
    blueprint_reserve = blueprint_token_allowance(cw)
    body_limit = body_token_budget(
        cw, author_style=style, reserve_tokens=cont + memo
    )

    batches = pack_groups(
        [(i + 1, [str(i + 1)], s) for i, s in enumerate(sizes)],
        body_limit,
        MAX_CHAPTERS_PER_BATCH,
    )
    batch_sizes = [sum(sizes[int(bid) - 1] for bid in batch) for batch in batches]
    body_total = sum(batch_sizes)

    # ---- 阶段 1 诊断（串行，每批一次）----
    diag_in = sum(
        b + int(b * OVERLAP_RATIO) + DIAG_FIXED_TOKENS + style_tokens for b in batch_sizes
    )
    diag_out_each = [
        diagnose_max_tokens(len(bat), with_json=with_json) for bat in batches
    ]
    diag_out = sum(diag_out_each)

    # ---- 阶段 2 蓝图 ----
    map_calls, map_in, map_out = _blueprint_calls(cw, diag_out_each)
    summary_ctx = min(diag_out + map_out, blueprint_context_chars(window_char_capacity(cw)))
    gap_reserve = min(blueprint.GAP_DIGEST_MAX_CHARS, summary_ctx // 3)
    bp_in = summary_ctx + gap_reserve + BLUEPRINT_FIXED_TOKENS + style_tokens
    bp_out = blueprint_reserve

    # ---- 阶段 3 重构（每批一次，脚手架每次都随请求重发）----
    scaffold = PROMPT_FIXED_TOKENS + blueprint_reserve + style_tokens + cont + memo
    ref_in = sum(b + scaffold for b in batch_sizes)
    ref_out = int(body_total * OUTPUT_RATIO)

    # ---- 阶段 3-2 缝合（批次边界 + 虚拟章边界，这里只算批次边界）----
    gates = getattr(req, "refactor_gates", {}) or {}
    boundaries = max(0, len(batches) - 1)
    if gates.get("skip_stitch_if_smooth"):
        # 硬分章断点会跳过，比例无法预知：按下限 0 次调用，并在 notes 里说明
        boundaries = 0
    # 窗口按字计入 prompt（两侧各一份），算式与 stitch._stitch_common 同一个函数：
    # 不加封顶，128k 窗口下预估会报出 1.4 万字的「交界窗口」，比一整章还长。
    window_chars = window_char_capacity(cw)
    extract = _chars_to_tokens(stitch_window_chars(window_chars))
    window_uncapped = int(window_chars * STITCH_WINDOW_RATIO)
    bp_for_stitch = _chars_to_tokens(blueprint_for_stitch_chars(window_chars))
    st_in = boundaries * (2 * extract + bp_for_stitch + STITCH_FIXED_TOKENS)
    st_out = boundaries * _chars_to_tokens(2 * extract * 1.2)

    prompt = diag_in + map_in + bp_in + ref_in + st_in
    completion = diag_out + map_out + bp_out + ref_out + st_out
    avg_body = body_total / max(1, len(batches))
    calls = {
        "diagnose": len(batches),
        "blueprint": map_calls + 1,
        "refactor": len(batches),
        "stitch": boundaries,
        "total": len(batches) + map_calls + 1 + len(batches) + boundaries,
    }
    chars_total = sum(sizes)  # token 口径下与字数同量级
    notes = [
        "预估为下界：不含自动重写、拆批重跑、上下文超限重试、单章重生成",
        "拆章按规则识别估算；真跑若触发自定义标记/AI 拆章，批数可能相差一两个批次",
        "缝合只算批次边界，虚拟章边界未计入",
    ]
    if window_uncapped > STITCH_MAX_WINDOW_CHARS:
        notes.append(
            f"缝合窗口已封顶 {STITCH_MAX_WINDOW_CHARS} 字"
            f"（按窗口本该是 {window_uncapped} 字；更大的窗口只会重写整章，不是补交界）"
        )
    return {
        "chapters": len(sizes),
        "batches": len(batches),
        "chapters_per_batch": [len(b) for b in batches],
        "body_token_limit": int(body_limit),
        "context_window": cw,
        "max_chapters_per_batch": MAX_CHAPTERS_PER_BATCH,
        "safety": CONTEXT_SAFETY,
        "calls": calls,
        "tokens": {
            "prompt": prompt,
            "completion": completion,
            "total": prompt + completion,
            "by_stage": {
                "diagnose": {"prompt": diag_in, "completion": diag_out},
                "blueprint": {"prompt": map_in + bp_in, "completion": map_out + bp_out},
                "refactor": {"prompt": ref_in, "completion": ref_out},
                "stitch": {"prompt": st_in, "completion": st_out},
            },
        },
        "scaffold_per_request": scaffold,
        "scaffold_share": round(scaffold / max(1, scaffold + avg_body), 3),
        "stitch_boundaries": boundaries,
        "stitch_window_chars": extract,
        "stitch_exposed_chars": boundaries * 2 * extract,
        "stitch_exposed_ratio": (
            round(boundaries * 2 * extract / max(1, chars_total), 3) if boundaries else 0.0
        ),
        "notes": notes,
    }
