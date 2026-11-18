"""逐章流式审阅（「边跑边审」）细粒度 API。

提供三个 O(1) 行级接口：
1. `GET /api/chapters`：基于 `.tfproj.db` 索引毫秒级列出章节状态、字数、重写次数与质检摘要；
2. `GET /api/chapters/{chapter_num}/diff`：读取单章原文与重构稿，返回分句/分段结构化 Diff；
3. `POST /api/chapters/{chapter_num}/review`：支持标记 `approved` / `pending`，或直接保存人工内联精修稿、
   重跑单章结构性质检（`quality.py`），并通过 `merge_progress_write` + `ProjectDB` 安全合并——
   即使 Phase 3 正在后台并发处理后续批次也不会互相覆盖。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query

from core.project_db import VALID_REVIEW_STATUSES, get_project_db
from core.quality import assess_batch_texts
from core.refactor import _refresh_qc_after_regen
from core.text_diff import compute_chapter_diff
from routes import state
from utils.atomic import atomic_write_text
from utils.errors import raise_internal_error
from utils.progress_io import ProgressUnreadable, merge_progress_write

logger = logging.getLogger("textforge")
router = APIRouter()

_CHAPTER_NUM_RE = re.compile(r"^[0-9]{1,6}$")


def _check_run_id(run_id: str | None) -> None:
    if run_id and state.pipeline_running and run_id != state.current_run_id:
        raise HTTPException(409, "run_id_mismatch")


def _validate_chapter_num(raw: str) -> int:
    token = (raw or "").strip()
    if not _CHAPTER_NUM_RE.match(token):
        raise HTTPException(400, f"非法的章节编号：{raw}")
    num = int(token)
    if num <= 0:
        raise HTTPException(400, f"章节编号必须为正整数：{raw}")
    return num


def _resolve_output_dir(output_path: str | None) -> Path:
    raw = (output_path or state.current_output_path or "").strip()
    if not raw:
        raise HTTPException(422, "缺少 output_path（且当前无运行中的任务）")
    if "\x00" in raw:
        raise HTTPException(400, "非法的输出目录路径")
    try:
        base = Path(raw).expanduser().resolve()
    except OSError as e:
        raise HTTPException(400, "输出目录路径无法解析") from e
    if not base.is_dir():
        raise HTTPException(404, "输出目录不存在")
    if not (
        (base / "progress.json").is_file()
        or (base / ".tfproj.db").is_file()
        or (base / "02_workspace").is_dir()
    ):
        raise HTTPException(404, "该目录尚无 TextForge 章节进度数据")
    return base


def _safe_inside_base(base: Path, rel_path: str) -> Path:
    """校验相对路径必须严格落在 `base` 内部且不含符号链接逃逸（同 status.py #9 安全口径）。"""
    clean = (rel_path or "").strip().replace("\\", "/")
    if not clean or clean.startswith("/") or ".." in clean.split("/"):
        raise HTTPException(400, "非法的章节文件路径")
    candidate = base / clean
    if candidate.is_symlink():
        raise HTTPException(400, "拒绝访问符号链接章节文件")
    try:
        resolved = candidate.resolve()
        norm_base = os.path.normcase(str(base.resolve()))
        norm_target = os.path.normcase(str(resolved))
    except OSError as e:
        raise HTTPException(400, "章节文件路径无法解析") from e
    if not norm_target.startswith(norm_base + os.sep):
        raise HTTPException(400, "章节文件路径越界")
    return resolved


def _read_chapter_text_candidates(base: Path, candidates: list[str]) -> str:
    for rel in candidates:
        if not rel:
            continue
        p = _safe_inside_base(base, rel)
        if p.is_file() and not p.is_symlink():
            try:
                return p.read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                continue
    return ""


def _list_chapters_sync(base: Path) -> dict[str, Any]:
    db = get_project_db(base)
    db.ensure_hydrated()
    rows = db.list_chapters_summary(include_empty=False)
    ready_count = sum(1 for r in rows if r["status"] == "ready_for_review")
    approved_count = sum(1 for r in rows if r["review_status"] == "approved")
    edited_count = sum(1 for r in rows if r["review_status"] == "edited")
    flagged_count = sum(1 for r in rows if r["qc_issues"])
    return {
        "output_path": str(base),
        "chapters": [
            {
                "chapter_num": r["chapter_num"],
                "chapter_id": r["chapter_id"],
                "title": r["title"],
                "status": r["status"],
                "review_status": r["review_status"],
                "qc_issues": r["qc_issues"],
                "regen_count": r["regen_count"],
                "char_raw": r["char_raw"],
                "char_refactored": r["char_refactored"],
            }
            for r in rows
        ],
        "total": len(rows),
        "ready_count": ready_count,
        "approved_count": approved_count,
        "edited_count": edited_count,
        "flagged_count": flagged_count,
    }


