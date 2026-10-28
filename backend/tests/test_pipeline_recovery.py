"""#8 后端关键路径回归：恢复分支、并发波前、异常分类、版本恢复守卫、历史元数据、
模型候选推导、日志增量切片。

说明：本项目未装 pytest-asyncio，异步逻辑用 asyncio.run 在同步用例内驱动；
本文件覆盖的均为纯函数与同步分支。
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import versions
from core.api_client import ContextOverflowError
from core.refactor import (
    ChapterBreakMissingError,
    _effective_concurrency,
    classify_batch_result,
)
from routes import history, state, system
import routes.pipeline_runner as runner


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    """把 runs.json 钉在临时目录，避免测试污染真实历史注册表。"""
    monkeypatch.setenv("TEXTFORGE_DATA_DIR", str(tmp_path))
    yield


def _req(**kw):
    base = dict(
        model="m", api_url="http://127.0.0.1:1234", context_window=8000,
        input_path="in.txt", input_mode="single_file", input_format="txt",
        refactor_mode="full_rewrite", refactor_gates={}, forbidden_canon=[],
        name_map={}, models={}, diagnose_json=None, fix_list=[],
        output_path="out", novel_name=None, rich_text=False, max_concurrency=1,
    )
    base.update(kw)
    return SimpleNamespace(**base)


# ---------- 并发波前 ----------

def test_concurrency_defaults_to_serial_and_bounds_1_to_4():
    assert _effective_concurrency(_req(max_concurrency=1), {}) == 1
    assert _effective_concurrency(_req(max_concurrency=3), {}) == 3
    assert _effective_concurrency(_req(max_concurrency=9), {}) == 4
    assert _effective_concurrency(_req(max_concurrency=0), {}) == 1
    assert _effective_concurrency(_req(max_concurrency=-2), {}) == 1
    # 缺省属性 / 非法值回落到串行
    assert _effective_concurrency(SimpleNamespace(), {}) == 1
    assert _effective_concurrency(_req(max_concurrency="abc"), {}) == 1


def test_accept_gate_forces_serial_even_when_concurrency_enabled():
    gates = {"require_chapter_accept": True}
    assert _effective_concurrency(_req(max_concurrency=4), gates) == 1
    assert _effective_concurrency(_req(max_concurrency=2), gates) == 1


# ---------- 批次异常分类 ----------

def test_classify_batch_result_four_branches():
    assert classify_batch_result(ContextOverflowError(1000, 800, "x")) == "overflow"
    assert classify_batch_result(ChapterBreakMissingError("缺分隔符")) == "contract"
    assert classify_batch_result(RuntimeError("网络抖动")) == "raise"
    assert classify_batch_result((["f1.txt"], [])) == "done"
    assert classify_batch_result(None) == "done"


# ---------- 恢复分支：阶段越过判定 ----------

def test_phase_passed_detects_resumed_past_blueprint_gate():
    # phase2_waiting 已越过 → phase2 不得重新生成蓝图
    assert state.phase_passed({"current_phase": "phase3-1"}, "phase2_waiting")
    assert state.phase_passed({"current_phase": "phase3_accept"}, "phase2_waiting")
    # 停在同一阶段不算越过；更早阶段 / 空进度不算越过
    assert not state.phase_passed({"current_phase": "phase2_waiting"}, "phase2_waiting")
    assert not state.phase_passed({"current_phase": "phase1"}, "phase2_waiting")
    assert not state.phase_passed({"current_phase": None}, "phase2_waiting")


def test_load_or_init_resumes_existing_progress_and_records_drift(tmp_path):
    progress_path = tmp_path / "progress.json"
    existing = {
        "current_phase": "phase3-1", "completed": False,
        "chapters": [], "batches": [], "novel_name": "测试书",
        "input_path": "in.txt", "input_mode": "single_file", "input_format": "txt",
        "output_path": str(tmp_path),
        "api_config": {"model": "old-model", "api_url": "http://a/v1", "context_window": 8000},
    }
    from utils.atomic import atomic_write_json
    atomic_write_json(progress_path, existing)

    loaded = runner._load_or_init_progress(_req(model="new-model"), "run-resume", progress_path)
    assert loaded["resumed"] is True
    assert loaded["run_id"] == "run-resume"
    assert loaded["pipeline_running"] is True
    assert loaded["completed"] is False
    # 模型变化进 config_drift，不静默沿用
    assert any("模型" in d and "old-model" in d and "new-model" in d
               for d in loaded.get("config_drift", []))


def test_load_or_init_fresh_progress_when_no_file(tmp_path):
    progress_path = tmp_path / "progress.json"
    progress = runner._load_or_init_progress(_req(), "run-fresh", progress_path)
    assert progress["resumed"] is False
    assert progress["pipeline_running"] is True
    # #7：各阶段提示词版本随新 run 落盘
    for stage in ("diagnose", "blueprint", "refactor", "stitch", "split", "regen"):
        assert stage in progress["prompt_versions"]


# ---------- 版本恢复：路径守卫与回滚 ----------

def _build_versioned_output(tmp_path: Path) -> Path:
    out = tmp_path / "book"
    final = out / "03_final"
    final.mkdir(parents=True)
    (final / "chapter1.txt").write_text("当前版 v1", encoding="utf-8")
    snap = out / "_versions" / "20260101_000000_old"
    snap.mkdir(parents=True)
    (snap / "chapter1.txt").write_text("待恢复 v2", encoding="utf-8")
    return out


def test_restore_rejects_path_traversal_and_invalid_version_id(tmp_path):
    out = _build_versioned_output(tmp_path)
    for bad in ("", "/", "../x", "a/b", "a\\b", ".."):
        with pytest.raises(ValueError, match="非法的版本号"):
            versions.restore(out, bad)
    with pytest.raises(FileNotFoundError):
        versions.restore(out, "missing-version")


def test_restore_copies_snapshot_back_and_snapshots_current_first(tmp_path):
    out = _build_versioned_output(tmp_path)
    result = versions.restore(out, "20260101_000000_old")
    assert (out / "03_final" / "chapter1.txt").read_text(encoding="utf-8") == "待恢复 v2"
    assert result["restored"] == "20260101_000000_old"
    # 恢复前先快照当前版，恢复动作本身可再被回退
    assert result["snapshot_before_restore"]
    assert any("恢复前" in name for name in
               (p.name for p in (out / "_versions").iterdir() if p.is_dir()))


# ---------- 历史元数据 ----------

def test_meta_from_progress_counts_chapters_batches_and_last_error():
    progress = {
        "novel_name": "书", "input_path": "in.txt", "input_mode": "single_file",
        "current_phase": "phase3-1",
        "chapters": [{"id": "1"}, {"id": "2"}, {"id": "3"}],
        "batches": [{"status": "phase3_done"}, {"status": "phase3_done"}, {"status": "pending"}],
        "error_logs": [{"message": "旧错"}, {"message": "最后一条错"}],
        "config_drift": ["api_key=sk-abc-123 模型差异"],
    }
    meta = history._meta_from_progress(progress)
    assert meta["total_chapters"] == 3
    assert meta["batches_done"] == 2
    assert meta["batches_total"] == 3
    assert meta["last_error"] == "最后一条错"
    assert meta["finished"] is False
    assert "sk-abc-123" not in json.dumps(meta["config_drift"], ensure_ascii=False)


def test_record_progress_swallows_malformed_progress(tmp_path):
    # 缺字段/缺文件也不允许抛到流水线主循环
    history.record_progress(tmp_path / "progress.json", {})
    history.record_progress(tmp_path / "progress.json", {"output_path": str(tmp_path)})


# ---------- 模型候选端点推导 ----------

def test_models_candidates_derives_chat_and_base_variants():
    assert system._models_candidates("http://host/v1/chat/completions") == ["http://host/v1/models"]
    assert system._models_candidates("http://host/v1/completions") == ["http://host/v1/models"]
    assert system._models_candidates("http://host/v1") == ["http://host/v1/models"]
    assert system._models_candidates("http://host/v1/models") == ["http://host/v1/models"]
    assert system._models_candidates("") == []
    # 候选去重
    cands = system._models_candidates("http://host/v1/models/")
    assert len(cands) == len(set(cands))


# ---------- 日志增量切片 ----------

def test_read_log_slice_offset_windows_and_clamps_past_end(tmp_path):
    log = tmp_path / "backend.log"
    log.write_text("\n".join(f"line {i}" for i in range(5)) + "\n", encoding="utf-8")

    first = system._read_log_slice(log, 2, 0)
    assert first["total"] == 5
    assert first["lines"] == ["line 0", "line 1"]
    assert first["offset"] == 2

    second = system._read_log_slice(log, 2, 2)
    assert second["lines"] == ["line 2", "line 3"]
    assert second["offset"] == 4

    at_end = system._read_log_slice(log, 2, 5)
    assert at_end["lines"] == [] and at_end["offset"] == 5

    beyond = system._read_log_slice(log, 2, 99)
    assert beyond["lines"] == [] and beyond["offset"] == 5
