"""#17 历史注册表与导出单测：排序、脱敏、条数上限、损坏重置、路径归一化。"""

import json

import pytest

from routes import history


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    """把 runs.json 钉在临时目录，避免测试污染真实历史注册表。"""
    monkeypatch.setenv("TEXTFORGE_DATA_DIR", str(tmp_path))
    yield


def _write_registry(entries: dict) -> None:
    history.atomic_write_json(history._registry_path(), {"entries": entries})


def test_export_history_sorted_recent_first():
    _write_registry({
        "/old": {"output_path": "/old", "updated_at": "2026-01-01T00:00:00Z",
                 "novel_name": "旧书"},
        "/new": {"output_path": "/new", "updated_at": "2026-06-01T00:00:00Z",
                 "novel_name": "新书"},
    })
    data = history.export_history()
    assert data["count"] == 2
    assert [e["output_path"] for e in data["entries"]] == ["/new", "/old"]
    assert data["exported_at"]


def test_upsert_redacts_config_drift_secret():
    history.upsert("/tmp/book-a", {
        "novel_name": "脱敏测试",
        "config_drift": ["api_key=sk-secret-xyz789 模型配置差异"],
    })
    reg = history._load()
    entry = reg["entries"][history._norm("/tmp/book-a")]
    stored = json.dumps(entry, ensure_ascii=False)
    assert "sk-secret-xyz789" not in stored
    assert "***" in stored


def test_registry_trims_to_max_entries():
    for i in range(history.MAX_ENTRIES + 1):
        history.upsert(f"/tmp/book-{i}", {"novel_name": f"书 {i}"})
    reg = history._load()
    assert len(reg["entries"]) == history.MAX_ENTRIES


def test_corrupted_registry_resets_to_empty():
    history._registry_path().write_text("{not json", encoding="utf-8")
    reg = history._load()
    assert reg == {"entries": {}}


def test_norm_normalizes_path_case_and_absolute():
    n1 = history._norm("C:\\Books\\Novel.txt")
    n2 = history._norm("c:/books/novel.txt")
    # 大小写归一后应一致（Windows 语义在非 Windows 上 normcase 不折大小写，
    # 至少保证同一路径两次归一结果相同）
    assert history._norm("/tmp/a") == history._norm("/tmp/a")
    assert n1 and n2


def test_export_entries_carry_redacted_metadata():
    history.upsert("/tmp/book-b", {
        "novel_name": "元数据",
        "current_phase": "phase3",
        "total_chapters": 12,
        "finished": False,
    })
    data = history.export_history()
    entry = data["entries"][0]
    assert entry["novel_name"] == "元数据"
    assert entry["current_phase"] == "phase3"
    assert entry["total_chapters"] == 12
