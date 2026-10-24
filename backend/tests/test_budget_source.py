"""窗口派生系数与结构判据只有一个来源：`core.budget` / `core.blueprint` / `core.splitter`。

为什么专门给它写用例：开跑前预估要替真跑算钱，而它最初是**照抄**真跑的系数写的。
本会话里那批抄写已经真的漂过两次 —— 诊断输出上限预估按 160/400/2000 算，
真跑却是 260/600/900/4000；分卷压缩的调用数按平均块长估，把真跑不调模型的单条块
也算成一次调用。两类错误的共同点是**界面照样出数**，只是数字是错的。
这里的断言不复述数值，而是禁止再出现第二份拷贝：一层字符串门禁，一层行为门禁
（改源头常数，两边必须一起动）。
"""

import asyncio
import inspect
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import batching, blueprint, budget, estimate, pipeline, refactor, splitter
from core import stitch as stitch_mod
from routes import control

_SENT = "他推开门，屋里静得能听见自己的心跳声。"


def _book(tmp: str, chapters: int = 6) -> Path:
    p = Path(tmp) / "book.txt"
    p.write_text("\n\n".join(f"第{i}章 起事\n{_SENT * 40}" for i in range(1, chapters + 1)),
                 encoding="utf-8")
    return p


def _req(path, **kw):
    base = dict(input_path=str(path), input_mode="single_file", input_format="txt",
                output_path=None, context_window=8000, author_style="出版级文学重构",
                refactor_gates={}, diagnose_json=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_chapter_cap_has_one_source():
    for fn in (batching.pack_groups, splitter._load_batches):
        default = inspect.signature(fn).parameters["max_chapters"].default
        assert default == budget.MAX_CHAPTERS_PER_BATCH, f"{fn.__name__} 又写了批章数上限"
    assert estimate.MAX_CHAPTERS_PER_BATCH is budget.MAX_CHAPTERS_PER_BATCH


def test_stitch_window_helper_shape():
    small = 8000 * 1.5
    assert budget.stitch_window_chars(small) == int(small * budget.STITCH_WINDOW_RATIO)
    assert budget.stitch_window_chars(small) < budget.STITCH_MAX_WINDOW_CHARS
    huge = 128000 * 1.5
    assert budget.stitch_window_chars(huge) == budget.STITCH_MAX_WINDOW_CHARS, "封顶失效"


def test_estimate_and_real_path_share_the_window(monkeypatch):
    """预估报出的交界窗口必须等于真跑实际用的窗口（同一个函数，不是同一个数字）。"""
    with tempfile.TemporaryDirectory() as tmp:
        book = _book(tmp)
        for cw in (8000, 32768, 131072):
            est = estimate.estimate_run(_req(book, context_window=cw))
            assert est["stitch_window_chars"] == budget.stitch_window_chars(cw * 1.5), cw
            assert est["max_chapters_per_batch"] == budget.MAX_CHAPTERS_PER_BATCH


def test_no_second_copy_of_window_ratios():
    """这些模块里不该再出现自己抄的窗口系数（数值本身在 budget 里改才生效）。"""
    banned = {
        estimate: ("0.375", "0.075", "0.60", "0.10", "* 1.5", "MAX_CHAPTERS_PER_BATCH = ",
                   "0.72", "0.22", "max(1200", "range(4)", "max(240", "min(900",
                   "GAP_DIGEST_MAX_CHARS = ", "FALLBACK_SEGMENT_CHARS = ",
                   "CHAPTER_MIN_CHARS = "),
        splitter: ("0.375", "max_chapters=8", "* 1.5"),
        batching: ("max_chapters=8",),
        refactor: ("chars * 0.10", "chars * 0.075", "* 1.5"),
        pipeline: ("chars * 0.05", "min(5000, max(2000", "* 1.5"),
        blueprint: ("chars * 0.60", "* 1.5"),
        control: ("chars * 0.20", "* 1.5"),
        stitch_mod: ("0.075", "STITCH_MAX_WINDOW_CHARS = "),
    }
    for mod, needles in banned.items():
        src = inspect.getsource(mod)
        for needle in needles:
            assert needle not in src, (
                f"{mod.__name__} 里仍有 {needle!r}（应收敛到 budget/blueprint/splitter 的唯一来源）"
            )


def test_confirm_endpoint_never_rejects_a_generated_blueprint():
    """确认接口接受的蓝图长度必须 ≥ phase2 自己的生成上限。

    旧写法是 `int(context_window * 1.5 * 0.20)`，与生成上限各算一份：窗口小时
    接受上限反而**低于**生成上限，模型合法产出的蓝图，用户在闸门里原样点确认
    会被这个接口 422 拒掉 —— 蓝图闸门就此变成死路。
    """
    for cw in (500, 1000, 2000, 3000, 4096, 8000, 16384, 32768, 131072, 262144):
        assert budget.blueprint_accept_max_chars(cw) >= budget.blueprint_chars(cw), cw
    # 反向断言：旧算式确实会违规，否则这条护栏只是空转
    would_break = [
        cw for cw in range(2000, 20000, 500)
        if int(cw * 1.5 * 0.20) < budget.blueprint_chars(cw)
    ]
    assert would_break, "旧算式已不再违规，请重新设计这条护栏的用例"


def test_blueprint_allowance_equals_generation_cap():
    """重构预留与 phase2 生成上限必须是同一个数，否则预留永远对不上实际长度。"""
    for cw in (8000, 16384, 32768, 131072):
        assert budget.blueprint_token_allowance(cw) == budget.blueprint_chars(cw)
        assert budget.blueprint_chars(cw) <= budget.BLUEPRINT_MAX_CHARS


def test_estimate_uses_splitter_phase0_rules(monkeypatch):
    """识别不到标题时的兜底段长，预估必须跟着 splitter 走。

    预估原本自己写了一份 `FALLBACK_SEGMENT_CHARS = 3000` 和 `MIN_CHAPTER_CHARS = 50`。
    这两条是 phase0 的**结构判据**：它们变了，段数、章数、批数全变，预估报出的
    规模和花费就与真跑不符 —— 而界面不会有任何提示。
    """
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "book.txt"
        p.write_text("没有标题的连续正文。" * 500, encoding="utf-8")
        req = _req(p)
        before = len(estimate.chapter_token_sizes(req))
        assert before >= 2, "用例失去意义：这段文本没走到兜底切分"
        monkeypatch.setattr(splitter, "FALLBACK_SEGMENT_CHARS", 1000)
        after = len(estimate.chapter_token_sizes(req))
        assert after > before, "预估把兜底段长抄成了自己的数字"

        monkeypatch.setattr(splitter, "FALLBACK_SEGMENT_CHARS", 3000)
        # 空章判据同理：把门槛抬到比所有段都长，预估应当一段都不剩
        monkeypatch.setattr(splitter, "EMPTY_CHAPTER_MIN_CHARS", 100000)
        assert estimate.chapter_token_sizes(req) == [], "预估把空章判据抄成了自己的数字"


def _real_map_targets(cw: int, items: list[str]) -> tuple[int, int]:
    """真跑压缩这批摘要：返回 (调用次数, 首次请求的输出长度)。"""
    seen: dict = {}

    async def fake_map(prompt: str, target: int) -> str:
        seen.setdefault("first_target", target)
        # 模型常把「不超过 target 字」写满 —— 这是最难压的情形，正好让第二轮还在跑，
        # 「轮数上限」这条分支才会被走到（第一轮就收敛的话，改轮数上限两边都不动）。
        return "压" * max(1, target)

    with tempfile.TemporaryDirectory() as tmp:
        _out, meta = asyncio.run(blueprint.compress_summaries(
            items,
            budget=budget.blueprint_context_chars(budget.window_char_capacity(cw)),
            map_call=fake_map,
            output_path=tmp,
            model="m",
            prompt_version="v",
        ))
    return meta["map_calls"], seen.get("first_target", 0)


@pytest.mark.parametrize("name,value", [
    ("MAP_CHUNK_RATIO", 0.4),
    ("MAP_TARGET_MAX_CHARS", 1800),
    ("MAX_COMPRESS_ROUNDS", 1),
])
def test_compression_constants_move_both_sides(monkeypatch, name, value):
    """改真跑的压缩常数，预估必须跟着变 —— 它读的是同一个来源，不是抄的数字。

    这条是 `test_no_second_copy_of_window_ratios` 的行为版：字符串门禁挡不住
    `int(budget * 0.4)` 这类换了写法的抄写，而「源头改了、下游不动」一定会暴露。
    """
    cw = 8000
    # 300 段 × ~225 字 ≈ 6.7 万字，远大于 8000 窗口的蓝图预算（7200 字）：
    # 第一轮压不完，才会真正走到「轮数上限」这条分支上。
    items = ["第%d段摘要：%s" % (i, "他推开门，屋里静得能听见自己的心跳声。" * 12)
             for i in range(300)]
    sizes = [len(x) for x in items]
    base_calls, base_target = _real_map_targets(cw, items)
    base = estimate._blueprint_calls(cw, sizes)
    monkeypatch.setattr(blueprint, name, value)
    moved_calls, moved_target = _real_map_targets(cw, items)
    after = estimate._blueprint_calls(cw, sizes)
    assert (moved_calls, moved_target) != (base_calls, base_target), "真跑侧没读到该常数"
    assert after != base, f"预估把 {name} 抄成了自己的数字"


def test_predicted_map_calls_equal_the_real_run():
    """同一批摘要：预估说调几次模型、每次写多少字，真跑就正好是这个数。

    靠的是预估直接调用 `blueprint.chunk_by_budget` 分块，而不是自己算平均块长。
    旧算法把真跑「原样保留、不调模型」的单条块也算成调用，因此**多**算 ——
    与本模块「预估是下界」的声明相反。
    摘要长度取互不相同的值：两块内容若完全相同，真跑会命中压缩缓存少调一次。
    """
    cw = 8000
    # 12 条常规摘要 + 1 条 6200 字的长摘要。长摘要自己占一块（块预算 5184 字），
    # 真跑对它**不调模型**（单条块原样保留）—— 少了这一条，用例就测不出多算。
    sizes = [900 + 7 * i for i in range(12)] + [6200]
    items = ["摘" * n for n in sizes]
    targets: list[int] = []

    async def fake_map(prompt: str, target: int) -> str:
        targets.append(target)
        return "压" * target

    with tempfile.TemporaryDirectory() as tmp:
        _out, meta = asyncio.run(blueprint.compress_summaries(
            items,
            budget=budget.blueprint_context_chars(budget.window_char_capacity(cw)),
            map_call=fake_map,
            output_path=tmp,
            model="m",
            prompt_version="v",
        ))
    assert meta["map_calls"] > 1, "用例失去意义：只调一次测不出分块是否同源"
    calls, _in, out_tokens = estimate._blueprint_calls(cw, sizes)
    assert calls == meta["map_calls"], f"预估 {calls} 次，真跑 {meta['map_calls']} 次"
    assert out_tokens == sum(targets), "预估的输出字数与真跑逐次请求的上限之和不符"
