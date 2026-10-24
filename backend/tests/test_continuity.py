"""跨批连续性上下文与预算预留的单元测试。"""

import tempfile
from pathlib import Path

from core.budget import (
    body_token_budget,
    continuity_token_allowance,
    story_memo_token_allowance,
)
from core.refactor import (
    CONTINUITY_MARK,
    STORY_MEMO_MARK,
    _build_refactor_prompt,
    _compact_story_memo,
    _continuity_for_batch,
    _story_memo_for_batch,
    _tail_for_prompt,
)
from utils.tokens import estimate_tokens


def _mk_output(tmp: str, *, reconstructed: str = "", original: str = "") -> Path:
    out = Path(tmp)
    split = out / "02_workspace/split"
    recon = out / "02_workspace/reconstructed"
    split.mkdir(parents=True)
    recon.mkdir(parents=True)
    (split / "1.txt").write_text(original, encoding="utf-8")
    (split / "2.txt").write_text("第二章原文", encoding="utf-8")
    if reconstructed:
        (recon / "chapter_1.txt").write_text(reconstructed, encoding="utf-8")
    return out


def test_tail_keeps_end_and_prefers_sentence_boundary():
    text = "开头。" + "中间内容。" * 200 + "这是真正的结尾。"
    tail = _tail_for_prompt(text, token_limit=80)
    assert tail.startswith("……")
    assert "这是真正的结尾。" in tail
    assert estimate_tokens(tail) <= 80


def test_tail_never_exceeds_the_reserved_tokens():
    """反向断言：旧实现按 600 **字符**裁，而 8000 窗口只预留 320 token，
    注入的片段能超出预留近一倍 —— 这条就是钉住那个口径统一的。"""
    for window in (8000, 16384, 32768, 131072):
        reserve = continuity_token_allowance(window)
        text = "他站在门口。" * 6000          # 远超预算的章尾
        tail = _tail_for_prompt(text, token_limit=reserve)
        assert tail, "不该被裁成空"
        assert estimate_tokens(tail) <= reserve, (
            f"窗口 {window}：预留 {reserve} token，实际注入 {estimate_tokens(tail)}")


def test_tail_short_text_returned_whole_and_empty_limits_are_empty():
    assert _tail_for_prompt("短短一句。", token_limit=320) == "短短一句。"
    assert _tail_for_prompt("", token_limit=320) == ""
    assert _tail_for_prompt("有内容", token_limit=2) == ""      # 预算过小就不注入
    assert _tail_for_prompt("有内容", token_limit=0) == ""


def test_continuity_serial_uses_reconstructed_text():
    with tempfile.TemporaryDirectory() as tmp:
        out = _mk_output(tmp, reconstructed="上一批重构结尾。", original="上一批原文结尾。")
        progress = {
            "chapters": [
                {"id": "1", "split_file": "02_workspace/split/1.txt", "is_empty": False},
                {"id": "2", "split_file": "02_workspace/split/2.txt", "is_empty": False},
            ]
        }
        text = _continuity_for_batch(
            out, progress, {"chapter_ids": ["2"]}, token_limit=320)
    assert text == "上一批重构结尾。"


def test_continuity_falls_back_to_original_when_recon_missing():
    # 并发时同波次前批可能还没写出重构稿，必须退回该章原文而不是跳过衔接。
    with tempfile.TemporaryDirectory() as tmp:
        out = _mk_output(tmp, reconstructed="", original="上一批原文结尾。")
        progress = {
            "chapters": [
                {"id": "1", "split_file": "02_workspace/split/1.txt", "is_empty": False},
                {"id": "2", "split_file": "02_workspace/split/2.txt", "is_empty": False},
            ]
        }
        text = _continuity_for_batch(
            out, progress, {"chapter_ids": ["2"]}, token_limit=320)
    assert text == "上一批原文结尾。"


def test_prompt_injects_continuity_before_batch_body():
    prompt = _build_refactor_prompt(
        "full_rewrite", "蓝图", "风格", "本批正文", n_chapters=1,
        continuity="上一批结尾片段。",
    )
    assert CONTINUITY_MARK in prompt
    assert prompt.index(CONTINUITY_MARK) < prompt.rindex("【待处理正文】")
    assert "不要复述" in prompt


def test_budget_reserves_continuity_tokens():
    window = 16000
    reserve = continuity_token_allowance(window)
    assert 200 <= reserve <= 600
    assert body_token_budget(window, reserve_tokens=reserve) < body_token_budget(window)
    # 预留不得把极小窗口压成负数或无下限
    assert body_token_budget(8000, reserve_tokens=reserve) >= 200


def test_compact_story_memo_drops_gap_diagnosis_and_json():
    raw = (
        "1. 章节摘要\n主角进城，遇见旧友。\n"
        "2. 人物状态\n主角受伤，旧友开始怀疑他。\n"
        "3. 断层诊断\n转场生硬。\n"
        '```json\n{"gaps": []}\n```'
    )
    memo = _compact_story_memo(raw, 500)
    assert "主角进城" in memo
    assert "旧友开始怀疑" in memo
    assert "转场生硬" not in memo
    assert '"gaps"' not in memo


def test_story_memo_uses_nearest_previous_non_overlapping_batch():
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        summary_dir = out / "01_summaries"
        summary_dir.mkdir(parents=True)
        (summary_dir / "batch_01.txt").write_text("第一章前情：旧城往事。", encoding="utf-8")
        (summary_dir / "batch_02.txt").write_text("第二章前情：主角受伤。", encoding="utf-8")
        progress = {
            "batches": [
                {"batch_id": 1, "chapter_ids": ["1"]},
                {"batch_id": 2, "chapter_ids": ["2"]},
                {"batch_id": 3, "chapter_ids": ["3"]},
            ]
        }
        memo = _story_memo_for_batch(
            out, progress, {"batch_id": 3, "chapter_ids": ["3"]}, token_limit=200)
    assert memo == "第二章前情：主角受伤。"


def test_prompt_injects_story_memo_before_batch_body():
    prompt = _build_refactor_prompt(
        "full_rewrite", "蓝图", "风格", "本批正文", n_chapters=1,
        continuity="上一批结尾。", story_memo="主角在上一批受伤。",
    )
    assert STORY_MEMO_MARK in prompt
    assert prompt.index(STORY_MEMO_MARK) < prompt.rindex("【待处理正文】")
    assert prompt.index(CONTINUITY_MARK) < prompt.rindex("【待处理正文】")


def test_budget_reserves_story_memo_tokens():
    window = 16000
    reserve = story_memo_token_allowance(window)
    assert 180 <= reserve <= 500
    assert body_token_budget(window, reserve_tokens=reserve) < body_token_budget(window)