def _get_chapter_diff_sync(base: Path, cnum: int) -> dict[str, Any]:
    db = get_project_db(base)
    db.ensure_hydrated()
    rec = db.get_chapter_record(cnum)
    if rec is None:
        raise KeyError(f"第 {cnum} 章不存在")
    cid = rec["chapter_id"]
    orig_text = _read_chapter_text_candidates(
        base,
        [
            rec["raw_file"],
            f"02_workspace/split/{cid}.txt",
            f"00_chapters/{cid}.txt",
            f"00_chapters/chapter_{cid}.txt",
        ],
    )
    ref_text = _read_chapter_text_candidates(
        base,
        [
            rec["refactored_file"],
            f"02_workspace/reconstructed/chapter_{cid}.txt",
            f"02_refactored/chapter_{cid}.txt",
            f"02_refactored/{cid}.txt",
        ],
    )
    diff = compute_chapter_diff(orig_text, ref_text)
    return {
        "chapter_num": rec["chapter_num"],
        "chapter_id": cid,
        "title": rec["title"],
        "status": rec["status"],
        "original_text": orig_text,
        "refactored_text": ref_text,
        "diff_blocks": diff["diff_blocks"],
        "stats": diff["stats"],
        "qc_issues": rec["qc_issues"],
        "regen_count": rec["regen_count"],
        "review_status": rec["review_status"],
    }


def _build_qc_req_shim(progress: dict[str, Any]) -> Any:
    live_req = state.current_req
    if live_req is not None and state.current_output_path:
        try:
            if os.path.normcase(str(Path(state.current_output_path).resolve())) == os.path.normcase(
                str(Path(str(progress.get("output_path") or "")).resolve())
            ):
                return live_req
        except OSError:
            pass
    return SimpleNamespace(
        forbidden_canon=progress.get("forbidden_canon") or [],
        name_map=progress.get("name_map") or {},
        refactor_mode=progress.get("refactor_mode") or "full_rewrite",
        refactor_gates=progress.get("refactor_gates") or {},
    )


def _apply_review_sync(
    base: Path,
    cnum: int,
    review_status: str,
    edited_text: str | None,
) -> dict[str, Any]:
    db = get_project_db(base)
    db.ensure_hydrated()
    rec = db.get_chapter_record(cnum)
    if rec is None:
        raise KeyError(f"第 {cnum} 章不存在")

    cid = rec["chapter_id"]
    pf = base / "progress.json"
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    if edited_text is not None:
        cleaned = edited_text.replace("\r\n", "\n").replace("\r", "\n")
        if not cleaned.strip():
            raise ValueError("精修正文不能为空")

        rel_ref = rec["refactored_file"] or f"02_workspace/reconstructed/chapter_{cid}.txt"
        ref_target = _safe_inside_base(base, rel_ref)
        ref_target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(ref_target, cleaned)

        fresh_issues: list[dict[str, Any]] = []
        if pf.is_file():
            committed: dict[str, Any] = {}

            def _mutate_edit(disk: dict[str, Any]) -> None:
                shim = _build_qc_req_shim(disk)
                qc_res = _refresh_qc_after_regen(shim, base, disk, cid, cleaned)
                committed["issues"] = qc_res
                disk.setdefault("chapter_qc_overrides", {})[cid] = qc_res
                disk.setdefault("chapter_reviews", {})[cid] = {
                    "status": review_status,
                    "updated_at": now,
                }
                cfiles = disk.setdefault("completed_files", [])
                if isinstance(cfiles, list) and rel_ref not in cfiles:
                    cfiles.append(rel_ref)

            merge_progress_write(pf, _mutate_edit)
            fresh_issues = committed.get("issues") or []
        else:
            # 无 progress.json（极简纯 DB 目录）仍执行单章质检
            fresh_issues = assess_batch_texts(
                chapters=[
                    {
                        "id": cid,
                        "title": rec["title"],
                        "split_file": rec["raw_file"],
                        "is_empty": rec["is_empty"],
                    }
                ],
                chapter_ids=[cid],
                texts={cid: cleaned},
                output_path=base,
            )

        char_ref = len(re.sub(r"\s+", "", cleaned))
        updated_rec = db.update_chapter_review(
            cnum,
            review_status=review_status,
            qc_issues=fresh_issues,
            char_refactored=len(cleaned.strip()),
            refactored_file=rel_ref,
            status="ready_for_review",
        )
        orig_text = _read_chapter_text_candidates(
            base, [rec["raw_file"], f"02_workspace/split/{cid}.txt"]
        )
        diff = compute_chapter_diff(orig_text, cleaned)
        return {
            "status": "updated",
            "chapter_num": updated_rec["chapter_num"],
            "chapter_id": cid,
            "review_status": updated_rec["review_status"],
            "qc_issues": updated_rec["qc_issues"],
            "char_raw": updated_rec["char_raw"],
            "char_refactored": updated_rec["char_refactored"],
            "non_ws_chars": char_ref,
            "diff_stats": diff["stats"],
        }

    # 仅变更审阅状态（approved / pending / edited）
    if pf.is_file():

        def _mutate_status(disk: dict[str, Any]) -> None:
            disk.setdefault("chapter_reviews", {})[cid] = {
                "status": review_status,
                "updated_at": now,
            }

        merge_progress_write(pf, _mutate_status)

    updated_rec = db.update_chapter_review(cnum, review_status=review_status)
    return {
        "status": "updated",
        "chapter_num": updated_rec["chapter_num"],
        "chapter_id": cid,
        "review_status": updated_rec["review_status"],
        "qc_issues": updated_rec["qc_issues"],
        "char_raw": updated_rec["char_raw"],
        "char_refactored": updated_rec["char_refactored"],
    }


