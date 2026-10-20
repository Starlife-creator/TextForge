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
from core.api_client import _process_sse_line
from utils.progress_io import is_completed


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


def test_is_completed_explicit_flag_wins():
    # 失败/停止：finished_at 有值、phase4 有值，但 completed=false，不能算完成
    assert not is_completed({"completed": False, "finished_at": "2026-01-01T00:00:00Z",
                             "current_phase": "phase4"})
    assert is_completed({"completed": True, "finished_at": "2026-01-01T00:00:00Z",
                         "current_phase": "phase4"})


def test_is_completed_legacy_fallback():
    # 旧 progress 没有 completed 字段：沿用 phase4 + finished_at 判定
    assert is_completed({"finished_at": "2026-01-01T00:00:00Z", "current_phase": "phase4"})
    assert not is_completed({"finished_at": "2026-01-01T00:00:00Z", "current_phase": "phase3"})
    assert not is_completed({"current_phase": "phase4"})


def test_sse_data_without_space():
    full, usage = [], {}
    chunk = _process_sse_line('data:{"choices":[{"delta":{"content":"你好"}}]}', full, usage)
    assert chunk == "你好" and "".join(full) == "你好"
    assert _process_sse_line("data:[DONE]", full, usage) is None


def test_sse_data_with_space_and_usage():
    full, usage = [], {}
    line = ('data: {"choices":[{"delta":{"content":"A"}}],'
            '"usage":{"prompt_tokens":3,"completion_tokens":2}}')
    assert _process_sse_line(line, full, usage) == "A"
    assert usage == {"prompt_tokens": 3, "completion_tokens": 2}


def test_event_stream_unsubscribes_on_early_close():
    """客户端在 run_id/replay 的 yield 处断开时，订阅也必须被移除。

    这正是原实现的泄漏窗口：注册在 try/finally 之外，GeneratorExit 不会触发
    unsubscribe，队列会永久留在 _subscribers 里。
    """
    import asyncio
    from routes import events, state

    old_run = state.current_run_id
    rid = "test-unsubscribe"
    state.current_run_id = rid

    async def scenario():
        agen = events._event_stream(rid)
        first = await anext(agen)
        assert "event: run_id" in first
        assert rid in events._subscribers
        await agen.aclose()
        assert rid not in events._subscribers

    try:
        asyncio.run(scenario())
    finally:
        state.current_run_id = old_run
        events._subscribers.pop(rid, None)

def _pipeline_req(**kw):
    from types import SimpleNamespace
    base = dict(
        model="m", api_url="http://127.0.0.1:1234", context_window=8000,
        input_path="in.txt", input_mode="single_file", input_format="txt",
        refactor_mode="full_rewrite", refactor_gates={}, forbidden_canon=[],
        name_map={}, models={}, diagnose_json=None, fix_list=[],
        output_path="out", novel_name=None, rich_text=False,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_config_drift_reports_prompt_version_change():
    from core.prompt_version import PROMPT_VERSION
    from routes.pipeline_runner import _config_drift
    diffs = _config_drift({"prompt_version": "old-version", "api_config": {}}, _pipeline_req())
    assert any("提示词版本" in d and PROMPT_VERSION in d for d in diffs)


def test_config_drift_skips_old_progress_without_prompt_version():
    from routes.pipeline_runner import _config_drift
    assert _config_drift({"api_config": {}}, _pipeline_req()) == []


def test_init_progress_records_prompt_version():
    from core.prompt_version import PROMPT_VERSION
    from routes.pipeline_runner import init_progress
    progress = init_progress(_pipeline_req(), "run-1")
    assert progress["prompt_version"] == PROMPT_VERSION
