"""零牺牲双引擎项目状态存储（`<output_dir>/.tfproj.db` SQLite WAL 侧索引与原子行级存储）。

设计权衡（为什么不直接废除 `progress.json`）：
1. 纯文本可检视性与应急恢复：用户与排障脚本可直接查看 `progress.json`、`02_workspace/split/`、
   `02_workspace/reconstructed/`、`03_final/`，无需专门工具。
2. 100% 向后兼容：既有未完成目录、历史记录、诊断包以及 250+ 条直接读写 `progress.json` 的测试无缝继续工作。
3. Windows 目录可移植性：SQLite WAL 若留下未 checkpoint 的 `-wal`/`-shm` 或未释放的文件句柄，
   复制目录会丢尾部事务、`output.clear` / `versions.restore` 会触发 `WinError 32`。

因此采用「`progress.json` 便携主快照 + `.tfproj.db` WAL 行级索引」双引擎架构：
- `save_progress_dict` / `merge_progress_write` 同步更新 `progress.json` 与 `.tfproj.db`；
- 仅有 `progress.json` 的旧目录在首次打开时自动回灌（auto-hydrate）生成 `.tfproj.db`；
- `/api/chapters` 与「边跑边审」直接走 `ProjectDB` 单行读写（O(1)，<1ms，零 30 MB deepcopy）；
- 流水线收尾/中止、`versions.restore`、`output.clear` 显式调用 `checkpoint_and_close()`
  （`PRAGMA wal_checkpoint(TRUNCATE)`），彻底消除 `-wal` 残留与 Windows 文件锁。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("textforge")

DB_FILENAME = ".tfproj.db"
DB_WAL_FILENAME = ".tfproj.db-wal"
DB_SHM_FILENAME = ".tfproj.db-shm"
DB_ARTIFACT_NAMES: tuple[str, str, str] = (DB_FILENAME, DB_WAL_FILENAME, DB_SHM_FILENAME)

VALID_REVIEW_STATUSES: frozenset[str] = frozenset({"pending", "approved", "edited"})

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS project_meta (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chapters (
    chapter_num INTEGER PRIMARY KEY,
    chapter_id TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    title TEXT NOT NULL DEFAULT '',
    raw_file TEXT NOT NULL DEFAULT '',
    refactored_file TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    review_status TEXT NOT NULL DEFAULT 'pending',
    qc_json TEXT NOT NULL DEFAULT '[]',
    regen_count INTEGER NOT NULL DEFAULT 0,
    char_raw INTEGER NOT NULL DEFAULT 0,
    char_refactored INTEGER NOT NULL DEFAULT 0,
    is_empty INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chapters_sort ON chapters(sort_order);
CREATE INDEX IF NOT EXISTS idx_chapters_cid ON chapters(chapter_id);
CREATE INDEX IF NOT EXISTS idx_chapters_review ON chapters(review_status);

CREATE TABLE IF NOT EXISTS batches (
    batch_id INTEGER PRIMARY KEY,
    chapter_ids_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'pending',
    output_files_json TEXT NOT NULL DEFAULT '[]',
    qc_json TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gate_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    gate TEXT NOT NULL,
    action TEXT NOT NULL,
    phase TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS error_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    code TEXT NOT NULL,
    message TEXT NOT NULL
);
"""


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _norm_dir_key(path: str | Path) -> str:
    try:
        return os.path.normcase(os.path.abspath(str(path)))
    except OSError:
        return str(path)


def _parse_chapter_num(raw_id: int | str, fallback_order: int = 1) -> int:
    if isinstance(raw_id, int):
        return raw_id
    s = str(raw_id).strip()
    if s.isdigit():
        return int(s)
    head = s.split("_", 1)[0]
    if head.isdigit():
        return int(head)
    return fallback_order


def _file_char_len(path: Path) -> int:
    try:
        if not path.is_file() or path.is_symlink():
            return 0
        return len(path.read_text(encoding="utf-8", errors="replace").strip())
    except OSError:
        return 0


