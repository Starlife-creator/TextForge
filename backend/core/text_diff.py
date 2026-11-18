"""句子/段落级章节对照 Diff 引擎（供「边跑边审」三栏工作台调用）。

为什么采用分句/分段切块而非字符级 SequenceMatcher：
1. 小说单章通常 2,000~12,000 字，字符级 `difflib.SequenceMatcher` 最坏退化至 O(N²)
   （单章可卡顿数百毫秒至数秒），且会把文学润色切成难以阅读的单字碎片。
2. 按句末标点与自然换行切成阅读单元（通常 80~350 句），`SequenceMatcher(autojunk=False)`
   在 1~3 ms 内即可精确对齐「未改原句 / 删减句 / 新增过渡句 / 整句改写」，直接驱动前端高亮。
"""

from __future__ import annotations

import difflib
import re
from typing import Any

# 保留句末标点（及紧随引号）与段落换行在切块末尾，拼接全部块可无损还原正文。
_UNIT_RE = re.compile(r"[^。！？…；!?;.\n]+(?:[。！？…；!?;.]+[”’\"'）)】》]*|\n+)?|\n+")


def _tokenize_units(text: str) -> list[str]:
    if not text:
        return []
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return [m.group(0) for m in _UNIT_RE.finditer(normalized) if m.group(0)]


def _non_ws_len(text: str) -> int:
    return len(re.sub(r"\s+", "", text or ""))


def compute_chapter_diff(original: str, refactored: str) -> dict[str, Any]:
    """计算单章原文与重构稿的结构化分句/分段 Diff 及统计摘要。"""
    orig_clean = (original or "").replace("\r\n", "\n").replace("\r", "\n")
    ref_clean = (refactored or "").replace("\r\n", "\n").replace("\r", "\n")

    orig_units = _tokenize_units(orig_clean)
    ref_units = _tokenize_units(ref_clean)

    matcher = difflib.SequenceMatcher(None, orig_units, ref_units, autojunk=False)
    blocks: list[dict[str, Any]] = []

    equal_blocks = 0
    insert_blocks = 0
    delete_blocks = 0
    replace_blocks = 0

    equal_chars = 0
    inserted_chars = 0
    deleted_chars = 0

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        orig_seg = "".join(orig_units[i1:i2])
        ref_seg = "".join(ref_units[j1:j2])
        c_orig = _non_ws_len(orig_seg)
        c_ref = _non_ws_len(ref_seg)

        if tag == "equal":
            equal_blocks += 1
            equal_chars += c_orig
        elif tag == "delete":
            delete_blocks += 1
            deleted_chars += c_orig
        elif tag == "insert":
            insert_blocks += 1
            inserted_chars += c_ref
        else:
            tag = "replace"
            replace_blocks += 1
            deleted_chars += c_orig
            inserted_chars += c_ref

        blocks.append(
            {
                "op": tag,
                "original_text": orig_seg,
                "refactored_text": ref_seg,
                "char_original": c_orig,
                "char_refactored": c_ref,
            }
        )

    total_orig = _non_ws_len(orig_clean)
    total_ref = _non_ws_len(ref_clean)
    denom = max(total_orig, total_ref)
    if denom == 0:
        change_ratio = 0.0
    elif orig_clean.strip() == ref_clean.strip():
        change_ratio = 0.0
    else:
        kept_ratio = equal_chars / denom
        change_ratio = round(max(0.0, min(1.0, 1.0 - kept_ratio)), 4)

    length_ratio = round(total_ref / total_orig, 4) if total_orig > 0 else None

    return {
        "diff_blocks": blocks,
        "stats": {
            "char_original": total_orig,
            "char_refactored": total_ref,
            "char_delta": total_ref - total_orig,
            "length_ratio": length_ratio,
            "change_ratio": change_ratio,
            "equal_blocks": equal_blocks,
            "insert_blocks": insert_blocks,
            "delete_blocks": delete_blocks,
            "replace_blocks": replace_blocks,
            "equal_chars": equal_chars,
            "inserted_chars": inserted_chars,
            "deleted_chars": deleted_chars,
        },
    }
