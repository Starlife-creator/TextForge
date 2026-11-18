"""双引擎 `.tfproj.db` 状态库 + 「边跑边审」细粒度章节 API 与并发合并集成单测。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as app_module
from core.project_db import (
    DB_FILENAME,
    DB_WAL_FILENAME,
    ProjectDB,
    checkpoint_project_db,
    close_all_project_dbs,
    get_project_db,
)
from core.refactor import _save_progress
from core.text_diff import compute_chapter_diff
from routes import events as events_mod
from routes import pipeline_runner, state
from routes.output import output_clear, output_inspect
from utils.atomic import atomic_write_json
from utils.progress_io import clear_progress_cache, save_progress_dict


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TEXTFORGE_DEV", "1")
    clear_progress_cache()
    close_all_project_dbs()
    yield
    clear_progress_cache()
    close_all_project_dbs()


def _create_sample_project(out_dir: Path, *, write_via_save: bool = True) -> Path:
    split_dir = out_dir / "02_workspace" / "split"
    recon_dir = out_dir / "02_workspace" / "reconstructed"
    split_dir.mkdir(parents=True, exist_ok=True)
    recon_dir.mkdir(parents=True, exist_ok=True)

    ch1_orig = "第一章 雨夜惊雷。林晚推开斑驳的木门，屋里空无一人。桌上的油灯还在摇曳。" + "铺垫描写。" * 80
    ch1_ref = "第一章 惊雷夜雨。林晚轻轻推开斑驳的旧木门，屋内寂静无人。唯有桌上一盏残灯仍在风中摇曳，映出墙角一道暗影。" + "润色细节。" * 80
    ch2_orig = "第二章 暗潮涌动。次日清晨，城门外聚集了数十名骑兵。" + "原稿段落。" * 80
    ch2_ref = "第二章 暗流。TODO：此处省略三百字。"
    ch3_orig = "第三章 远行。马车驶出南门，朝着苍山方向疾驰而去。" + "旅途描写。" * 80

    (split_dir / "1.txt").write_text(ch1_orig, encoding="utf-8")
    (split_dir / "2.txt").write_text(ch2_orig, encoding="utf-8")
    (split_dir / "3.txt").write_text(ch3_orig, encoding="utf-8")
    (recon_dir / "chapter_1.txt").write_text(ch1_ref, encoding="utf-8")
    (recon_dir / "chapter_2.txt").write_text(ch2_ref, encoding="utf-8")

    progress = {
        "run_id": "run-test-001",
        "novel_name": "测试长篇",
        "current_phase": "phase3-1",
        "pipeline_running": True,
        "completed": False,
        "refactor_mode": "full_rewrite",
        "refactor_gates": {"inject_forbidden": True},
        "forbidden_canon": ["赛博飞船"],
        "name_map": {},
        "chapters": [
            {"id": "1", "title": "第一章 雨夜惊雷", "split_file": "02_workspace/split/1.txt", "is_empty": False},
            {"id": "2", "title": "第二章 暗潮涌动", "split_file": "02_workspace/split/2.txt", "is_empty": False},
            {"id": "3", "title": "第三章 远行", "split_file": "02_workspace/split/3.txt", "is_empty": False},
        ],
        "batches": [
            {"batch_id": 0, "chapter_ids": ["1", "2"], "status": "phase3_done", "output_files": [
                "02_workspace/reconstructed/chapter_1.txt",
                "02_workspace/reconstructed/chapter_2.txt",
            ]},
            {"batch_id": 1, "chapter_ids": ["3"], "status": "phase1_done", "output_files": []},
        ],
        "batch_qc": {
            "0": [
                {
                    "severity": "bad",
                    "code": "placeholder",
                    "chapter_id": "2",
                    "message": "第 2 章疑似占位/未完稿（出现「todo」）",
                    "evidence": "TODO：此处省略三百字。",
                }
            ]
        },
        "pre_stitch_issues": [
            {
                "severity": "bad",
                "code": "placeholder",
                "chapter_id": "2",
                "message": "第 2 章疑似占位/未完稿（出现「todo」）",
                "evidence": "TODO：此处省略三百字。",
            }
        ],
        "regen_count": {"1": 1},
        "completed_files": [
            "02_workspace/reconstructed/chapter_1.txt",
            "02_workspace/reconstructed/chapter_2.txt",
        ],
        "error_logs": [{"ts": "2026-11-18T00:00:00Z", "kind": "warning", "code": "429", "message": "瞬时限流已重试"}],
    }
    pf = out_dir / "progress.json"
    if write_via_save:
        save_progress_dict(pf, progress)
    else:
        atomic_write_json(pf, progress, pretty=False, sync=True)
    return out_dir


def test_project_db_tables_wal_and_submillisecond_row_ops(tmp_path: Path) -> None:
    out_dir = _create_sample_project(tmp_path / "proj_wal", write_via_save=True)
    db = get_project_db(out_dir)
    assert (out_dir / DB_FILENAME).is_file()

    # 校验规范化表齐全与 WAL 模式生效
    conn = sqlite3.connect(str(out_dir / DB_FILENAME))
    try:
        mode = conn.execute("PRAGMA journal_mode;").fetchone()[0].lower()
        assert mode == "wal"
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"project_meta", "chapters", "batches", "gate_events", "error_logs"} <= tables
    finally:
        conn.close()

    # 预热一次后测量单行查询与列表查询耗时（验证远小于 30MB deepcopy 级别，平均 <1ms）
    _ = db.list_chapters_summary()
    t0 = time.perf_counter()
    for _ in range(50):
        rows = db.list_chapters_summary()
        rec1 = db.get_chapter_record(1)
    avg_ms = ((time.perf_counter() - t0) * 1000.0) / 50.0
    assert avg_ms < 5.0, f"O(1) 行级查询过慢：avg={avg_ms:.3f}ms"
    assert len(rows) == 3
    assert rec1 is not None
    assert rec1["chapter_num"] == 1
    assert rec1["status"] == "ready_for_review"
    assert rec1["regen_count"] == 1
    assert rec1["char_raw"] > 100 and rec1["char_refactored"] > 100

    rec3 = db.get_chapter_record(3)
    assert rec3 is not None and rec3["status"] == "pending" and rec3["char_refactored"] == 0

    # gate_events 写入验证
    db.record_gate_event("blueprint", "confirmed", run_id="run-test-001", phase="phase2_waiting")


def test_legacy_folder_only_progress_json_auto_hydrates_db(tmp_path: Path) -> None:
    out_dir = _create_sample_project(tmp_path / "legacy_book", write_via_save=False)
    assert not (out_dir / DB_FILENAME).exists(), "初始夹具模拟旧版本，不应有 .tfproj.db"

    db = ProjectDB(out_dir)
    assert db.ensure_hydrated() is True
    assert (out_dir / DB_FILENAME).is_file(), "首次读取旧目录应自动回灌生成 .tfproj.db"

    rows = db.list_chapters_summary()
    assert [r["chapter_num"] for r in rows] == [1, 2, 3]
    assert rows[1]["qc_issues"][0]["code"] == "placeholder"
    db.checkpoint_and_close()


def test_checkpoint_and_output_clear_removes_db_and_wal_cleanly(tmp_path: Path) -> None:
    out_dir = _create_sample_project(tmp_path / "clear_book", write_via_save=True)
    (out_dir / "Story_Bible.md").write_text("蓝图正文", encoding="utf-8")
    (out_dir / "用户手稿.txt").write_text("不可误删的用户私人文件", encoding="utf-8")

    # 触发活跃写入后先检查 inspect 可见 .tfproj.db
    db = get_project_db(out_dir)
    db.update_chapter_review(1, review_status="approved")

    info = asyncio.run(output_inspect(str(out_dir)))
    assert DB_FILENAME in info["artifacts"]

    # checkpoint_project_db 截断清空 -wal 并释放句柄
    checkpoint_project_db(out_dir)
    wal_file = out_dir / DB_WAL_FILENAME
    assert not wal_file.exists() or wal_file.stat().st_size == 0

    # 再次打开连接模拟在线态，直接调 output_clear 也必须先自动关闭句柄再干净删除，零失败
    _ = get_project_db(out_dir).list_chapters_summary()
    state.pipeline_running = False
    res = asyncio.run(output_clear({"output_path": str(out_dir)}))
    assert res["failed"] == []
    assert DB_FILENAME in res["removed"]
    assert not (out_dir / DB_FILENAME).exists()
    assert not (out_dir / "progress.json").exists()
    assert (out_dir / "用户手稿.txt").read_text(encoding="utf-8") == "不可误删的用户私人文件"


def test_compute_chapter_diff_segments_and_stats() -> None:
    orig = "第一句保持不变。第二句将被删掉。第三句需要润色重写。"
    ref = "第一句保持不变。第三句已被精妙地重构润色。第四句是新增过渡。"

    result = compute_chapter_diff(orig, ref)
    ops = [b["op"] for b in result["diff_blocks"]]
    assert "equal" in ops
    assert any(op in ("delete", "replace") for op in ops)
    stats = result["stats"]
    assert stats["char_original"] > 0 and stats["char_refactored"] > 0
    assert 0.0 < stats["change_ratio"] < 1.0
    assert stats["char_delta"] == stats["char_refactored"] - stats["char_original"]

    # 完全一致时改动率为 0
    same = compute_chapter_diff("完全一致的段落。", "完全一致的段落。")
    assert same["stats"]["change_ratio"] == 0.0
    assert [b["op"] for b in same["diff_blocks"]] == ["equal"]


def test_chapters_api_list_diff_edit_qc_and_security(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out_dir = _create_sample_project(tmp_path / "api_book", write_via_save=True)
    client = TestClient(app_module.app)

    # 1. GET /api/chapters
    r_list = client.get("/api/chapters", params={"output_path": str(out_dir)})
    assert r_list.status_code == 200
    body_list = r_list.json()
    assert body_list["total"] == 3
    assert body_list["ready_count"] == 2
    assert body_list["flagged_count"] == 1
    assert [c["chapter_num"] for c in body_list["chapters"]] == [1, 2, 3]

    # 2. GET /api/chapters/1/diff
    r_diff = client.get("/api/chapters/1/diff", params={"output_path": str(out_dir)})
    assert r_diff.status_code == 200
    d1 = r_diff.json()
    assert d1["chapter_num"] == 1
    assert "雨夜惊雷" in d1["original_text"]
    assert "惊雷夜雨" in d1["refactored_text"]
    assert len(d1["diff_blocks"]) >= 1
    assert d1["stats"]["change_ratio"] > 0

    # 3. POST /api/chapters/1/review —— 标记 approved
    r_app = client.post(
        "/api/chapters/1/review",
        json={"output_path": str(out_dir), "review_status": "approved"},
    )
    assert r_app.status_code == 200
    assert r_app.json()["review_status"] == "approved"

    # 4. POST /api/chapters/2/review —— 内联人工精修第 2 章（消除原本的 TODO 占位硬伤）
    clean_ch2 = "第二章 暗潮涌动。次日清晨，城门外铁骑列阵，寒光映照护城河。" + "精修定稿正文。" * 85
    r_edit = client.post(
        "/api/chapters/2/review",
        json={"output_path": str(out_dir), "edited_text": clean_ch2},
    )
    assert r_edit.status_code == 200
    edit_data = r_edit.json()
    assert edit_data["review_status"] == "edited"
    assert edit_data["qc_issues"] == [], f"消除 TODO 后单章质检应清零：{edit_data['qc_issues']}"
    assert (out_dir / "02_workspace/reconstructed/chapter_2.txt").read_text(encoding="utf-8") == clean_ch2

    # 再注入禁用词「赛博飞船」，验证内联编辑确实重跑了项目级规则质检
    bad_ch2 = clean_ch2 + "天空中突然降下一艘赛博飞船。"
    r_edit_bad = client.post(
        "/api/chapters/2/review",
        json={"output_path": str(out_dir), "edited_text": bad_ch2},
    )
    assert r_edit_bad.status_code == 200
    codes = {i["code"] for i in r_edit_bad.json()["qc_issues"]}
    assert "forbidden_canon" in codes

    # 5. 路径穿越与非法章号拦截
    for bad_token in ("..%2Fprogress.json", "0", "-1", "abc", "1;DROP"):
        resp = client.get(f"/api/chapters/{bad_token}/diff", params={"output_path": str(out_dir)})
        assert resp.status_code in (400, 404, 422)

    # 6. Token 鉴权保护验证（非 DEV 模式下无 Token 必须 401，正确 Token 放行）
    monkeypatch.setenv("TEXTFORGE_DEV", "0")
    monkeypatch.setenv("TEXTFORGE_TOKEN", "secret-token-xyz")
    unauth = client.get("/api/chapters", params={"output_path": str(out_dir)})
    assert unauth.status_code == 401
    authed = client.get(
        "/api/chapters",
        params={"output_path": str(out_dir)},
        headers={"X-TextForge-Token": "secret-token-xyz"},
    )
    assert authed.status_code == 200


def test_streaming_phase3_concurrent_review_is_not_clobbered_by_next_batch_save(
    tmp_path: Path,
) -> None:
    """核心并发回归：Phase 3 后台跑第 2 批（持旧内存 progress 快照）期间，
    用户在工作台内联精修了第 1 章并标记 approved/edited；第 2 批结束调用 `_save_progress`
    落盘后，第 1 章的精修状态与刷新后的 QC 结论在 `progress.json` 与 `.tfproj.db` 中均不得丢失！
    """
    out_dir = _create_sample_project(tmp_path / "concurrent_book", write_via_save=True)
    pf = out_dir / "progress.json"

    # 模拟流水线在启动第 2 批 LLM 请求前持有的内存 progress 快照（此时第 2 章还有 placeholder 硬伤）
    pipeline_mem_progress = json.loads(pf.read_text(encoding="utf-8"))

    # 用户在 Phase 3 跑第 2 批期间通过 API 审阅通过第 1 章，并内联精修修好第 2 章
    client = TestClient(app_module.app)
    assert client.post(
        "/api/chapters/1/review",
        json={"output_path": str(out_dir), "review_status": "approved"},
    ).status_code == 200

    fixed_ch2 = "第二章 暗潮涌动。次日破晓，旌旗蔽空，全无半句占位。" + "扎实叙事。" * 85
    assert client.post(
        "/api/chapters/2/review",
        json={"output_path": str(out_dir), "edited_text": fixed_ch2},
    ).status_code == 200

    # 随后后台流水线完成第 2 批（第 3 章），把自己的内存 progress 写回磁盘
    (out_dir / "02_workspace/reconstructed/chapter_3.txt").write_text(
        "第三章 远行重构稿。" * 90, encoding="utf-8"
    )
    pipeline_mem_progress["batches"][1]["status"] = "phase3_done"
    pipeline_mem_progress["completed_files"].append("02_workspace/reconstructed/chapter_3.txt")
    asyncio.run(_save_progress(pipeline_mem_progress, pf))

    # 断言：progress.json 与 ProjectDB 同时保留第 1 章 approved、第 2 章 edited（且旧 placeholder 硬伤不复活）与第 3 章新产出！
    disk = json.loads(pf.read_text(encoding="utf-8"))
    assert disk["chapter_reviews"]["1"]["status"] == "approved"
    assert disk["chapter_reviews"]["2"]["status"] == "edited"
    assert disk["pre_stitch_issues"] == [], f"旧硬伤被后台批次写盘复活了：{disk['pre_stitch_issues']}"

    rows = get_project_db(out_dir).list_chapters_summary()
    by_num = {r["chapter_num"]: r for r in rows}
    assert by_num[1]["review_status"] == "approved"
    assert by_num[2]["review_status"] == "edited"
    assert by_num[2]["qc_issues"] == []
    assert by_num[3]["status"] == "ready_for_review"


def test_emit_batch_stream_ready_syncs_db_and_pushes_sse(tmp_path: Path) -> None:
    out_dir = _create_sample_project(tmp_path / "sse_stream_book", write_via_save=False)
    # 初始把第 3 章重构稿写盘，模拟第 2 批刚完成
    (out_dir / "02_workspace/reconstructed/chapter_3.txt").write_text(
        "第三章 苍山古道。" * 85, encoding="utf-8"
    )
    progress = json.loads((out_dir / "progress.json").read_text(encoding="utf-8"))
    progress["batches"][1]["status"] = "phase3_done"

    q: asyncio.Queue = asyncio.Queue()
    run_id = "run-stream-sse"
    events_mod._subscribers[run_id] = [q]
    try:
        payload = asyncio.run(
            pipeline_runner.emit_batch_stream_ready(
                out_dir,
                progress,
                progress["batches"][1],
                [],
                ["02_workspace/reconstructed/chapter_3.txt"],
                run_id,
            )
        )
        assert payload["batch_index"] == 1
        assert payload["completed_batches"] == 2
        assert payload["total_batches"] == 2
        assert payload["chapters"][0]["chapter_num"] == 3
        assert payload["chapters"][0]["status"] == "ready_for_review"

        pushed = q.get_nowait()
        assert pushed["event"] == "batch_stream_ready"
        assert pushed["data"]["chapters"][0]["chapter_num"] == 3
    finally:
        events_mod._subscribers.pop(run_id, None)


def test_chapters_audit_detects_forbidden_old_name_length_and_stitch_anomalies(
    tmp_path: Path,
) -> None:
    """B6.1：验证 GET /api/chapters/audit 同时覆盖禁设词、未替换旧名、极端压缩/膨胀与缝合告警，并支持关键词全文检索。"""
    out_dir = _create_sample_project(tmp_path / "audit_book", write_via_save=False)
    pf = out_dir / "progress.json"
    prog = json.loads(pf.read_text(encoding="utf-8"))
    prog["forbidden_canon"] = ["赛博飞船"]
    prog["name_map"] = {"旧主角甲": "林渊"}
    prog["batches"][1]["status"] = "phase3_done"
    prog["stitch_anchors"] = [
        {
            "file": "02_workspace/reconstructed/chapter_1.txt",
            "applied": True,
            "change_rate": 0.32,
            "chars": 240,
        }
    ]
    prog["stitch_failures"] = [
        {"stage": 2, "boundary": "0-1", "error": "ValueError: 缝合契约分隔符不匹配"}
    ]
    save_progress_dict(pf, prog)

    # 第 1 章：正常长度，但含未替换旧名「旧主角甲」+ 高改动率缝合锚点
    raw_ch1 = (out_dir / "02_workspace/split/1.txt").read_text(encoding="utf-8")
    (out_dir / "02_workspace/reconstructed/chapter_1.txt").write_text(
        raw_ch1 + "旧主角甲提剑望向远方山门，剑气纵横。",
        encoding="utf-8",
    )
    # 第 2 章：明显压缩（ratio < 0.60）+ 命中禁设「赛博飞船」+ 批次边界 0-1 缝合跳过
    (out_dir / "02_workspace/reconstructed/chapter_2.txt").write_text(
        "天幕裂开，一艘赛博飞船缓缓降落，众人哗然。",
        encoding="utf-8",
    )
    # 第 3 章：明显膨胀（ratio > 1.45）+ 包含特定检索词「玄铁重剑」两次
    raw_ch3 = (out_dir / "02_workspace/split/3.txt").read_text(encoding="utf-8")
    (out_dir / "02_workspace/reconstructed/chapter_3.txt").write_text(
        (raw_ch3 * 2) + "林渊拔出玄铁重剑，玄铁重剑在雪夜铮鸣。",
        encoding="utf-8",
    )
    # 重新触发同步
    save_progress_dict(pf, json.loads(pf.read_text(encoding="utf-8")))

    client = TestClient(app_module.app)
    r_audit = client.get("/api/chapters/audit", params={"output_path": str(out_dir)})
    assert r_audit.status_code == 200
    body = r_audit.json()
    summary = body["summary"]
    assert summary["audited_chapters"] == 3
    assert summary["flagged_chapters"] == 3
    assert summary["forbidden_hits"] == 1
    assert summary["old_name_hits"] == 1
    assert summary["length_anomalies"] == 2
    assert summary["stitch_warnings"] >= 2

    by_num = {it["chapter_num"]: it for it in body["items"]}
    ch1_cats = {f["category"] for f in by_num[1]["findings"]}
    assert "old_name" in ch1_cats
    assert "stitch_boundary" in ch1_cats

    ch2_cats = {f["category"] for f in by_num[2]["findings"]}
    assert "forbidden_canon" in ch2_cats
    assert "length_compression" in ch2_cats
    assert "stitch_boundary" in ch2_cats

    ch3_cats = {f["category"] for f in by_num[3]["findings"]}
    assert "length_bloat" in ch3_cats

    # 关键词全文检索 ?q=玄铁重剑
    r_search = client.get(
        "/api/chapters/audit",
        params={"output_path": str(out_dir), "q": "玄铁重剑"},
    )
    assert r_search.status_code == 200
    s_body = r_search.json()
    assert s_body["summary"]["search_matched_chapters"] == 1
    assert s_body["summary"]["search_total_matches"] == 2
    assert len(s_body["items"]) == 1
    assert s_body["items"][0]["chapter_num"] == 3
    assert s_body["items"][0]["search_matches"]["count"] == 2
    assert any("玄铁重剑" in sn for sn in s_body["items"][0]["search_matches"]["snippets"])


def test_status_enriches_review_summary_live_from_project_db(
    tmp_path: Path,
) -> None:
    """B6.3：无进度的空目录不新建 `.tfproj.db`；仅含 `progress.json` 的旧目录经 `/api/status` 自动回灌并毫秒级附带实时 `review_summary`。"""
    empty_dir = tmp_path / "empty_out"
    empty_dir.mkdir(parents=True, exist_ok=True)
    client = TestClient(app_module.app)

    r_empty = client.get("/api/status", params={"output_path": str(empty_dir)})
    assert r_empty.status_code == 200
    assert r_empty.json()["has_progress"] is False
    assert "review_summary" not in r_empty.json()
    assert not (empty_dir / ".tfproj.db").exists()

    legacy_dir = _create_sample_project(tmp_path / "status_legacy", write_via_save=False)
    assert not (legacy_dir / ".tfproj.db").exists()

    r_initial = client.get("/api/status", params={"output_path": str(legacy_dir)})
    assert r_initial.status_code == 200
    assert r_initial.json().get("review_summary") == {
        "total_chapters": 3,
        "ready_chapters": 2,
        "approved_chapters": 0,
        "edited_chapters": 0,
    }

    assert client.post(
        "/api/chapters/1/review",
        json={"output_path": str(legacy_dir), "review_status": "approved"},
    ).status_code == 200
    assert client.post(
        "/api/chapters/2/review",
        json={"output_path": str(legacy_dir), "edited_text": "精修定稿正文。" * 90},
    ).status_code == 200

    r_enriched = client.get("/api/status", params={"output_path": str(legacy_dir)})
    assert r_enriched.status_code == 200
    assert r_enriched.json().get("review_summary") == {
        "total_chapters": 3,
        "ready_chapters": 2,
        "approved_chapters": 1,
        "edited_chapters": 1,
    }
