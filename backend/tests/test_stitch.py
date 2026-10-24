"""缝合契约、锚点落盘与失败降级的单元测试。"""

import asyncio
import json
import tempfile
from pathlib import Path

import core.api_client as api_client
from core.budget import STITCH_MAX_WINDOW_CHARS, STITCH_WINDOW_RATIO, stitch_window_chars
from core.stitch import (
    _record_anchors,
    _record_stitch_failure,
    _stitch_common,
    _stitch_retry_hook,
    apply_stitches,
    check_replacement_length,
    parse_stitch_result,
)


def _uniq(n=60, tag="章"):
    """每行都不同的长文本：重复文本会让 shingle 集合自碰撞，改动率失真。"""
    return "".join(f"第{i:02d}{tag}内容含{i * 7 % 100}号标记。" for i in range(n))


def test_parse_stitch_result():
    prev, nxt = parse_stitch_result(
        "===PREV_END===\n前文改写。\n===NEXT_START===\n后文改写。"
    )
    assert prev == "前文改写。"
    assert nxt == "后文改写。"


def test_replacement_length_guard_rejects_truncation():
    original = "原文内容。" * 100
    check_replacement_length(original, original + "补充过渡。")
    try:
        check_replacement_length(original, "只返回了一句过渡。")
    except ValueError as e:
        assert "过短" in str(e)
    else:
        raise AssertionError("过短的替换段必须被拒绝")


def test_record_anchors_writes_full_jsonl_and_dedupes():
    patch = {
        "file": "02_workspace/reconstructed/chapter_1.txt",
        "start": 10,
        "end": 20,
        "original": "原文片段",
        "replacement": "替换片段",
        "before_hash": "sha256:abc",
        "applied": True,
        "source": "batch_stitch",
    }
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        progress = {}
        _record_anchors(progress, [patch], out)
        _record_anchors(progress, [dict(patch)], out)
        lines = (out / "_diagnostics/stitch_anchors.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()

    assert len(lines) == 1
    saved = json.loads(lines[0])
    assert saved["original"] == "原文片段"
    assert saved["replacement"] == "替换片段"
    assert len(progress["stitch_anchors"]) == 1
    assert "original" not in progress["stitch_anchors"][0]


def _drive_stitch(monkeypatch, prev_text, next_text, out_text, *, chars,
                  minimal_change=None):
    """跑一遍真实的 `_stitch_common`，把模型返回换成 `out_text`。

    桩必须调用 `validate`：契约校验（标记/长度/改动率）就挂在 call_with_retry 的
    validate 回调上，直接返回文本会让被测代码绕过全部校验，测试结果不作数。
    """
    seen = {}

    async def fake_call(client, api_url, payload, emit, run_id, **kw):
        seen["payload"] = payload
        validate = kw.get("validate")
        if validate:
            validate(out_text)
        return out_text, {"prompt_tokens": 10, "completion_tokens": 20}, 0

    monkeypatch.setattr(api_client, "call_with_retry", fake_call)
    prev_rel = "02_workspace/reconstructed/chapter_1.txt"
    next_rel = "02_workspace/reconstructed/chapter_2.txt"
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        for rel, text in ((prev_rel, prev_text), (next_rel, next_text)):
            f = out / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text, encoding="utf-8")
        payload_base = {
            "output_path": str(out), "api_url": "http://mock/v1/chat/completions",
            "blueprint": "B", "author_style": "S", "progress": {},
            "payload": {"model": "m"},
        }
        if minimal_change is not None:
            payload_base["minimal_change"] = minimal_change
        patches = asyncio.run(_stitch_common(
            prev_rel, next_rel, out, payload_base, None, None, "run", chars))
    return patches, seen


def test_stitch_window_is_capped_so_it_cannot_swallow_a_chapter(monkeypatch):
    """缝合窗口必须封顶：窗口原样随 context_window 线性放大，128k 下会算出 1.4 万字。

    那时「修补交界处」实际上变成「整段替换一整章」，而长度比 0.5~3.0 对整段重写
    完全不设防（重写后长度差不多一样）。这里断言的是**封顶后**的覆盖面积，
    并且面积取自 `budget.stitch_window_chars` —— 真跑与预估必须共用同一个函数。
    """
    chars = 128000 * 1.5
    expected = stitch_window_chars(chars)
    assert expected == STITCH_MAX_WINDOW_CHARS, "128k 窗口下封顶应当生效"
    assert int(chars * STITCH_WINDOW_RATIO) > STITCH_MAX_WINDOW_CHARS
    prev_text = _uniq(2000, "前")
    next_text = _uniq(2000, "后")
    assert len(prev_text) > expected and len(next_text) > expected
    prev_seg = prev_text[-expected:]
    next_seg = next_text[:expected]
    out_text = f"===PREV_END===\n{prev_seg}\n===NEXT_START===\n{next_seg}\n"

    patches, seen = _drive_stitch(monkeypatch, prev_text, next_text, out_text, chars=chars)

    assert [p["end"] - p["start"] for p in patches] == [expected, expected], (
        f"窗口未封顶：实际覆盖 {patches[0]['end'] - patches[0]['start']} 字")
    assert patches[0]["start"] == len(prev_text) - expected
    prompt = seen["payload"]["messages"][0]["content"]
    assert "第00前" not in prompt, "章首正文不该再进缝合提示词"


