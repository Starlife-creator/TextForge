"""缝合契约、锚点落盘与失败降级的单元测试。"""

import json
import tempfile
from pathlib import Path

from core.stitch import (
    _record_anchors,
    _record_stitch_failure,
    check_replacement_length,
    parse_stitch_result,
)


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