class ProjectDB:
    """单个输出目录对应的 `.tfproj.db` WAL 状态引擎。"""

    def __init__(self, output_dir: str | Path) -> None:
        self.output_dir = Path(output_dir)
        self.db_path = self.output_dir / DB_FILENAME
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._schema_ready = False

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        self.output_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=5.0,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute("PRAGMA foreign_keys=ON;")
        if not self._schema_ready:
            conn.executescript(_SCHEMA_SQL)
            conn.commit()
            self._schema_ready = True
        self._conn = conn
        return conn

    def close(self) -> None:
        """关闭底层 SQLite 连接，不触发新文件创建。"""
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except sqlite3.Error as e:
                    logger.debug(f"[project_db] close 忽略异常: {e}")
                finally:
                    self._conn = None
                    self._schema_ready = False

    def checkpoint_and_close(self) -> None:
        """执行 `PRAGMA wal_checkpoint(TRUNCATE)` 并关闭连接，确保无 `-wal` 残留或句柄占用。"""
        with self._lock:
            if self._conn is None and not self.db_path.is_file():
                return
            try:
                conn = self._get_conn()
                conn.commit()
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                conn.commit()
            except sqlite3.Error as e:
                logger.warning(f"[project_db] wal_checkpoint 警告 ({self.db_path}): {e}")
            finally:
                if self._conn is not None:
                    try:
                        self._conn.close()
                    except sqlite3.Error:
                        pass
                    self._conn = None
                    self._schema_ready = False

    def __enter__(self) -> ProjectDB:
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.checkpoint_and_close()

    def get_meta(self, key: str, default: Any = None) -> Any:
        with self._lock:
            if self._conn is None and not self.db_path.is_file():
                return default
            conn = self._get_conn()
            row = conn.execute(
                "SELECT value_json FROM project_meta WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                return default
            try:
                return json.loads(str(row["value_json"]))
            except json.JSONDecodeError:
                return default

    def set_meta(self, key: str, value: Any) -> None:
        now = _utc_now()
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                """
                INSERT INTO project_meta (key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (key, payload, now),
            )
            conn.commit()

    def ensure_hydrated(
        self,
        progress_loader: Callable[[Path], dict[str, Any]] | None = None,
    ) -> bool:
        """确保 `.tfproj.db` 与磁盘 `progress.json` 同步（旧目录首次打开时自动回灌）。

        若 `.tfproj.db` 已记录相同的 `(mtime_ns, size)` 快照指纹，则直接 O(1) 命中返回，
        绝不重复解析或深拷贝 `progress.json`。
        """
        pf = self.output_dir / "progress.json"
        if not pf.is_file():
            return self.db_path.is_file()
        try:
            st = pf.stat()
            stamp = [st.st_mtime_ns, st.st_size]
        except OSError:
            return self.db_path.is_file()

        with self._lock:
            if self.db_path.is_file():
                recorded = self.get_meta("_progress_stamp")
                if recorded == stamp:
                    return True
            if progress_loader is None:
                from utils.progress_io import read_progress

                progress_loader = read_progress
            data = progress_loader(pf)
            self.sync_from_progress(data, progress_path=pf)
            return True

    def sync_from_progress(
        self,
        progress: dict[str, Any],
        *,
        progress_path: Path | None = None,
    ) -> None:
        """将内存/磁盘 `progress` 字典增量同步到规范化表，保留已有人工审阅状态。"""
        if not isinstance(progress, dict):
            return
        now = _utc_now()
        stamp: list[int] | None = None
        if progress_path is not None and progress_path.is_file():
            try:
                st = progress_path.stat()
                stamp = [st.st_mtime_ns, st.st_size]
            except OSError:
                stamp = None

        with self._lock:
            conn = self._get_conn()
            existing_rows = {
                int(r["chapter_num"]): dict(r)
                for r in conn.execute(
                    "SELECT chapter_num, review_status, char_raw, char_refactored, status, updated_at "
                    "FROM chapters"
                ).fetchall()
            }

            meta_pairs: dict[str, Any] = {
                "run_id": progress.get("run_id"),
                "novel_name": progress.get("novel_name"),
                "current_phase": progress.get("current_phase"),
                "completed": bool(progress.get("completed", False)),
                "pipeline_running": bool(progress.get("pipeline_running", False)),
                "resumed": bool(progress.get("resumed", False)),
                "finished_at": progress.get("finished_at"),
                "usage": progress.get("usage") or {},
            }
            if stamp is not None:
                meta_pairs["_progress_stamp"] = stamp

            conn.executemany(
                """
                INSERT INTO project_meta (key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                [
                    (k, json.dumps(v, ensure_ascii=False, separators=(",", ":")), now)
                    for k, v in meta_pairs.items()
                ],
            )

            # 归集每章质检问题：batch_qc 为基准，chapter_qc_overrides（人工精修/单章重写）优先覆盖
            per_chapter_qc: dict[str, list[dict[str, Any]]] = {}
            batch_qc_map = progress.get("batch_qc")
            if isinstance(batch_qc_map, dict):
                for issues in batch_qc_map.values():
                    if not isinstance(issues, list):
                        continue
                    for item in issues:
                        if isinstance(item, dict) and item.get("chapter_id") is not None:
                            cid_str = str(item["chapter_id"])
                            per_chapter_qc.setdefault(cid_str, []).append(item)
            qc_overrides = progress.get("chapter_qc_overrides")
            if isinstance(qc_overrides, dict):
                for cid_str, override_issues in qc_overrides.items():
                    if isinstance(override_issues, list):
                        per_chapter_qc[str(cid_str)] = [
                            x for x in override_issues if isinstance(x, dict)
                        ]

            regen_map = progress.get("regen_count")
            if not isinstance(regen_map, dict):
                regen_map = {}

            review_map = progress.get("chapter_reviews")
            if not isinstance(review_map, dict):
                review_map = {}

            completed_files = set(str(x) for x in (progress.get("completed_files") or []))
            done_cids: set[str] = set()
            raw_batches = progress.get("batches")
            batch_tuples: list[tuple[int, str, str, str, str, str]] = []
            if isinstance(raw_batches, list):
                for idx, b in enumerate(raw_batches):
                    if not isinstance(b, dict):
                        continue
                    raw_bid = b.get("batch_id", idx)
                    try:
                        bid = int(raw_bid)
                    except (TypeError, ValueError):
                        bid = idx
                    cids = [str(x) for x in (b.get("chapter_ids") or [])]
                    b_status = str(b.get("status") or "pending")
                    if b_status == "phase3_done":
                        done_cids.update(cids)
                    out_files = [str(x) for x in (b.get("output_files") or [])]
                    b_qc = (
                        batch_qc_map.get(str(raw_bid), [])
                        if isinstance(batch_qc_map, dict)
                        else []
                    )
                    batch_tuples.append(
                        (
                            bid,
                            json.dumps(cids, ensure_ascii=False, separators=(",", ":")),
                            b_status,
                            json.dumps(out_files, ensure_ascii=False, separators=(",", ":")),
                            json.dumps(b_qc or [], ensure_ascii=False, separators=(",", ":")),
                            now,
                        )
                    )

            conn.execute("DELETE FROM batches")
            if batch_tuples:
                conn.executemany(
                    """
                    INSERT INTO batches
                        (batch_id, chapter_ids_json, status, output_files_json, qc_json, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    batch_tuples,
                )

            raw_chapters = progress.get("chapters")
            if isinstance(raw_chapters, list):
                # 虚拟子章映射：若父章下的所有虚拟子章均已完成重构，也视为就绪
                virtual_children: dict[str, list[str]] = {}
                for ch in raw_chapters:
                    if isinstance(ch, dict) and ch.get("is_virtual") and ch.get("original_id"):
                        virtual_children.setdefault(str(ch["original_id"]), []).append(
                            str(ch.get("id"))
                        )

                chapter_tuples: list[tuple[Any, ...]] = []
                seen_nums: set[int] = set()
                order_idx = 0
                for ch in raw_chapters:
                    if not isinstance(ch, dict):
                        continue
                    if ch.get("is_virtual"):
                        continue
                    order_idx += 1
                    cid = str(ch.get("id") or order_idx)
                    cnum = _parse_chapter_num(cid, order_idx)
                    while cnum in seen_nums:
                        cnum += 1
                    seen_nums.add(cnum)

                    title = str(ch.get("title") or ch.get("filename") or f"第{cnum}章")
                    is_empty = 1 if ch.get("is_empty") else 0
                    raw_rel = str(ch.get("split_file") or f"02_workspace/split/{cid}.txt")
                    ref_rel = f"02_workspace/reconstructed/chapter_{cid}.txt"

                    prev = existing_rows.get(cnum)
                    ref_abs = self.output_dir / ref_rel
                    ref_exists = ref_abs.is_file()
                    v_kids = virtual_children.get(cid) or []
                    v_all_done = bool(v_kids) and all(vk in done_cids for vk in v_kids)

                    if is_empty:
                        ch_status = "empty"
                    elif (
                        ref_exists
                        or ref_rel in completed_files
                        or cid in done_cids
                        or v_all_done
                        or (prev and prev.get("status") == "ready_for_review" and ref_exists)
                    ):
                        ch_status = "ready_for_review"
                    else:
                        ch_status = "pending"

                    # review_status：优先读 progress.chapter_reviews，否则保留库中已有值
                    rev_entry = review_map.get(cid)
                    if isinstance(rev_entry, dict) and rev_entry.get("status") in VALID_REVIEW_STATUSES:
                        review_status = str(rev_entry["status"])
                    elif isinstance(rev_entry, str) and rev_entry in VALID_REVIEW_STATUSES:
                        review_status = rev_entry
                    elif prev and str(prev.get("review_status")) in VALID_REVIEW_STATUSES:
                        review_status = str(prev["review_status"])
                    else:
                        review_status = "pending"

                    # 字数缓存：已算过且文件未变空时直接复用，只在首次或刚就绪时读盘
                    prev_char_raw = int(prev["char_raw"]) if prev else 0
                    char_raw = (
                        prev_char_raw
                        if prev_char_raw > 0
                        else _file_char_len(self.output_dir / raw_rel)
                    )
                    prev_char_ref = int(prev["char_refactored"]) if prev else 0
                    if ref_exists and (prev_char_ref == 0 or review_status == "edited"):
                        char_ref = _file_char_len(ref_abs)
                    elif ref_exists:
                        char_ref = prev_char_ref
                    else:
                        char_ref = 0

                    # 合并虚拟子章的质检问题到父逻辑章
                    ch_issues = list(per_chapter_qc.get(cid, []))
                    for vk in v_kids:
                        ch_issues.extend(per_chapter_qc.get(vk, []))
                    qc_json = json.dumps(ch_issues, ensure_ascii=False, separators=(",", ":"))
                    regen_cnt = int(regen_map.get(cid, 0) or 0)

                    chapter_tuples.append(
                        (
                            cnum,
                            cid,
                            order_idx,
                            title,
                            raw_rel,
                            ref_rel,
                            ch_status,
                            review_status,
                            qc_json,
                            regen_cnt,
                            char_raw,
                            char_ref,
                            is_empty,
                            now,
                        )
                    )

                conn.execute("DELETE FROM chapters")
                if chapter_tuples:
                    conn.executemany(
                        """
                        INSERT INTO chapters (
                            chapter_num, chapter_id, sort_order, title,
                            raw_file, refactored_file, status, review_status,
                            qc_json, regen_count, char_raw, char_refactored,
                            is_empty, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        chapter_tuples,
                    )

            err_logs = progress.get("error_logs")
            if isinstance(err_logs, list):
                conn.execute("DELETE FROM error_logs")
                err_tuples = [
                    (
                        str(e.get("ts") or now),
                        str(e.get("kind") or "unknown"),
                        str(e.get("code") or "unknown"),
                        str(e.get("message") or ""),
                    )
                    for e in err_logs[-100:]
                    if isinstance(e, dict)
                ]
                if err_tuples:
                    conn.executemany(
                        "INSERT INTO error_logs (ts, kind, code, message) VALUES (?, ?, ?, ?)",
                        err_tuples,
                    )

            conn.commit()

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        try:
            qc_issues = json.loads(str(row["qc_json"] or "[]"))
            if not isinstance(qc_issues, list):
                qc_issues = []
        except json.JSONDecodeError:
            qc_issues = []
        return {
            "chapter_num": int(row["chapter_num"]),
            "chapter_id": str(row["chapter_id"]),
            "title": str(row["title"]),
            "raw_file": str(row["raw_file"]),
            "refactored_file": str(row["refactored_file"]),
            "status": str(row["status"]),
            "review_status": str(row["review_status"]),
            "qc_issues": qc_issues,
            "regen_count": int(row["regen_count"]),
            "char_raw": int(row["char_raw"]),
            "char_refactored": int(row["char_refactored"]),
            "is_empty": bool(row["is_empty"]),
            "updated_at": str(row["updated_at"]),
        }

    def list_chapters_summary(self, *, include_empty: bool = False) -> list[dict[str, Any]]:
        """O(1) 索引顺序读取章节列表（不碰 `progress.json`、不做大字典深拷贝）。"""
        with self._lock:
            if self._conn is None and not self.db_path.is_file():
                return []
            conn = self._get_conn()
            if include_empty:
                rows = conn.execute(
                    "SELECT * FROM chapters ORDER BY sort_order ASC, chapter_num ASC"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM chapters WHERE is_empty = 0 ORDER BY sort_order ASC, chapter_num ASC"
                ).fetchall()
            return [self._row_to_dict(r) for r in rows]

    def get_chapter_record(self, chapter_num: int | str) -> dict[str, Any] | None:
        """按主键 / chapter_id 单行精确查询（<1ms）。"""
        with self._lock:
            if self._conn is None and not self.db_path.is_file():
                return None
            conn = self._get_conn()
            raw_str = str(chapter_num).strip()
            row = None
            if isinstance(chapter_num, int) or raw_str.isdigit():
                row = conn.execute(
                    "SELECT * FROM chapters WHERE chapter_num = ?", (int(raw_str),)
                ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT * FROM chapters WHERE chapter_id = ?", (raw_str,)
                ).fetchone()
            return self._row_to_dict(row) if row is not None else None

    def update_chapter_review(
        self,
        chapter_num: int | str,
        *,
        review_status: str,
        qc_issues: list[dict[str, Any]] | None = None,
        char_refactored: int | None = None,
        refactored_file: str | None = None,
        status: str | None = None,
        regen_count: int | None = None,
    ) -> dict[str, Any]:
        """原子更新单章审阅态、字数与质检结果（O(1) 单行写）。"""
        if review_status not in VALID_REVIEW_STATUSES:
            raise ValueError(f"非法的 review_status: {review_status}")
        now = _utc_now()
        with self._lock:
            rec = self.get_chapter_record(chapter_num)
            if rec is None:
                raise KeyError(f"章节不存在: {chapter_num}")
            cnum = rec["chapter_num"]
            new_qc = (
                json.dumps(qc_issues, ensure_ascii=False, separators=(",", ":"))
                if qc_issues is not None
                else json.dumps(rec["qc_issues"], ensure_ascii=False, separators=(",", ":"))
            )
            new_char_ref = (
                int(char_refactored)
                if char_refactored is not None
                else int(rec["char_refactored"])
            )
            new_ref_file = (
                str(refactored_file) if refactored_file is not None else str(rec["refactored_file"])
            )
            new_status = str(status) if status is not None else str(rec["status"])
            new_regen = int(regen_count) if regen_count is not None else int(rec["regen_count"])

            conn = self._get_conn()
            conn.execute(
                """
                UPDATE chapters
                SET review_status = ?,
                    qc_json = ?,
                    char_refactored = ?,
                    refactored_file = ?,
                    status = ?,
                    regen_count = ?,
                    updated_at = ?
                WHERE chapter_num = ?
                """,
                (
                    review_status,
                    new_qc,
                    new_char_ref,
                    new_ref_file,
                    new_status,
                    new_regen,
                    now,
                    cnum,
                ),
            )
            conn.commit()
            updated = self.get_chapter_record(cnum)
            assert updated is not None
            return updated

    def record_batch_ready(
        self,
        batch_id: int,
        chapter_nums: list[int | str],
        *,
        qc_issues: list[dict[str, Any]] | None = None,
        output_files: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Phase 3 单批次落盘后立即将对应章节标记为 `ready_for_review`（供「边跑边审」实时推送）。"""
        now = _utc_now()
        issues_list = list(qc_issues or [])
        by_cid: dict[str, list[dict[str, Any]]] = {}
        for item in issues_list:
            if isinstance(item, dict) and item.get("chapter_id") is not None:
                by_cid.setdefault(str(item["chapter_id"]), []).append(item)

        with self._lock:
            conn = self._get_conn()
            conn.execute(
                """
                INSERT INTO batches (batch_id, chapter_ids_json, status, output_files_json, qc_json, updated_at)
                VALUES (?, ?, 'phase3_done', ?, ?, ?)
                ON CONFLICT(batch_id) DO UPDATE SET
                    status = 'phase3_done',
                    output_files_json = excluded.output_files_json,
                    qc_json = excluded.qc_json,
                    updated_at = excluded.updated_at
                """,
                (
                    int(batch_id),
                    json.dumps([str(x) for x in chapter_nums], ensure_ascii=False),
                    json.dumps(list(output_files or []), ensure_ascii=False),
                    json.dumps(issues_list, ensure_ascii=False),
                    now,
                ),
            )

            ready_records: list[dict[str, Any]] = []
            seen_cnums: set[int] = set()
            for raw_ch in chapter_nums:
                rec = self.get_chapter_record(raw_ch)
                if rec is None:
                    # 虚拟章 id（如 "1_0"）折叠回父章编号
                    head = str(raw_ch).split("_", 1)[0]
                    rec = self.get_chapter_record(head) if head != str(raw_ch) else None
                if rec is None or rec["chapter_num"] in seen_cnums:
                    continue
                seen_cnums.add(rec["chapter_num"])
                cnum = rec["chapter_num"]
                cid = rec["chapter_id"]
                raw_len = rec["char_raw"] or _file_char_len(self.output_dir / rec["raw_file"])
                ref_len = _file_char_len(self.output_dir / rec["refactored_file"])
                ch_qc = by_cid.get(cid, rec["qc_issues"])
                conn.execute(
                    """
                    UPDATE chapters
                    SET status = 'ready_for_review',
                        char_raw = ?,
                        char_refactored = ?,
                        qc_json = ?,
                        updated_at = ?
                    WHERE chapter_num = ?
                    """,
                    (
                        raw_len,
                        ref_len,
                        json.dumps(ch_qc, ensure_ascii=False, separators=(",", ":")),
                        now,
                        cnum,
                    ),
                )
            conn.commit()
            for cnum in sorted(seen_cnums):
                updated = self.get_chapter_record(cnum)
                if updated is not None:
                    ready_records.append(updated)
            return ready_records

    def record_gate_event(
        self,
        gate: str,
        action: str,
        *,
        run_id: str | None = None,
        phase: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """记录人工闸门事件留痕（进入/确认/超时）。"""
        now = _utc_now()
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                """
                INSERT INTO gate_events (run_id, gate, action, phase, detail_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    gate,
                    action,
                    phase,
                    json.dumps(detail or {}, ensure_ascii=False, separators=(",", ":")),
                    now,
                ),
            )
            conn.commit()


_POOL_LOCK = threading.Lock()
_OPEN_DBS: dict[str, ProjectDB] = {}


def get_project_db(output_dir: str | Path) -> ProjectDB:
    """获取或创建指定输出目录的共享 `ProjectDB` 实例（线程安全）。"""
    key = _norm_dir_key(output_dir)
    with _POOL_LOCK:
        db = _OPEN_DBS.get(key)
        if db is None:
            db = ProjectDB(output_dir)
            _OPEN_DBS[key] = db
        return db


def checkpoint_project_db(output_dir: str | Path) -> None:
    """对指定输出目录的 `.tfproj.db` 执行 TRUNCATE checkpoint 并释放句柄。"""
    key = _norm_dir_key(output_dir)
    with _POOL_LOCK:
        db = _OPEN_DBS.pop(key, None)
    if db is not None:
        db.checkpoint_and_close()
        return
    p = Path(output_dir)
    if (p / DB_FILENAME).is_file():
        ProjectDB(p).checkpoint_and_close()


def close_all_project_dbs() -> None:
    """关闭进程内所有缓存的 `ProjectDB` 实例（供测试清理或停机钩子调用）。"""
    with _POOL_LOCK:
        items = list(_OPEN_DBS.values())
        _OPEN_DBS.clear()
    for db in items:
        db.checkpoint_and_close()
