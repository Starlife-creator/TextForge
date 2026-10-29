"""R4/R12（B2.1）：写 progress.json 必须对着**磁盘当前内容**改，不能用旧快照整份盖。

真实交错（确定性复刻，不靠线程概率）：两次单章重写各自在**一次 LLM 调用之前**读到
快照，几十秒后先后提交。负向对照用「读一次 + 整份写」把这个失败模式钉出来，正向
用例走 merge_progress_write（提交时重读磁盘）——同样的交错下两份结论都在。
"""
from __future__ import annotations

import copy
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

from core.quality import has_blocking_issues
from core.refactor import _refresh_qc_after_regen
from utils.atomic import atomic_write_json
from utils.progress_io import merge_progress_write

CLEAN = "重构后的内容。" * 300
DIRTY = "TODO：这里待补。"


def _req():
    return SimpleNamespace(forbidden_canon=[], name_map={}, refactor_mode="full_rewrite",
                           refactor_gates={})


def _bad(cid: str, code: str, message: str) -> dict:
    return {"severity": "bad", "code": code, "chapter_id": cid,
            "message": message, "evidence": ""}


def _layout(tmp_path: Path) -> Path:
    """两章同批，各带一条旧硬伤结论；返回 progress.json 路径。"""
    out = tmp_path
    split = out / "02_workspace" / "split"
    split.mkdir(parents=True)
    progress = {
        "chapters": [{"id": cid, "title": f"第{cid}章",
                      "split_file": f"02_workspace/split/{cid}.txt", "is_empty": False}
                     for cid in ("1", "2")],
        "batches": [{"batch_id": 1, "chapter_ids": ["1", "2"], "status": "phase3_done"}],
        "batch_qc": {"1": [_bad("1", "placeholder", "旧：章节 1 疑似占位"),
                           _bad("2", "content_collapse", "旧：章节 2 塌缩")]},
        "pre_stitch_issues": [_bad("1", "placeholder", "旧"), _bad("2", "content_collapse", "旧")],
        # 期间流水线/验收端点写过的键：整份盖盘的后果就体现在这两个键上
        "accepted_batches": [7], "regen_count": {"5": 2},
    }
    for cid in ("1", "2"):
        (split / f"{cid}.txt").write_text("原文内容。" * 300, encoding="utf-8")
    pf = out / "progress.json"
    atomic_write_json(pf, progress)
    return pf


def _codes(pf: Path) -> set[tuple[str, str]]:
    disk = json.loads(pf.read_text(encoding="utf-8"))
    return {(str(i.get("chapter_id")), str(i.get("code")))
            for i in (disk.get("batch_qc") or {}).get("1", [])}


def test_stale_snapshot_write_reverts_the_other_chapter(tmp_path):
    """负向对照（**必须红在旧写法上**）：读一次 + 整份写，后写者把先写者的清理盖回去。"""
    pf = _layout(tmp_path)
    snapshot = json.loads(pf.read_text(encoding="utf-8"))     # 两位写者同时读到

    a = copy.deepcopy(snapshot)
    _refresh_qc_after_regen(_req(), tmp_path, a, "1", CLEAN)   # 章节 1 重写干净了
    atomic_write_json(pf, a)

    b = copy.deepcopy(snapshot)
    _refresh_qc_after_regen(_req(), tmp_path, b, "2", DIRTY)   # 章节 2 重写引入新硬伤
    atomic_write_json(pf, b)

    got = _codes(pf)
    assert ("1", "placeholder") in got, (
        "本用例描述的是旧写法「读一次 + 整份写」的缺陷本身：后写者用 LLM 调用前的快照，"
        "把先写者刚清掉的旧指控又盖回盘上")


def test_merge_write_keeps_both_regen_conclusions(tmp_path):
    """正向：同样的交错走 merge（提交时重读磁盘）⇒ 两章的结论都在，旧账不复活。"""
    pf = _layout(tmp_path)
    out = tmp_path

    merge_progress_write(pf, lambda d: _refresh_qc_after_regen(_req(), out, d, "1", CLEAN))
    merge_progress_write(pf, lambda d: _refresh_qc_after_regen(_req(), out, d, "2", DIRTY))

    got = _codes(pf)
    assert ("1", "placeholder") not in got, "章节 1 已重写干净，旧指控必须消失"
    assert ("1", "content_collapse") not in got
    assert ("2", "placeholder") in got or ("2", "content_collapse") in got, (
        f"章节 2 的新硬伤必须在册：{got!r}")
    assert has_blocking_issues(
        json.loads(pf.read_text(encoding="utf-8"))["batch_qc"]["1"])


def test_merge_write_preserves_keys_written_by_others(tmp_path):
    """本次只改 batch_qc：别人的键（验收选择、重写计数）必须原样留在盘上。"""
    pf = _layout(tmp_path)

    def mutate(disk: dict) -> None:
        disk["batch_qc"] = {"1": [_bad("9", "placeholder", "只改这一处")]}

    merged = merge_progress_write(pf, mutate)

    disk = json.loads(pf.read_text(encoding="utf-8"))
    assert disk["accepted_batches"] == [7], "别人的验收选择被整份盖盘抹掉了"
    assert disk["regen_count"] == {"5": 2}, "重写次数保险丝被抹掉了"
    assert _codes(pf) == {("9", "placeholder")}
    assert merged["batches"] == disk["batches"], "返回值应当就是落盘的那份"


def test_regen_paths_use_the_merge_writer():
    """接线守卫：两条 R4/R12 路径必须真的走 merge，而不是又改回整份写。"""
    from core import refactor
    from routes import control

    src_regen = inspect.getsource(refactor.regen_single_chapter)
    assert "merge_progress_write" in src_regen
    assert 'atomic_write_json(output_path / "progress.json", progress)' not in src_regen

    src_control = inspect.getsource(control.regen_chapter)
    assert "merge_progress_write" in src_control
    assert "updated = progress" not in src_control, (
        "读盘失败退回旧快照 = R12，绝不能装回来")