def test_minimal_change_gate_rejects_wholesale_rewrite(monkeypatch):
    """闸门开启时，长度合规但句子全换的补丁必须被拒绝（并交给降级路径）。"""
    prev_seg = _uniq(90, "前")[:900]
    next_seg = _uniq(90, "后")[:900]
    bulk = _uniq(90, "改")[:900]  # 与原文等长：长度比护栏放行，只有改动率能挡住
    assert len(bulk) == len(prev_seg)
    out_text = f"===PREV_END===\n{bulk}\n===NEXT_START===\n{next_seg}\n"

    try:
        _drive_stitch(monkeypatch, prev_seg, next_seg, out_text,
                      chars=8000 * 1.5, minimal_change=True)
    except Exception as e:
        assert "改动过大" in str(e), f"错因应是改动率闸门，实测：{type(e).__name__}: {e}"
    else:
        raise AssertionError("整段重写必须被最小改动闸门拒绝")


def test_gate_off_keeps_legacy_behaviour(monkeypatch):
    """关掉闸门即回到旧行为：只受长度比约束，整段重写照样落地（改动率仍被记录）。"""
    prev_seg = _uniq(90, "前")[:900]
    next_seg = _uniq(90, "后")[:900]
    bulk = _uniq(90, "改")[:900]
    out_text = f"===PREV_END===\n{bulk}\n===NEXT_START===\n{next_seg}\n"

    patches, _ = _drive_stitch(monkeypatch, prev_seg, next_seg, out_text,
                               chars=8000 * 1.5, minimal_change=False)

    assert [p["change_rate"] for p in patches][0] > 0.35
    assert patches[0]["replacement"] == bulk


def test_gate_allows_short_boundaries_it_cannot_measure(monkeypatch):
    """两句以下的短边界没有改动率分辨力：闸门必须放行而不是误杀。

    这是 `change_rate` 返回 None 的既有语义在闸门上的体现，别让「顺手把 None 当拒绝」
    把章尾只有一句话的书全部降级。
    """
    prev_seg = "夜色沉下来。他按上门框。"
    next_seg = "门后是空房间。他没有点灯。"
    out_text = ("===PREV_END===\n月色很亮。她推开窗。\n"
                "===NEXT_START===\n屋里没人。桌上有一封信。\n")

    patches, _ = _drive_stitch(monkeypatch, prev_seg, next_seg, out_text,
                               chars=8000 * 1.5, minimal_change=True)

    assert [p["change_rate"] for p in patches] == [None, None]


def test_stitch_retry_hook_switches_correction_by_error():
    """被拒的原因不同，纠正提示也不同：「照抄」问题不能只叮嘱「完整返回」。"""
    p = {"messages": [], "temperature": 0.8}
    _stitch_retry_hook(p, ValueError("前文结尾改动过大：原文句子有 100% 没有被原样保留"))
    rewrite = p["messages"][-1]["content"]
    assert "逐字照抄" in rewrite and "不要替换或改写任何原有句子" in rewrite
    assert p["temperature"] == 0.4

    p2 = {"messages": []}
    _stitch_retry_hook(p2, ValueError("缝合替换段过短：仅返回 12 字"))
    assert "完整" in p2["messages"][-1]["content"]
    assert "逐字照抄" not in p2["messages"][-1]["content"]


def test_minimal_change_gate_passes_faithful_echo_and_stays_local(monkeypatch):
    """原样返回必须放行（闸门不能把「没改」判成「改太多」），且门控字段不进请求体。"""
    prev_seg = _uniq(90, "前")[:900]
    next_seg = _uniq(90, "后")[:900]
    out_text = f"===PREV_END===\n{prev_seg}\n===NEXT_START===\n{next_seg}\n"
    patches, seen = _drive_stitch(monkeypatch, prev_seg, next_seg, out_text,
                                  chars=8000 * 1.5, minimal_change=True)
    assert [p["change_rate"] for p in patches] == [0.0, 0.0]
    assert "minimal_change" not in seen["payload"], "本地门控字段泄漏给服务商"


