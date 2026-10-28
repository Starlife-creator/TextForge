"""#17 progress.json 版本迁移单测：旧文件回填默认、幂等、留 .bak、损坏报错。"""

import json
from pathlib import Path

import pytest

from utils.progress_io import (
    PROGRESS_SCHEMA_VERSION,
    ProgressUnreadable,
    migrate_progress,
    migrate_progress_file,
)


def test_migrate_backfills_missing_defaults_and_keeps_existing():
    old = {
        "novel_name": "旧书",
        "current_phase": "phase3",
        "chapters": [{"id": "1"}],
        # 其余字段全部缺失
    }
    data, applied = migrate_progress(old)
    assert applied == [f"backfill_defaults_v{PROGRESS_SCHEMA_VERSION}"]
    assert data["progress_schema_version"] == PROGRESS_SCHEMA_VERSION
    # 已有值不被覆盖
    assert data["novel_name"] == "旧书"
    assert data["current_phase"] == "phase3"
    assert data["chapters"] == [{"id": "1"}]
    # 回填的默认结构可用
    assert data["batches"] == []
    assert data["batch_qc"] == {}
    assert data["error_logs"] == []
    assert data["usage"] == {"prompt_tokens": 0, "completion_tokens": 0, "estimated": False}
    assert data["auto_rewrite_count"] == 0
    assert data["completed"] is False


def test_migrate_is_idempotent_on_current_schema():
    data = {"progress_schema_version": PROGRESS_SCHEMA_VERSION, "novel_name": "x"}
    migrated, applied = migrate_progress(data)
    assert applied == []
    assert migrated is data


def test_migrate_progress_file_writes_back_and_keeps_bak(tmp_path: Path):
    p = tmp_path / "progress.json"
    p.write_text(json.dumps({"novel_name": "老书", "current_phase": "phase1"}),
                 encoding="utf-8")

    data, applied = migrate_progress_file(p)
    assert applied
    assert data["progress_schema_version"] == PROGRESS_SCHEMA_VERSION

    # .bak 保留迁移前原文
    bak = p.with_suffix(".json.bak")
    assert bak.exists()
    assert json.loads(bak.read_text(encoding="utf-8")) == {"novel_name": "老书", "current_phase": "phase1"}

    # 第二次调用幂等：不再写回、不再产生新备份
    data2, applied2 = migrate_progress_file(p)
    assert applied2 == []
    assert data2["novel_name"] == "老书"


def test_migrate_progress_file_corrupted_raises(tmp_path: Path):
    p = tmp_path / "progress.json"
    p.write_text("{broken", encoding="utf-8")
    with pytest.raises(ProgressUnreadable):
        migrate_progress_file(p)


def test_migrate_rejects_non_object_top_level(tmp_path: Path):
    p = tmp_path / "progress.json"
    p.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ProgressUnreadable):
        migrate_progress_file(p)


def test_migrate_preserves_existing_container_values():
    old = {
        "chapters": [{"id": "9"}],
        "batches": [{"batch_id": 1, "status": "phase3_done"}],
        "usage": {"prompt_tokens": 42},
    }
    data, _ = migrate_progress(old)
    assert data["chapters"] == [{"id": "9"}]
    assert data["batches"] == [{"batch_id": 1, "status": "phase3_done"}]
    assert data["usage"] == {"prompt_tokens": 42}


def test_migrate_does_not_share_default_mutable_between_runs() -> None:
    """#2 review：两本书回填默认值后互不影响（chapters/usage/accepted_batches 不串数据）。"""
    from utils.progress_io import migrate_progress
    a, _ = migrate_progress({"novel_name": "A", "progress_schema_version": 1})
    b, _ = migrate_progress({"novel_name": "B", "progress_schema_version": 1})
    assert a["chapters"] is not b["chapters"]
    assert a["usage"] is not b["usage"]
    assert a["accepted_batches"] is not b["accepted_batches"]
    a["chapters"].append({"id": 1})
    a["usage"]["prompt_tokens"] = 99999
    a["accepted_batches"].append("batch-1")
    assert b["chapters"] == []
    assert b["usage"]["prompt_tokens"] == 0
    assert b["accepted_batches"] == []


def test_migrate_scrubs_historical_plaintext_models() -> None:
    """#6 review：迁移时一次性清洗历史明文 models。"""
    from utils.progress_io import migrate_progress
    data = {"novel_name": "old", "progress_schema_version": 1,
            "models": {"phase1": {"api_key": "sk-leaked-old"}}}
    out, _ = migrate_progress(data)
    assert out["models"]["phase1"]["api_key"] == "***"
