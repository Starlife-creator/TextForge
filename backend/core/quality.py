"""成品质量报告：把「这次重构到底怎么样」变成可核查的事实。

只做**结构性与可机械验证**的检查（缺失、塌缩、重复、占位、残留标记、设定冲突），
不假装能判断文学质量。报告写到 _diagnostics/quality_report.json，前端按需展示；
生成失败绝不影响成品导出。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections import defaultdict
from pathlib import Path

from utils.atomic import atomic_write_json

logger = logging.getLogger("textforge")

SCHEMA = 1
SHINGLE = 60
SHINGLE_STEP = 10
MIN_SHARED = 2
MIN_CONTAINMENT = 0.01
MAX_DUPLICATE_PAIRS = 20
MAX_POSTINGS = 30

RESIDUAL_MARKERS = ("===CHAPTER_BREAK===", "===PREV_END===", "===NEXT_START===")
# 最后一组是模型元话术（不是小说正文）：出现即说明模型把「回复用户」的措辞
# 写进了成品。只收几乎不可能出现在小说对白里的固定短语，避免误伤剧情。
PLACEHOLDERS = (
    "todo", "占位符", "待补", "此处省略", "此处略", "未完成待续",
    "作为ai", "作为 ai", "作为一个ai", "作为一个 ai",
    "以下是重构", "以下是修改后", "希望以上内容",
    "如需继续请告诉", "如果你需要我继续",
)

BAD = "bad"
WARN = "warn"


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", newline="")
    except OSError:
        return ""


def _snippet(text: str, idx: int, length: int, radius: int = 24) -> str:
    start = max(0, idx - radius)
    end = min(len(text), idx + length + radius)
    body = re.sub(r"\s+", " ", text[start:end]).strip()
    return ("…" if start > 0 else "") + body + ("…" if end < len(text) else "")


def find_hits(text: str, needles) -> list[tuple[str, str]]:
    """返回 (命中词, 上下文片段)，每个词只取第一次出现。"""
    low = text.casefold()
    hits: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in needles or []:
        needle = str(raw).strip()
        key = needle.casefold()
        if len(needle) < 2 or key in seen:
            continue
        idx = low.find(key)
        if idx >= 0:
            seen.add(key)
            hits.append((needle, _snippet(text, idx, len(needle))))
    return hits


def shingle_set(text: str) -> set[str]:
    """去掉空白后切成 60 字片段；步长 10，既能抓改几个字的重复也能控制开销。"""
    compact = re.sub(r"\s+", "", text or "")
    if not compact:
        return set()
    if len(compact) <= SHINGLE:
        return {compact}
    return {
        compact[i : i + SHINGLE]
        for i in range(0, len(compact) - SHINGLE + 1, SHINGLE_STEP)
    }


def find_duplicate_pairs(texts: dict[str, str]) -> list[dict]:
    """跨章重复：共享 60 字片段 >=2 且占较短章 >=1%。"""
    sets = {cid: shingle_set(text) for cid, text in texts.items()}
    index: dict[str, list[str]] = defaultdict(list)
    for cid, shingles in sets.items():
        for sh in shingles:
            index[sh].append(cid)

    pair_counts: dict[tuple[str, str], int] = defaultdict(int)
    for ids in index.values():
        if not (2 <= len(ids) <= MAX_POSTINGS):
            continue
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                pair_counts[(ids[i], ids[j])] += 1

    dups: list[dict] = []
    for (a, b), shared in pair_counts.items():
        base = min(len(sets[a]), len(sets[b]))
        min_shared = 1 if base <= 2 else MIN_SHARED
        if base and shared >= min_shared and shared / base >= MIN_CONTAINMENT:
            dups.append(
                {
                    "a": a,
                    "b": b,
                    "shared_shingles": shared,
                    "containment": round(shared / base, 4),
                }
            )
    dups.sort(key=lambda d: (-d["shared_shingles"], d["a"], d["b"]))
    return dups[:MAX_DUPLICATE_PAIRS]


def _chapter_label(ch: dict) -> str:
    title = ch.get("title") or ch.get("filename") or f"chapter_{ch.get('id')}"
    return str(title)


def assess_batch_texts(
    *,
    chapters: list[dict],
    chapter_ids: list[str],
    texts: dict[str, str],
    output_path: str | Path,
    forbidden_canon=(),
    name_map=None,
    refactor_mode: str = "full_rewrite",
    gates=None,
) -> list[dict]:
    """在批次落盘前做内容级检查，返回按严重度排序的问题清单。

    与最终质量报告共用同一组阈值，但只覆盖本批章节；用于决定是否自动重写。
    这里只判断可机械验证的硬伤，不评价文笔。
    """
    output_path = Path(output_path)
    by_id = {str(c.get("id")): c for c in chapters or []}
    forbidden = list(forbidden_canon or [])
    old_names = [k for k in (name_map or {}) if len(str(k).strip()) >= 2]
    reskin = (refactor_mode or "") == "reskin"
    inject_forbidden = bool((gates or {}).get("inject_forbidden"))
    issues: list[dict] = []

    def add(severity: str, code: str, cid: str, message: str, evidence: str = "") -> None:
        issues.append(
            {
                "severity": severity,
                "code": code,
                "chapter_id": str(cid),
                "message": message,
                "evidence": evidence,
            }
        )

    seen_hashes: dict[str, str] = {}
    for raw_cid in chapter_ids or []:
        cid = str(raw_cid)
        ch = by_id.get(cid)
        if ch is None:
            add(BAD, "missing_chapter", cid, f"章节 {cid} 不在章节表中")
            continue
        if ch.get("is_empty"):
            continue
        split_file = ch.get("split_file") or f"02_workspace/split/{cid}.txt"
        orig = _read_text(output_path / split_file).strip()
        recon = (texts.get(cid) or "").strip()
        if not orig:
            continue
        label = _chapter_label(ch)
        if not recon:
            add(BAD, "empty_chapter", cid, f"第 {cid} 章（{label}）重构稿为空")
            continue

        for marker in RESIDUAL_MARKERS:
            idx = recon.find(marker)
            if idx >= 0:
                add(
                    BAD,
                    "residual_marker",
                    cid,
                    f"第 {cid} 章（{label}）残留机器标记 {marker}",
                    _snippet(recon, idx, len(marker)),
                )
                break

        ph_hits = find_hits(recon, PLACEHOLDERS)
        if ph_hits:
            needle, snippet = ph_hits[0]
            add(
                BAD,
                "placeholder",
                cid,
                f"第 {cid} 章（{label}）疑似占位/未完稿（出现「{needle}」）",
                snippet,
            )

        if inject_forbidden and forbidden:
            fh = find_hits(recon, forbidden)
            if fh:
                needle, snippet = fh[0]
                add(
                    BAD,
                    "forbidden_canon",
                    cid,
                    f"第 {cid} 章（{label}）命中禁用设定「{needle}」",
                    snippet,
                )

        if reskin and old_names:
            oh = find_hits(recon, old_names)
            if oh:
                needle, snippet = oh[0]
                add(
                    WARN,
                    "old_name_residue",
                    cid,
                    f"第 {cid} 章（{label}）仍出现改名前的旧称「{needle}」",
                    snippet,
                )

        orig_len, recon_len = len(orig), len(recon)
        ratio = recon_len / orig_len if orig_len else None
        if ratio is not None:
            if orig_len >= 500 and ratio < 0.30:
                add(
                    BAD,
                    "content_collapse",
                    cid,
                    f"第 {cid} 章（{label}）明显塌缩：{orig_len} 字 → {recon_len} 字（{ratio:.0%}）",
                )
            elif orig_len >= 500 and ratio > 3.0:
                add(
                    BAD,
                    "content_expansion",
                    cid,
                    f"第 {cid} 章（{label}）异常膨胀：{orig_len} 字 → {recon_len} 字（{ratio:.0%}）",
                )
            elif ratio < 0.55 or ratio > 1.8:
                add(
                    WARN,
                    "length_off",
                    cid,
                    f"第 {cid} 章（{label}）篇幅偏离较大：{orig_len} 字 → {recon_len} 字（{ratio:.0%}）",
                )

        digest = hashlib.sha256(recon.encode("utf-8")).hexdigest()
        if digest in seen_hashes:
            add(
                BAD,
                "duplicate_chapter",
                cid,
                f"第 {cid} 章（{label}）与第 {seen_hashes[digest]} 章内容完全相同",
            )
        else:
            seen_hashes[digest] = cid

    issues.sort(key=lambda i: 0 if i["severity"] == BAD else 1)
    return issues


def has_blocking_issues(issues) -> bool:
    return any(i.get("severity") == BAD for i in issues or [])


def build_quality_report(
    *,
    chapters: list[dict],
    texts: list[str],
    progress: dict,
    req,
    output_path: str | Path,
) -> dict:
    output_path = Path(output_path)
    forbidden = list(getattr(req, "forbidden_canon", []) or [])
    name_map = dict(getattr(req, "name_map", {}) or {})
    old_names = [k for k in name_map if len(str(k).strip()) >= 2]
    reskin = getattr(req, "refactor_mode", "full_rewrite") == "reskin"

    issues: list[dict] = []
    rows: list[dict] = []
    recon_texts: dict[str, str] = {}

    def add_issue(severity: str, code: str, message: str, *, chapter_id=None,
                  evidence: str = "") -> None:
        issues.append(
            {
                "severity": severity,
                "code": code,
                "chapter_id": None if chapter_id is None else str(chapter_id),
                "message": message,
                "evidence": evidence,
            }
        )

    total_orig = 0
    total_recon = 0
    skipped_empty = 0

    for ch, recon in zip(chapters, texts, strict=False):
        cid = str(ch.get("id"))
        label = _chapter_label(ch)
        split_file = ch.get("split_file") or f"02_workspace/split/{cid}.txt"
        orig = _read_text(output_path / split_file)
        orig_len = len(orig.strip())
        recon = recon or ""
        recon_len = len(recon.strip())

        if ch.get("is_empty") or orig_len == 0:
            skipped_empty += 1
            if recon_len:
                add_issue(
                    WARN,
                    "unexpected_content",
                    f"第 {cid} 章（{label}）原文为空，重构稿却有 {recon_len} 字",
                    chapter_id=cid,
                )
            rows.append(
                {
                    "id": cid,
                    "title": label,
                    "original_chars": orig_len,
                    "reconstructed_chars": recon_len,
                    "ratio": None,
                    "status": "skipped",
                    "issue_codes": [],
                }
            )
            continue

        total_orig += orig_len
        total_recon += recon_len
        recon_texts[cid] = recon
        ratio = round(recon_len / orig_len, 3) if orig_len else None
        codes: list[str] = []
        chapter_bad = False
        chapter_warn = False

        def flag(severity: str, code: str, message: str, evidence: str = "",
                 _codes: list[str] = codes, _cid: str = cid) -> None:
            nonlocal chapter_bad, chapter_warn
            _codes.append(code)
            chapter_bad = chapter_bad or severity == BAD
            chapter_warn = chapter_warn or severity == WARN
            add_issue(severity, code, message, chapter_id=_cid, evidence=evidence)

        if recon_len == 0:
            flag(BAD, "empty_chapter", f"第 {cid} 章（{label}）重构稿为空")
        else:
            for marker in RESIDUAL_MARKERS:
                idx = recon.find(marker)
                if idx >= 0:
                    flag(
                        BAD,
                        "residual_marker",
                        f"第 {cid} 章（{label}）残留机器标记 {marker}",
                        _snippet(recon, idx, len(marker)),
                    )
                    break

            ph_hits = find_hits(recon, PLACEHOLDERS)
            if ph_hits:
                needle, snippet = ph_hits[0]
                flag(
                    BAD,
                    "placeholder",
                    f"第 {cid} 章（{label}）疑似占位/未完稿（出现「{needle}」）",
                    snippet,
                )

            fh = find_hits(recon, forbidden)
            if fh:
                needle, snippet = fh[0]
                flag(
                    BAD,
                    "forbidden_canon",
                    f"第 {cid} 章（{label}）命中禁用设定「{needle}」",
                    snippet,
                )

            if reskin and old_names:
                oh = find_hits(recon, old_names)
                if oh:
                    needle, snippet = oh[0]
                    flag(
                        WARN,
                        "old_name_residue",
                        f"第 {cid} 章（{label}）仍出现改名前的旧称「{needle}」",
                        snippet,
                    )

            if ratio is not None:
                if orig_len >= 500 and ratio < 0.30:
                    flag(
                        BAD,
                        "content_collapse",
                        f"第 {cid} 章（{label}）明显塌缩：{orig_len} 字 → {recon_len} 字（{ratio:.0%}）",
                    )
                elif orig_len >= 500 and ratio > 3.0:
                    flag(
                        BAD,
                        "content_expansion",
                        f"第 {cid} 章（{label}）异常膨胀：{orig_len} 字 → {recon_len} 字（{ratio:.0%}）",
                    )
                elif ratio < 0.55 or ratio > 1.8:
                    flag(
                        WARN,
                        "length_off",
                        f"第 {cid} 章（{label}）篇幅偏离较大：{orig_len} 字 → {recon_len} 字（{ratio:.0%}）",
                    )

        rows.append(
            {
                "id": cid,
                "title": label,
                "original_chars": orig_len,
                "reconstructed_chars": recon_len,
                "ratio": ratio,
                "status": BAD if chapter_bad else (WARN if chapter_warn else "ok"),
                "issue_codes": codes,
            }
        )

    duplicates = find_duplicate_pairs(recon_texts)
    if duplicates:
        by_id = {r["id"]: r for r in rows}
        for d in duplicates:
            add_issue(
                BAD,
                "cross_chapter_duplicate",
                f"第 {d['a']} 章与第 {d['b']} 章存在 {d['shared_shingles']} 处相同长片段"
                f"（占较短章 {d['containment']:.0%}）",
            )
            for cid in (d["a"], d["b"]):
                row = by_id.get(cid)
                if row is not None:
                    if "duplicate" not in row["issue_codes"]:
                        row["issue_codes"].append("duplicate")
                    row["status"] = BAD

    if progress.get("qc_issues"):
        add_issue(
            WARN,
            "qc_issue",
            "重构阶段质检记录：" + "；".join(str(x) for x in progress["qc_issues"][:10]),
        )
    if progress.get("pre_stitch_issues"):
        add_issue(
            WARN,
            "pre_stitch_qc",
            "批次质检仍有未消除的问题："
            + "；".join(
                str(i.get("message") if isinstance(i, dict) else i)
                for i in progress["pre_stitch_issues"][:10]
            ),
        )
    if progress.get("auto_rewrite_log"):
        kept = sum(1 for x in progress["auto_rewrite_log"] if x.get("kept") == "rewrite")
        add_issue(
            WARN,
            "auto_rewrite",
            f"自动重写 {len(progress['auto_rewrite_log'])} 个批次（采用重写稿 {kept} 个），"
            "建议重点抽读这些批次",
        )
    if progress.get("stitch_failures"):
        preview = "；".join(
            f"{x.get('boundary')}: {x.get('error')}"
            for x in progress["stitch_failures"][:5]
            if isinstance(x, dict)
        )
        add_issue(
            WARN,
            "stitch_skipped",
            f"{len(progress['stitch_failures'])} 处缝合失败已跳过（未应用的补丁保持原文），"
            f"建议人工检查：{preview}",
        )
    if progress.get("config_drift"):
        add_issue(
            WARN,
            "config_drift",
            "续跑时配置不一致：" + "；".join(str(x) for x in progress["config_drift"][:10]),
        )

    issues.sort(key=lambda i: 0 if i["severity"] == BAD else 1)
    counts = {BAD: 0, WARN: 0}
    for issue in issues:
        counts[issue["severity"]] = counts.get(issue["severity"], 0) + 1
    status = BAD if counts[BAD] else (WARN if counts[WARN] else "good")
    if status == "good":
        headline = "未发现结构性问题（自动检查不覆盖文风与情节质量，仍建议抽读）"
    elif status == "warn":
        headline = f"发现 {counts[WARN]} 处需要留意的风险，未发现硬性缺陷"
    else:
        headline = f"发现 {counts[BAD]} 处必须处理的问题"

    def count_code(code: str) -> int:
        return sum(1 for i in issues if i["code"] == code)

    report = {
        "schema": SCHEMA,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "status": status,
        "headline": headline,
        "summary": {
            "chapters": len(chapters),
            "chapters_reconstructed": len(recon_texts),
            "chapters_skipped_empty": skipped_empty,
            "chapters_with_issues": len(
                {i["chapter_id"] for i in issues if i["chapter_id"]}
            ),
            "total_original_chars": total_orig,
            "total_reconstructed_chars": total_recon,
            "length_ratio": round(total_recon / total_orig, 3) if total_orig else None,
            "duplicate_pairs": len(duplicates),
            "empty_chapters": count_code("empty_chapter"),
            "residual_markers": count_code("residual_marker"),
            "placeholder_hits": count_code("placeholder"),
            "forbidden_hits": count_code("forbidden_canon"),
            "old_name_hits": count_code("old_name_residue"),
            "auto_rewrites": len(progress.get("auto_rewrite_log") or []),
            "pre_stitch_issues": len(progress.get("pre_stitch_issues") or []),
            "stitch_failures": len(progress.get("stitch_failures") or []),
            "issues_by_severity": counts,
        },
        "issues": issues[:200],
        "issues_truncated": len(issues) > 200,
        "chapters": rows,
        "duplicates": duplicates,
        "config": {
            "novel_name": progress.get("novel_name"),
            "model": getattr(req, "model", None),
            "context_window": getattr(req, "context_window", None),
            "input_mode": getattr(req, "input_mode", None),
            "input_format": getattr(req, "input_format", None),
            "refactor_mode": getattr(req, "refactor_mode", "full_rewrite"),
            "refactor_gates": getattr(req, "refactor_gates", {}) or {},
            "prompt_version": progress.get("prompt_version"),
            "forbidden_canon_count": len(forbidden),
            "name_map_count": len(name_map),
            "forbidden_check": "字面匹配，仅能发现逐字出现的设定冲突",
        },
        "usage": progress.get("usage") or {},
        "caveat": (
            "自动检查覆盖：空章/篇幅塌缩或膨胀/跨章重复/占位未完稿/机器标记残留/"
            "禁用设定字面命中/改名残留。它不能判断文笔、节奏、人物弧光与情节合理性。"
        ),
    }
    return report


def write_quality_report(output_path: str | Path, report: dict) -> Path:
    path = Path(output_path) / "_diagnostics" / "quality_report.json"
    atomic_write_json(path, report)
    return path


def read_quality_report(output_path: str | Path) -> dict | None:
    path = Path(output_path) / "_diagnostics" / "quality_report.json"
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return obj if isinstance(obj, dict) else None
