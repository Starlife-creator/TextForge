"""#7 提示词版本注册表骨架：条目完整性、fail-fast、落盘与逐阶段漂移比对回归。"""

import pytest
from types import SimpleNamespace

from core.prompt_registry import (
    REGISTRY, STAGES, PROMPT_VERSION, get_prompt_entry, get_prompt_version,
    load_registry, stage_versions,
)


def _req(**kw):
    base = dict(
        model="m", api_url="http://127.0.0.1:1234", context_window=8000,
        input_path="in.txt", input_mode="single_file", input_format="txt",
        refactor_mode="full_rewrite", refactor_gates={}, forbidden_canon=[],
        name_map={}, models={}, diagnose_json=None, fix_list=[],
        output_path="out", novel_name=None, rich_text=False,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_registry_covers_all_stages_with_required_fields():
    assert set(REGISTRY) == set(STAGES)
    for stage in STAGES:
        entry = get_prompt_entry(stage)
        for field in ("version", "updated", "description", "source"):
            assert entry.get(field), f"{stage}.{field} 不能为空"
        assert entry["stage"] == stage
        assert entry["version"] == get_prompt_version(stage)


def test_prompt_version_matches_blueprint_stage():
    assert PROMPT_VERSION == get_prompt_version("blueprint")


def test_unknown_stage_fails_fast_with_keyerror():
    with pytest.raises(KeyError, match="缺少阶段条目"):
        get_prompt_entry("nonexistent_stage")


def test_stage_versions_snapshot_is_complete():
    versions = stage_versions()
    assert set(versions) == set(STAGES)
    assert all(isinstance(v, str) and v for v in versions.values())


def test_load_registry_raises_when_stage_missing(monkeypatch):
    saved = dict(REGISTRY)
    try:
        del REGISTRY["stitch"]
        with pytest.raises(RuntimeError, match="stitch"):
            load_registry()
    finally:
        REGISTRY.clear()
        REGISTRY.update(saved)


def test_prompt_engine_reexports_registry_api():
    import core.prompt_engine as engine
    for name in ("PROMPT_VERSION", "REGISTRY", "STAGES", "get_prompt_entry",
                 "get_prompt_version", "load_registry", "stage_versions"):
        assert hasattr(engine, name)


def test_prompt_version_module_reexports_for_backcompat():
    from core.prompt_version import PROMPT_VERSION as legacy_pv
    assert legacy_pv == PROMPT_VERSION


def test_init_progress_records_per_stage_versions():
    from routes.pipeline_runner import init_progress
    progress = init_progress(_req(), "run-registry")
    assert progress["prompt_version"] == PROMPT_VERSION
    assert progress["prompt_versions"] == stage_versions()
    assert set(progress["prompt_versions"]) == set(STAGES)


def test_config_drift_reports_per_stage_version_change():
    from routes.pipeline_runner import _config_drift
    old = {"prompt_versions": dict(stage_versions())}
    old["prompt_versions"]["refactor"] = "2026-09-01.refactor.0"
    diffs = _config_drift(old, _req())
    assert any("提示词·refactor" in d and "2026-09-01.refactor.0" in d for d in diffs)
    assert not any("提示词·blueprint" in d for d in diffs)


def test_config_drift_legacy_global_compat_for_old_progress():
    from routes.pipeline_runner import _config_drift
    diffs = _config_drift({"prompt_version": "old-version", "api_config": {}}, _req())
    assert any("提示词版本" in d and PROMPT_VERSION in d for d in diffs)


def test_config_drift_skips_progress_without_version_field():
    from routes.pipeline_runner import _config_drift
    assert _config_drift({"api_config": {}}, _req()) == []
