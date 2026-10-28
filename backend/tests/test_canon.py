"""#5 人物/伏笔档案最小闭环：持久化、增量更新、按预算注入与 Markdown 渲染回归。"""

from pathlib import Path

import pytest

from core import canon


def _out(tmp_path: Path) -> Path:
    return tmp_path / "book"


# ---------- 持久化 ----------

def test_load_missing_file_returns_empty_skeleton(tmp_path):
    loaded = canon.load_canon(_out(tmp_path))
    assert loaded == {"version": 1, "persons": [], "foreshadows": []}


def test_save_and_load_roundtrip(tmp_path):
    c = canon.empty_canon()
    canon.upsert_person(c, name="苏晚", aliases=("晚晚",), identity="学生",
                        status="未明", chapter="1")
    path = canon.save_canon(_out(tmp_path), c)
    assert path.exists()
    loaded = canon.load_canon(_out(tmp_path))
    assert loaded["persons"][0]["name"] == "苏晚"
    assert loaded["persons"][0]["chapters"] == ["1"]
    assert (_out(tmp_path) / "02_workspace/canon/canon.md").exists()


def test_corrupted_canon_resets_to_empty(tmp_path):
    d = _out(tmp_path) / "02_workspace/canon"
    d.mkdir(parents=True)
    (d / "canon.json").write_text("{bad json", encoding="utf-8")
    assert canon.load_canon(_out(tmp_path))["persons"] == []


# ---------- 人物登记 ----------

def test_upsert_person_merges_aliases_and_appends_chapter_once():
    c = canon.empty_canon()
    p1 = canon.upsert_person(c, name="林深", aliases=("阿深",), chapter="1")
    canon.upsert_person(c, name="林深", aliases=("阿深", "林子"), chapter="1")
    canon.upsert_person(c, name="林深", chapter="3", status="主角")
    assert p1["aliases"] == ["阿深", "林子"]
    assert p1["chapters"] == ["1", "3"]
    assert p1["status"] == "主角"
    assert len(c["persons"]) == 1


def test_upsert_person_rejects_empty_name():
    c = canon.empty_canon()
    with pytest.raises(ValueError):
        canon.upsert_person(c, name="  ")


# ---------- 伏笔登记 ----------

def test_foreshadow_reuses_unresolved_entry_and_resolves_on_callback():
    c = canon.empty_canon()
    f = canon.upsert_foreshadow(c, description="玉佩的来历", chapter="1")
    assert f["planted_chapter"] == "1" and f["status"] == "open"
    again = canon.upsert_foreshadow(c, description="玉佩的来历", chapter="4")
    assert again["id"] == f["id"] and len(c["foreshadows"]) == 1
    canon.upsert_foreshadow(c, description="玉佩的来历", status="resolved",
                             resolved_chapter="9")
    assert again["status"] == "resolved"
    assert again["resolved_chapter"] == "9"


# ---------- 批次增量更新 ----------

def test_apply_batch_appends_chapter_only_for_known_person():
    c = canon.empty_canon()
    canon.upsert_person(c, name="苏晚", chapter="1")
    canon.upsert_person(c, name="陈默", chapter="2")
    canon.apply_batch(c, "3", "苏晚推门而入，屋里空无一人。")
    suwan = next(p for p in c["persons"] if p["name"] == "苏晚")
    chenmo = next(p for p in c["persons"] if p["name"] == "陈默")
    assert suwan["chapters"] == ["1", "3"]
    assert chenmo["chapters"] == ["2"]


# ---------- 预算注入 ----------

def test_context_empty_when_canon_empty():
    assert canon.build_canon_context(canon.empty_canon()) == ""


def test_context_prioritizes_keyword_match_then_resident_entries():
    c = canon.empty_canon()
    canon.upsert_person(c, name="苏晚", chapter="1")
    canon.upsert_person(c, name="路人甲", chapter="2")
    canon.upsert_foreshadow(c, description="玉佩的来历", chapter="1")
    text = canon.build_canon_context(c, keywords=("苏晚",))
    # 命中条目排在常驻条目之前
    assert text.index("苏晚") < text.index("路人甲")
    assert "玉佩" in text


def test_context_truncates_to_budget():
    c = canon.empty_canon()
    for i in range(20):
        canon.upsert_person(c, name=f"人物{i}号", chapter=str(i))
    text = canon.build_canon_context(c, budget_chars=120)
    assert len(text) <= 120 + len("【人物档案（仅用于保持称谓与状态一致，不要复述进正文）】")
    assert "人物0号" in text


# ---------- Markdown 渲染 ----------

def test_markdown_sections_and_entry_lines():
    c = canon.empty_canon()
    canon.upsert_person(c, name="苏晚", aliases=("晚晚",), identity="学生", chapter="1")
    canon.upsert_foreshadow(c, description="玉佩", chapter="1")
    md = canon.render_markdown(c)
    assert "## 人物表" in md and "## 伏笔表" in md
    assert "苏晚" in md and "晚晚" in md
    assert "玉佩" in md and "未回收" in md
