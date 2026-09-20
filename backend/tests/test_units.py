"""TextForge 后端核心逻辑单元测试（D1）。

运行方式（CI 中，backend venv 已装依赖）：
    cd backend
    python -m pytest tests -q
"""
import tempfile
from pathlib import Path

from core.splitter import _parse_diag_json
from core.refactor import _render_gate_rules, _iter_output_chapter_ids, _run_qc
from core.stitch import _is_hard_break


def test_parse_diag_json_fenced():
    r = ('建议补过渡。\n```json\n'
         '{"summary":"摘要A","gaps":[{"type":"plot_jump","severity":"high","location":"第2章开头"},'
         '{"id":"x","type":"plot_jump","severity":"low"}]}```')
    obj = _parse_diag_json(r, 1)
    assert obj is not None and obj["batch_id"] == 1
    assert len(obj["gaps"]) == 2
    assert obj["gaps"][0]["id"] == "B1G1"
    assert obj["gaps"][1]["id"] == "B1G2"  # 稳定 id 覆盖模型给的松散 id


def test_parse_diag_json_unparseable():
    assert _parse_diag_json("没有 json，纯文本逻辑连贯", 2) is None


def test_render_gate_rules_all_off():
    assert _render_gate_rules({}, []) == ""


def test_render_gate_rules_partial():
    out = _render_gate_rules({"lock_names": True, "inject_forbidden": True}, ["主角不能再死亡"])
    assert "锁定人名" in out and "禁止修改" in out and "主角不能再死亡" in out
    assert "锁定剧情" not in out


def test_iter_output_chapter_ids_folds_virtual():
    chapters = [
        {"id": "1", "is_empty": False},
        {"id": "2", "is_virtual_parent": True, "is_empty": False},
        {"id": "2_0", "is_virtual": True, "original_id": "2", "is_empty": False},
        {"id": "3", "is_empty": True},
    ]
    assert _iter_output_chapter_ids({"chapters": chapters}) == ["1", "2"]


def test_run_qc_detects_issues():
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        rec = out / "02_workspace/reconstructed"
        rec.mkdir(parents=True)
        (rec / "chapter_1.txt").write_text("正常重构内容", encoding="utf-8")
        (rec / "chapter_2.txt").write_text("正文 ===CHAPTER_BREAK=== 残留", encoding="utf-8")
        (rec / "chapter_3.txt").write_text("   ", encoding="utf-8")
        chapters = [{"id": str(i), "is_empty": False} for i in (1, 2, 3)]
        issues = _run_qc(out, {"chapters": chapters})
        assert any("CHAPTER_BREAK" in i for i in issues)
        assert any("为空章" in i for i in issues)


def test_is_hard_break():
    assert _is_hard_break("第一章 风起\n\n正文……")
    assert _is_hard_break("# 标题\n正文")
    assert _is_hard_break("Chapter 12\n正文")
    assert _is_hard_break("---\n正文")
    assert not _is_hard_break("他推开门走了进去。\n\n大厅里空无一人。")