def test_stitch_failure_is_recorded_and_bounded():
    events = []
    progress = {}
    _record_stitch_failure(
        progress, 2, "1-2", RuntimeError("模型输出缺标记"),
        lambda ev, data, rid: events.append((ev, data)), "run",
    )
    assert progress["stitch_failures"][0]["boundary"] == "1-2"
    assert "缺标记" in progress["stitch_failures"][0]["error"]
    assert events and events[0][0] == "error"
    assert events[0][1]["kind"] == "warning"

    for i in range(200):
        _record_stitch_failure(progress, 2, f"x-{i}", RuntimeError("x"))
    assert len(progress["stitch_failures"]) == 100


def test_record_anchors_keeps_change_rate_and_covered_chars():
    """轻量引用必须带上改动率与覆盖字数：质量报告只读 progress.json。"""
    patch = {
        "file": "02_workspace/reconstructed/chapter_3.txt",
        "start": 100, "end": 700,
        "original": _uniq(60, "前"), "replacement": _uniq(60, "后"),
        "before_hash": "sha256:xyz", "applied": True,
        "source": "batch_stitch", "change_rate": 0.127,
    }
    progress = {}
    _record_anchors(progress, [patch], None)
    ref = progress["stitch_anchors"][0]
    assert ref["change_rate"] == 0.127
    assert ref["chars"] == 600
    assert ref["applied"] is True


def test_stitch_common_attaches_change_rate(monkeypatch):
    """改动率是在生成补丁时就地测量的，且能区分「插入过渡句」与「没动」。"""
    # 文件长度取恰好等于缝合窗口（chars*1.5*0.075 = 8000*1.5*0.075 = 900 字），
    # 这样提示词里被截出来的段与文件内容一致，桩返回值才能对上真实原文。
    prev_seg = _uniq(90, "前")[:900]
    next_seg = _uniq(90, "后")[:900]
    half = len(prev_seg) // 2
    # 前文结尾：插一句过渡 + 改写其中一句（应当是「小改动」）；后文开头：原样返回
    prev_repl = (prev_seg[:half] + "他停下脚步，回头望了一眼。" + prev_seg[half:]).replace(
        "第10前内容含70号标记", "第10前这里被改写过了"
    )
    out_text = f"===PREV_END===\n{prev_repl}\n===NEXT_START===\n{next_seg}\n"

    async def fake_call(client, api_url, payload, emit, run_id, **kw):
        # 必须真的走 validate：契约校验（标记/长度）就挂在它上面，
        # 直接返回文本会让被测代码绕过校验，测试结果不作数。
        validate = kw.get("validate")
        if validate:
            validate(out_text)
        return out_text, {"prompt_tokens": 10, "completion_tokens": 20}, 0

    monkeypatch.setattr(api_client, "call_with_retry", fake_call)

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        prev_rel = "02_workspace/reconstructed/chapter_1.txt"
        next_rel = "02_workspace/reconstructed/chapter_2.txt"
        for rel, text in ((prev_rel, prev_seg), (next_rel, next_seg)):
            f = out / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text, encoding="utf-8")
        payload_base = {
            "output_path": str(out), "api_url": "http://mock/v1/chat/completions",
            "blueprint": "B", "author_style": "S", "progress": {},
            "payload": {"model": "m"},
        }
        patches = asyncio.run(_stitch_common(
            prev_rel, next_rel, out, payload_base, None, None, "run", 8000 * 1.5))

    assert len(patches) == 2
    rates = [p["change_rate"] for p in patches]
    assert all(r is not None for r in rates), rates
    assert 0 < rates[0] < 0.05, f"插一句过渡+改一句应为小改动，实测 {rates[0]}"
    assert rates[1] == 0.0, f"原样返回的边界改动率必须是 0，实测 {rates[1]}"

    # 顺序修正的另一半：应用之后 applied 必须为真，否则按 applied 汇总的改动率恒为空
    grouped = {}
    for p in patches:
        grouped.setdefault(p["file"], []).append(p)
    progress2 = {}
    with tempfile.TemporaryDirectory() as tmp2:
        out2 = Path(tmp2)
        for rel, text in ((prev_rel, prev_seg), (next_rel, next_seg)):
            f = out2 / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text, encoding="utf-8")
        apply_stitches(out2, grouped)
        _record_anchors(progress2, patches, out2)
    assert [a["applied"] for a in progress2["stitch_anchors"]] == [True, True], (
        "apply_stitches 之后就地把补丁标成已应用，锚点必须读到真值")
    assert [a["chars"] for a in progress2["stitch_anchors"]] == [900, 900]
