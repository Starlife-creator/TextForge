"""开跑前预估：章数 / 批数 / 调用数 / token 量级，**不接模型、零网络调用**。

为什么要它：批大小由 `context_window` 推导，而这个值一改变，批数、边界数、
脚手架重复次数全都跟着变。用户界面里只填了一个数字，代价却藏在后面三个阶段里
（实测口径见 core/budget.py 模块文档）。跑一次几十万字的书可能是几小时，
所以「开跑前先把代价说出来」比「跑完再告诉你用了多少 token」有用得多。

怎么保证不漂移：
  - 单批正文上限用 `budget.body_token_budget`（与 phase1 同一个函数）；
  - 装批用 `batching.pack_groups`（与 phase1 同一个算法）；
  - 缝合窗口/蓝图预留照抄 stitch.py、pipeline.py、refactor.py 里的那几个系数。
  这几处系数若被改动，`tests/test_estimate.py` 的对照断言会失败。

明确**不计入**的部分（都写进 notes，界面上照原样显示）：自动重写、拆批重跑、
上下文超限重试、逐章验收里的单章重生成。因此这是下界，不是上界。
"""

from __future__ import annotations

import math
import unicodedata
from pathlib import Path

from core.batching import pack_groups
from core.budget import (
    CONTEXT_SAFETY,
    OUTPUT_RATIO,
    PROMPT_FIXED_TOKENS,
    blueprint_token_allowance,
    body_token_budget,
    continuity_token_allowance,
    story_memo_token_allowance,
)
from utils.sort import natural_sort_key
from utils.tokens import estimate_tokens

# 与 splitter._load_batches 的默认值同源：单批最多 8 章
MAX_CHAPTERS_PER_BATCH = 8
# 诊断提示词的固定骨架（角色设定 + 三段要求），粗估 token
DIAG_FIXED_TOKENS = 600
# 诊断输出的经验值：每章摘要 ≤150 字，另加人物状态与断层诊断
DIAG_TOKENS_PER_CHAPTER = 160
DIAG_TOKENS_FLOOR = 400
DIAG_MAX_TOKENS = 2000
# 蓝图 final 调用的规则文本骨架
BLUEPRINT_FIXED_TOKENS = 300
# 缝合提示词的规则与标记说明骨架
STITCH_FIXED_TOKENS = 400
# 断层清单在蓝图上下文里的预留（blueprint.read_gap_digest 的默认上限）
GAP_DIGEST_MAX_CHARS = 3000
# phase0 拆不出章节标题时的兜底段长（splitter._split_single_file）
FALLBACK_SEGMENT_CHARS = 3000
# 空章判据：与 splitter._materialize_chapters 的 `is_empty` 同源（少于 50 字不入批）
MIN_CHAPTER_CHARS = 50

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
    from core.splitter import _read_file, _segments_from_rule_matches, detect_chapters

    input_path = Path(req.input_path)
    bodies: list[str] = []
    if req.input_mode == "single_file":
        text = unicodedata.normalize("NFC", _read_file(input_path, req.input_format))
        matches, _mode = detect_chapters(text)
        segs = _segments_from_rule_matches(text, matches)
        if len(segs) >= 2:
            bodies = [body for _title, body in segs]
        else:
            # 兜底：与 phase0 一致，按 3000 字硬切
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
    # 少于 MIN_CHAPTER_CHARS 的段在 phase0 会被标成空章并跳过，这里同判据
    return [
        estimate_tokens(b) for b in bodies if len(b.strip()) >= MIN_CHAPTER_CHARS
    ]


def _blueprint_calls(context_window: int, summary_tokens: int) -> tuple[int, int, int]:
    """分卷压缩的 (调用数, 输入 token, 输出 token)。

    照抄 blueprint.compress_summaries 的算式：预算 = context_window*1.5*0.60，
    每轮的块预算 = max(1200, 预算*0.72)，单次输出 target = clamp(240, 900, 块长*0.22)。
    """
    budget = _chars_to_tokens(context_window * 1.5 * 0.60)
    if summary_tokens <= budget:
        return 0, 0, 0
    chunk_budget = max(1200, _chars_to_tokens(budget * 0.72))
    calls = 0
    in_tokens = 0.0
    out_tokens = 0.0
    total = float(summary_tokens)
    for _round in range(4):  # max_rounds=4
        if total <= budget:
            break
        n_chunks = max(1, math.ceil(total / chunk_budget))
        per_chunk = total / max(1, n_chunks)
        target = max(240, min(900, per_chunk * 0.22))
        # 只有块内多于一条摘要时才会被压缩（compress_summaries 对单条块直接原样保留）
        compressible = n_chunks if per_chunk > 240 else 0
        calls += compressible
        in_tokens += compressible * (per_chunk + 150)  # 150 ≈ 压缩提示词骨架
        out_tokens += compressible * target
        total = compressible * target + (n_chunks - compressible) * per_chunk
    return calls, _chars_to_tokens(in_tokens), _chars_to_tokens(out_tokens)


def estimate_run(req) -> dict:
    """返回一次运行的规模与 token 预估（下界）。"""
    sizes = chapter_token_sizes(req)
    if not sizes:
        raise ValueError("没有可处理的章节（全是空章）")

    cw = int(req.context_window)
    style = getattr(req, "author_style", "") or ""
    style_tokens = estimate_tokens(style)
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
    overlap_ratio = 0.375  # splitter.phase1_diagnose: overlap_limit = body_limit * 0.375
    diag_in = sum(
        b + int(b * overlap_ratio) + DIAG_FIXED_TOKENS + style_tokens for b in batch_sizes
    )
    diag_out = sum(
        min(DIAG_MAX_TOKENS, len(bat) * DIAG_TOKENS_PER_CHAPTER + DIAG_TOKENS_FLOOR)
        for bat in batches
    )

    # ---- 阶段 2 蓝图 ----
    map_calls, map_in, map_out = _blueprint_calls(cw, diag_out)
    summary_ctx = min(diag_out + map_out, _chars_to_tokens(cw * 1.5 * 0.60))
    gap_reserve = min(GAP_DIGEST_MAX_CHARS, summary_ctx // 3)
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
    extract = _chars_to_tokens(cw * 1.5 * 0.075)
    bp_for_stitch = _chars_to_tokens(cw * 1.5 * 0.10)
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
        "notes": [
            "预估为下界：不含自动重写、拆批重跑、上下文超限重试、单章重生成",
            "拆章按规则识别估算；真跑若触发自定义标记/AI 拆章，批数可能相差一两个批次",
            "缝合只算批次边界，虚拟章边界未计入",
        ],
    }
