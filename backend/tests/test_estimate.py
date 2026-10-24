"""开跑前预估的算式测试：批数必须与真跑同源，且随上下文窗口单调变化。"""

import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.budget import (
    STITCH_MAX_WINDOW_CHARS,
    body_token_budget,
    continuity_token_allowance,
    diagnose_max_tokens,
    story_memo_token_allowance,
)
from core.estimate import chapter_token_sizes, estimate_run
from core.splitter import _load_batches
from utils.tokens import estimate_tokens

_SENT = "他推开门，屋里静得能听见自己的心跳声。"


def _chapter_text(n=40):
    """每章约 760 字（19 字/句 × 40，中文按 1 字 ≈ 1 token）。"""
    return _SENT * n


def _book(tmp: str, chapters: int = 4) -> Path:
    p = Path(tmp) / "book.txt"
    body = "\n\n".join(f"第{i}章 起事\n{_chapter_text()}" for i in range(1, chapters + 1))
    p.write_text(body, encoding="utf-8")
    return p


def _req(input_path, **kw):
    base = dict(
        input_path=str(input_path),
        input_mode="single_file",
        input_format="txt",
        output_path=None,  # 预估不写任何文件
        context_window=8000,
        author_style="出版级文学重构",
        refactor_gates={},
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_estimate_chapter_sizes_follow_rule_detection():
    with tempfile.TemporaryDirectory() as tmp:
        book = _book(tmp, 4)
        sizes = chapter_token_sizes(_req(book))
        est = estimate_run(_req(book))

    assert len(sizes) == 4, "规则识别应切出 4 章"
    assert est["chapters"] == 4
    assert all(700 < s < 800 for s in sizes), sizes


def test_estimate_batches_shrink_as_window_grows():
    """反向断言：窗口变大必须让批数下降。批数若与窗口无关，说明算式没接进来。

    手算依据：每章约 850 token；body_token_budget(8000)≈1851 → 每批 2 章；
    body_token_budget(32768)≈11587 → 8 章（=单批章数上限）合成一批。
    """
    with tempfile.TemporaryDirectory() as tmp:
        book = _book(tmp, 8)
        small = estimate_run(_req(book, context_window=8000))
        big = estimate_run(_req(book, context_window=32768))

    assert small["chapters_per_batch"] == [2, 2, 2, 2], str(small["chapters_per_batch"])
    assert big["chapters_per_batch"] == [8], str(big["chapters_per_batch"])
    assert big["batches"] < small["batches"]
    assert big["calls"]["total"] < small["calls"]["total"]
    assert big["tokens"]["prompt"] < small["tokens"]["prompt"]


def test_estimate_scaffold_share_drops_with_window():
    """窗口越小，每次请求里重复发送的脚手架占比越高 —— 这是小窗口最贵的一笔隐性开销。"""
    with tempfile.TemporaryDirectory() as tmp:
        book = _book(tmp, 8)
        small = estimate_run(_req(book, context_window=8000))
        big = estimate_run(_req(book, context_window=131072))

    assert 0 < small["scaffold_share"] < 1
    assert big["scaffold_share"] < small["scaffold_share"]


def test_estimate_internals_add_up():
    with tempfile.TemporaryDirectory() as tmp:
        est = estimate_run(_req(_book(tmp, 5)))

    by = est["tokens"]["by_stage"]
    assert est["tokens"]["prompt"] == sum(v["prompt"] for v in by.values())
    assert est["tokens"]["completion"] == sum(v["completion"] for v in by.values())
    assert est["calls"]["total"] == (
        est["calls"]["diagnose"] + est["calls"]["blueprint"]
        + est["calls"]["refactor"] + est["calls"]["stitch"]
    )
    assert est["calls"]["diagnose"] == est["calls"]["refactor"] == est["batches"]
    assert est["stitch_boundaries"] == est["batches"] - 1
    assert est["stitch_exposed_chars"] == (
        est["stitch_boundaries"] * 2 * est["stitch_window_chars"]
    )


def test_estimate_batches_match_the_real_packer():
    """防漂移：预估的批划分必须与 phase1 的 _load_batches 逐批一致。

    窗口取 131072：此时 token 上限很宽，真正决定划分的是「单批最多 8 章」这条 ——
    只有在这种配置下，改动章数上限才会让两处结果分叉（用 16k 窗口测不出漂移）。
    """
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        book = _book(tmp, 12)
        est = estimate_run(_req(book, context_window=131072))

        split_dir = out / "02_workspace/split"
        split_dir.mkdir(parents=True)
        chapters = []
        for i in range(1, est["chapters"] + 1):
            (split_dir / f"{i}.txt").write_text(_chapter_text(), encoding="utf-8")
            chapters.append({"id": str(i), "filename": f"{i}.txt",
                             "split_file": f"02_workspace/split/{i}.txt"})
        cw = 131072
        limit = body_token_budget(
            cw, author_style="出版级文学重构",
            reserve_tokens=continuity_token_allowance(cw) + story_memo_token_allowance(cw))
        real = _load_batches(chapters, out, limit)

    assert est["body_token_limit"] == int(limit)
    assert est["chapters_per_batch"] == [8, 4], str(est["chapters_per_batch"])
    assert [len(b) for b in real] == est["chapters_per_batch"], (
        f"真跑装批 {real} 与预估 {est['chapters_per_batch']} 不一致")


def test_estimate_multi_file_skips_empty_chapters():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        for i in range(1, 4):
            (d / f"{i}.txt").write_text(_chapter_text(), encoding="utf-8")
        (d / "4.txt").write_text("   \n\n  ", encoding="utf-8")  # 空章
        req = _req(d, input_mode="multi_file", input_format="txt")
        est = estimate_run(req)
        sizes = chapter_token_sizes(req)

    assert est["chapters"] == 3
    assert len(sizes) == 3


def test_estimate_smooth_gate_drops_stitch_calls():
    with tempfile.TemporaryDirectory() as tmp:
        book = _book(tmp, 6)
        est = estimate_run(_req(book, refactor_gates={"skip_stitch_if_smooth": True}))

    assert est["stitch_boundaries"] == 0
    assert est["calls"]["stitch"] == 0
    assert est["stitch_exposed_chars"] == 0


def test_estimate_rejects_all_empty_book():
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "empty.txt"
        p.write_text("   \n  \n", encoding="utf-8")
        with pytest.raises(ValueError):
            estimate_run(_req(p))


def test_estimate_reports_chapter_token_floor():
    with tempfile.TemporaryDirectory() as tmp:
        text = _chapter_text()
        est = estimate_run(_req(_book(tmp, 1)))

    assert est["chapters"] == 1
    assert est["tokens"]["prompt"] > estimate_tokens(text)
    assert est["body_token_limit"] > 0


def test_estimate_window_knee_justifies_the_ui_default():
    """32768 是拐点：批数在这里触底，再往上放大窗口只是多付脚手架。

    这条是前端默认值（src/presets.ts 的 DEFAULT_CONTEXT_WINDOW）与「为什么不出 128k
    档预设」的依据。若日后预留口径不再随窗口放大，本用例先失败，提醒前端重选默认值。
    """
    with tempfile.TemporaryDirectory() as tmp:
        book = _book(tmp, 60)
        runs = {
            cw: estimate_run(_req(book, context_window=cw))
            for cw in (8000, 16384, 32768, 65536, 131072)
        }

    assert runs[8000]["batches"] > runs[16384]["batches"] > runs[32768]["batches"]
    assert runs[32768]["batches"] == runs[65536]["batches"] == runs[131072]["batches"], (
        "批数在 32768 之后还在下降，拐点判断已过时")
    assert max(runs[32768]["chapters_per_batch"]) == 8, "触底的原因应是单批 8 章上限"
    # 触底之后继续放大窗口：输入变贵而不是变便宜（脚手架随每批重发且预留随窗口放大）
    assert runs[131072]["tokens"]["prompt"] > runs[32768]["tokens"]["prompt"]
    assert runs[32768]["scaffold_share"] < runs[8000]["scaffold_share"]
    assert runs[32768]["scaffold_share"] < runs[131072]["scaffold_share"]


def test_estimate_stitch_window_follows_the_cap():
    """预估的缝合面积必须跟着窗口封顶走，否则界面报出的覆盖面积比真跑的大两倍。"""
    with tempfile.TemporaryDirectory() as tmp:
        book = _book(tmp, 8)
        small = estimate_run(_req(book, context_window=8000))
        huge = estimate_run(_req(book, context_window=128000))

    assert small["stitch_window_chars"] == int(8000 * 1.5 * 0.075)
    assert small["stitch_window_chars"] < STITCH_MAX_WINDOW_CHARS, "语料窗口不该被封顶"
    assert huge["stitch_window_chars"] == STITCH_MAX_WINDOW_CHARS, (
        f"大窗口未封顶：预估 {huge['stitch_window_chars']} 字")
    assert int(128000 * 1.5 * 0.075) > STITCH_MAX_WINDOW_CHARS, "用例失去意义，请换更大窗口"
    # 封顶必须说出来：界面若悄悄按 1200 字报覆盖面积，用户会以为真跑也只动这么点
    assert not any("封顶" in n for n in small["notes"]), str(small["notes"])
    assert any("封顶" in n and "1200" in n for n in huge["notes"]), str(huge["notes"])


def test_estimate_diagnose_output_uses_the_same_formula_as_phase1():
    """诊断输出预留必须与真跑的 `diagnose_max_tokens` 一致：两份算式各写一遍必漂移。"""
    with tempfile.TemporaryDirectory() as tmp:
        book = _book(tmp, 12)
        plain = estimate_run(_req(book))
        with_json = estimate_run(_req(book, diagnose_json="diag_out"))

    per_batch = plain["chapters_per_batch"]
    assert plain["tokens"]["by_stage"]["diagnose"]["completion"] == sum(
        diagnose_max_tokens(n) for n in per_batch), str(per_batch)
    assert with_json["tokens"]["by_stage"]["diagnose"]["completion"] == sum(
        diagnose_max_tokens(n, with_json=True) for n in per_batch), str(per_batch)
    assert (with_json["tokens"]["by_stage"]["diagnose"]["completion"]
            > plain["tokens"]["by_stage"]["diagnose"]["completion"])
