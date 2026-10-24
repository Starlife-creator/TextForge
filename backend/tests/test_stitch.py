"""缝合契约、锚点落盘与失败降级的单元测试。"""

import asyncio
import json
import tempfile
from pathlib import Path

import core.api_client as api_client
from core.stitch import (
    _record_anchors,
    _record_stitch_failure,
    _stitch_common,
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
