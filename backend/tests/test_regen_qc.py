"""单章重生成后刷新质检结论（T7）的单元测试。"""

import tempfile
from pathlib import Path
from types import SimpleNamespace

from core.quality import has_blocking_issues
from core.refactor import ACCEPT_ABSORB_KEYS, _refresh_qc_after_regen


def _req(**kw):
    base = dict(forbidden_canon=[], name_map={}, refactor_mode="full_rewrite",
                refactor_gates={})
    base.update(kw)
    return SimpleNamespace(**base)


def _bad(cid, code, message):
    return {"severity": "bad", "code": code, "chapter_id": cid,
            "message": message, "evidence": ""}


def _setup(tmp: str, chapters=("1", "2")):
    """两章同批：章节 1、2 各有一条旧的硬伤结论，重写章节 1 只应清掉章节 1 的那条。"""
    out = Path(tmp)
    split = out / "02_workspace/split"
    split.mkdir(parents=True)
    progress = {
        "chapters": [
            {"id": cid, "title": f"第{cid}章",
             "split_file": f"02_workspace/split/{cid}.txt", "is_empty": False}
            for cid in chapters
        ],
        "batches": [{"batch_id": 1, "chapter_ids": list(chapters),
                     "status": "phase3_done"}],
        "batch_qc": {"1": [_bad("1", "placeholder", "旧：章节 1 疑似占位"),
                           _bad("2", "content_collapse", "旧：章节 2 塌缩")]},
        "pre_stitch_issues": [_bad("1", "placeholder", "旧：章节 1 疑似占位"),
                              _bad("2", "content_collapse", "旧：章节 2 塌缩")],
    }
    for cid in chapters:
        (split / f"{cid}.txt").write_text("原文内容。" * 300, encoding="utf-8")
    return out, progress


def _keys(progress, bid="1"):
    return {(str(i.get("chapter_id")), i.get("code"))
            for i in (progress.get("batch_qc") or {}).get(bid, [])}


def test_refresh_clears_only_the_regenerated_chapter():
    with tempfile.TemporaryDirectory() as tmp:
        out, progress = _setup(tmp)
        fresh = _refresh_qc_after_regen(
            _req(), out, progress, "1", "重构后的内容。" * 300)

    assert fresh == [] and not has_blocking_issues(fresh)
    assert ("1", "placeholder") not in _keys(progress), "假警报必须随正文一起消失"
    assert ("2", "content_collapse") in _keys(progress), "同批别的章不能被顺手清掉"
    assert [i["chapter_id"] for i in progress["pre_stitch_issues"]] == ["2"], (
        "派生物 pre_stitch_issues 必须同步刷新，否则报告里的「遗留硬伤」是旧账")


def test_refresh_records_new_issue_from_rewritten_text():
    with tempfile.TemporaryDirectory() as tmp:
        out, progress = _setup(tmp)
        fresh = _refresh_qc_after_regen(_req(), out, progress, "1", "TODO：这里待补。")

    codes = {i["code"] for i in fresh}
    assert {"placeholder", "content_collapse"} <= codes, str(fresh)
    assert ("1", "placeholder") in _keys(progress)
    assert has_blocking_issues((progress["batch_qc"] or {})["1"]), "新硬伤必须进结论"
    assert {i["chapter_id"] for i in progress["pre_stitch_issues"]} == {"1", "2"}


def test_refresh_survives_when_chapter_is_in_several_batches():
    """虚拟章场景：同一章 id 可能同时出现在多个批次的结论里，都要刷新。"""
    with tempfile.TemporaryDirectory() as tmp:
        out, progress = _setup(tmp, chapters=("1", "2"))
        progress["batches"].append(
            {"batch_id": 2, "chapter_ids": ["1"], "status": "phase3_done"})
        progress["batch_qc"]["2"] = [_bad("1", "placeholder", "旧：批次 2 里的章节 1")]
        _refresh_qc_after_regen(_req(), out, progress, "1", "重构后的内容。" * 300)

    assert ("1", "placeholder") not in _keys(progress, "1")
    assert ("1", "placeholder") not in _keys(progress, "2"), "只刷当前批次会留下旧账"
    assert {i["chapter_id"] for i in progress["pre_stitch_issues"]} == {"2"}, (
        "派生物里也不该再有章节 1 的旧账")


def test_accept_gate_absorbs_refreshed_qc_keys():
    """验收门放行时要把刷新后的结论并回内存，否则流水线下一次写盘会覆盖掉。"""
    assert {"batch_qc", "pre_stitch_issues"} <= set(ACCEPT_ABSORB_KEYS)
    assert "accepted_batches" in ACCEPT_ABSORB_KEYS