@router.get("/api/chapters")
async def list_chapters(output_path: str | None = Query(None)) -> dict[str, Any]:
    """O(1) 索引返回当前/指定输出目录的章节审阅与质检概览。"""
    base = await asyncio.to_thread(_resolve_output_dir, output_path)
    try:
        return await asyncio.to_thread(_list_chapters_sync, base)
    except ProgressUnreadable as e:
        raise HTTPException(409, str(e)) from e
    except HTTPException:
        raise
    except Exception as e:
        raise raise_internal_error(e, "读取章节列表失败，详情请查看后端日志", "list_chapters")


@router.get("/api/chapters/{chapter_num}/diff")
async def chapter_diff(
    chapter_num: str,
    output_path: str | None = Query(None),
) -> dict[str, Any]:
    """读取单章原文与重构稿并计算句子/段落级结构化 Diff。"""
    cnum = _validate_chapter_num(chapter_num)
    base = await asyncio.to_thread(_resolve_output_dir, output_path)
    try:
        return await asyncio.to_thread(_get_chapter_diff_sync, base, cnum)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except ProgressUnreadable as e:
        raise HTTPException(409, str(e)) from e
    except HTTPException:
        raise
    except Exception as e:
        raise raise_internal_error(e, "计算章节对照失败，详情请查看后端日志", "chapter_diff")


@router.post("/api/chapters/{chapter_num}/review")
async def review_chapter(
    chapter_num: str,
    payload: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    """更新单章审阅状态（approved/pending）或保存内联人工精修正文并重跑单章质检。"""
    cnum = _validate_chapter_num(chapter_num)
    _check_run_id(payload.get("run_id"))
    raw_output_path = payload.get("output_path")
    output_arg = str(raw_output_path) if isinstance(raw_output_path, str) else None
    base = await asyncio.to_thread(_resolve_output_dir, output_arg)

    has_edit = "edited_text" in payload and payload["edited_text"] is not None
    edited_text = str(payload["edited_text"]) if has_edit else None

    raw_status = payload.get("review_status")
    if raw_status is None:
        if not has_edit:
            raise HTTPException(422, "必须提供 review_status 或 edited_text")
        review_status = "edited"
    else:
        review_status = str(raw_status).strip()
        if review_status not in VALID_REVIEW_STATUSES:
            raise HTTPException(
                422, f"review_status 仅允许 {sorted(VALID_REVIEW_STATUSES)}"
            )

    try:
        async with state.progress_lock:
            return await asyncio.to_thread(
                _apply_review_sync, base, cnum, review_status, edited_text
            )
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    except PermissionError as e:
        raise HTTPException(503, "文件正被占用，请稍后重试") from e
    except ProgressUnreadable as e:
        raise HTTPException(409, str(e)) from e
    except HTTPException:
        raise
    except Exception as e:
        raise raise_internal_error(e, "保存章节审阅失败，详情请查看后端日志", "review_chapter")
