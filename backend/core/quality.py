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
from collections import Counter, defaultdict
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

# 缝合改动率的提醒阈值：平均改动率超过它，说明多数边界不是「补一句过渡」，而是接近整段重写。
# 必须**低于** stitch.STITCH_MAX_CHANGE_RATE（拒绝闸门 0.35）：闸门开着时，每个已应用的
# 补丁改动率都不可能超过 0.35 —— 阈值若设在 0.5，这条提醒就永远不会触发，等于把信号写死。
# 0.25 的含义是「普遍贴着上限改」：只提示抽读定位，不拦截。
STITCH_WARN_MEAN_RATE = 0.25
STITCH_WORST_ITEMS = 5

# ---- 篇幅 / 塌缩 / 膨胀判定阈值（#19：全部收口在本文件，取值依据写在注释里） ----
# 触发塌缩/膨胀判定所需的最小原文字数：原文太短时篇幅比噪声大，不参与硬伤判定。
COLLAPSE_MIN_ORIG_CHARS = 500
# 塌缩：重构稿不足原文 30% —— 典型「摘要化」或整段丢失，判 BAD。
COLLAPSE_RATIO = 0.30
# 膨胀：重构稿超过原文 3 倍 —— 模型掺水扩写或把一章拆成多章篇幅，判 BAD。
EXPANSION_RATIO = 3.0
# 篇幅偏离提醒（WARN，不拦截）：落在 55%~180% 之外即提示抽读；硬伤判定另有上两条。
WARN_LOW_RATIO = 0.55
WARN_HIGH_RATIO = 1.8

# ---- 报告容量上限 ----
# 报告里 issues 最多留存多少条；超出部分仍计入 summary 计数（不在生成阶段截断统计）。
MAX_ISSUES_REPORT = 200
# 逐条拼接进 warn 提示的问题条数（qc_issues / pre_stitch_issues / config_drift 共用）。
MAX_QC_ISSUE_PREVIEW = 10
# 缝合失败清单拼接预览的条数。
MAX_STITCH_FAILURE_PREVIEW = 5
# 每批次留存的质检问题条数（refactor.py 的 batch_qc 共用同一口径）。
MAX_BATCH_QC_ISSUES = 50
# 批次质检汇总进 pre_stitch_issues 的条数上限。
MAX_PRE_STITCH_ISSUES = 200

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

# 分句：只认句末标点。**换行不算边界** —— 缝合常常只是重新分段，
# 若把 \n 当边界，「同一段文字换个分行」会被量成整段改写。
_SENT_SPLIT_RE = re.compile(r"[。！？…；!?;]+")


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
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


def change_rate(original: str, replacement: str) -> float | None:
    """缝合补丁的改动率：原文句子中**没有被原样保留**的比例。

    口径是「分句后逐句对照」：0.0 = 一字未改（哪怕新增了几句过渡），
    1.0 = 原文句子一句都没留下。选这个口径而不是滑窗重合度，有两个实测原因：
      1. 60 字滑窗按步长 10 采样，**在中间插一句**会让后续片段整体错位，
         实测把「插入 13 字过渡」量成 52% 改动 —— 恰恰把最理想的结果判成最坏。
      2. 字符级 difflib 是 O(n²)，15000 字窗口单次要 1.6–8 秒；缝合是逐边界调用，
         这个开销没必要。分句对照是 O(n)。

    代价：句内改一个字就把整句计为改动，所以它**偏严不偏松** —— 作为将来
    「最小改动闸门」的判据，偏严是安全方向。
    句子不足 3 句时返回 **None**（窗口可能是一整段无标点长句，测量无意义），
    而不是 0 —— 把「无法判断」伪装成「没有改动」会让报告虚高。
    """
    a = _sentences(original)
    if len(a) < 3:
        return None
    kept = sum((Counter(a) & Counter(_sentences(replacement))).values())
    return round(1 - kept / len(a), 4)


def _sentences(text: str) -> list[str]:
    """先去掉**全部**空白（重新分段/换行不算改动），再按句末标点分句。"""
    compact = re.sub(r"\s+", "", text or "")
    return [s for s in _SENT_SPLIT_RE.split(compact) if s]


def stitch_metrics(anchors) -> dict:
    """汇总缝合补丁的观测值。只统计**已应用**的补丁：

    失败降级的补丁没有改到正文，计入的话会把「跳过」也算成「改写」。
    """
    applied = [a for a in anchors or [] if isinstance(a, dict) and a.get("applied")]
    measured = [
        a for a in applied
        if isinstance(a.get("change_rate"), (int, float))
    ]
    rates = sorted(a["change_rate"] for a in measured)
    mean = round(sum(rates) / len(rates), 4) if rates else None
    p90 = None
    if rates:
        rank = -((-len(rates) * 9) // 10)   # 最近秩 ceil(0.9n)
        p90 = round(rates[rank - 1], 4)
    worst = sorted(measured, key=lambda a: -a["change_rate"])[:STITCH_WORST_ITEMS]
    return {
        "applied_patches": len(applied),
        "measured": len(rates),
        "exposed_chars": sum(int(a.get("chars") or 0) for a in applied),
        "mean": mean,
        "p90": p90,
        "max": round(rates[-1], 4) if rates else None,
        "worst": [
            {
                "file": a.get("file"),
                "source": a.get("source"),
                "chars": a.get("chars"),
                "change_rate": a["change_rate"],
            }
            for a in worst
        ],
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
            if orig_len >= COLLAPSE_MIN_ORIG_CHARS and ratio < COLLAPSE_RATIO:
                add(
                    BAD,
                    "content_collapse",
                    cid,
                    f"第 {cid} 章（{label}）明显塌缩：{orig_len} 字 → {recon_len} 字（{ratio:.0%}）",
                )
            elif orig_len >= COLLAPSE_MIN_ORIG_CHARS and ratio > EXPANSION_RATIO:
                add(
                    BAD,
                    "content_expansion",
                    cid,
                    f"第 {cid} 章（{label}）异常膨胀：{orig_len} 字 → {recon_len} 字（{ratio:.0%}）",
                )
            elif ratio < WARN_LOW_RATIO or ratio > WARN_HIGH_RATIO:
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


# ---- #13 章节健康分最小闭环 ----
# 0~100 分：满分 100，按严重度与问题码扣分；空章直接 0 分。评分规则只机械可解释，
# 不做文学质量判断。阈值后续用 quality_report.json 积累的真机分布回填。
HEALTH_MAX = 100
HEALTH_GOOD_MIN = 85
HEALTH_WARN_MIN = 60
HEALTH_BAD_PENALTY = 40
HEALTH_WARN_PENALTY = 10
# 问题码专属扣分：硬伤码比通用严重度扣分更细，避免「一个 BAD 抵 40 分」一刀切。
HEALTH_CODE_PENALTY: dict[str, int] = {
    "empty_chapter": 100,
    "content_collapse": 35,
    "content_expansion": 35,
    "placeholder": 30,
    "residual_marker": 30,
    "forbidden_canon": 30,
    "duplicate_chapter": 30,
    "cross_chapter_duplicate": 30,
}


def health_grade(score: int) -> str:
    """健康分档位：good ≥85，warn 60~84，bad <60。"""
    if score >= HEALTH_GOOD_MIN:
        return "good"
    if score >= HEALTH_WARN_MIN:
        return "warn"
    return "bad"


def chapter_health_score(row: dict, issues: list[dict] | None = None) -> int:
    """按章节行与其问题清单算健康分（0~100），扣分制、不封顶下界。

    - 空章（empty_chapter）直接 0 分；
    - BAD 问题通用 -40、WARN 通用 -10；问题码专属扣分在严重度之上叠加；
    - 篇幅落在提醒带之外再 -8（与既有 length_off 阈值同源）。
    """
    issues = list(issues or [])
    score = HEALTH_MAX
    if row.get("status") == "skipped":
        return 0
    # 篇幅偏离额外扣分（不与问题码重复叠加到致命程度）
    ratio = row.get("ratio")
    if isinstance(ratio, (int, float)) and (ratio < WARN_LOW_RATIO or ratio > WARN_HIGH_RATIO):
        score -= 8
    for issue in issues:
        code = str(issue.get("code") or "")
        score -= HEALTH_CODE_PENALTY.get(code, HEALTH_BAD_PENALTY if issue.get("severity") == BAD
                                          else HEALTH_WARN_PENALTY)
    return max(0, min(HEALTH_MAX, score))


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
    # 缝合的两条护栏阈值：报告要能自证「当时是按什么口径放行/拒绝的」。
    # 延迟导入：这些值所在的模块在顶层依赖本模块，直接 import 会成环。
    from core.budget import STITCH_MAX_WINDOW_CHARS
    from core.stitch import STITCH_MAX_CHANGE_RATE

    issues: list[dict] = []
    rows: list[dict] = []
    recon_texts: dict[str, str] = {}
    # #13：按章节归集问题，供健康分逐章扣分；跨章重复问题在循环结束后补入。
    chapter_issues: dict[str, list[dict]] = {}

    def add_issue(severity: str, code: str, message: str, *, chapter_id=None,
                  evidence: str = "") -> None:
        entry = {
            "severity": severity,
            "code": code,
            "chapter_id": None if chapter_id is None else str(chapter_id),
            "message": message,
            "evidence": evidence,
        }
        issues.append(entry)
        if chapter_id is not None:
            chapter_issues.setdefault(str(chapter_id), []).append(entry)

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
                    "health_score": 0,
                    "health_grade": "bad",
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
                if orig_len >= COLLAPSE_MIN_ORIG_CHARS and ratio < COLLAPSE_RATIO:
                    flag(
                        BAD,
                        "content_collapse",
                        f"第 {cid} 章（{label}）明显塌缩：{orig_len} 字 → {recon_len} 字（{ratio:.0%}）",
                    )
                elif orig_len >= COLLAPSE_MIN_ORIG_CHARS and ratio > EXPANSION_RATIO:
                    flag(
                        BAD,
                        "content_expansion",
                        f"第 {cid} 章（{label}）异常膨胀：{orig_len} 字 → {recon_len} 字（{ratio:.0%}）",
                    )
                elif ratio < WARN_LOW_RATIO or ratio > WARN_HIGH_RATIO:
                    flag(
                        WARN,
                        "length_off",
                        f"第 {cid} 章（{label}）篇幅偏离较大：{orig_len} 字 → {recon_len} 字（{ratio:.0%}）",
                    )

        row = {
            "id": cid,
            "title": label,
            "original_chars": orig_len,
            "reconstructed_chars": recon_len,
            "ratio": ratio,
            "status": BAD if chapter_bad else (WARN if chapter_warn else "ok"),
            "issue_codes": codes,
        }
        row["health_score"] = chapter_health_score(row, chapter_issues.get(cid, []))
        row["health_grade"] = health_grade(int(row["health_score"]))  # type: ignore[call-overload]
        rows.append(row)

    duplicates = find_duplicate_pairs(recon_texts)
    if duplicates:
        by_id: dict = {r["id"]: r for r in rows}
        for d in duplicates:
            dup_msg = (
                f"第 {d['a']} 章与第 {d['b']} 章存在 {d['shared_shingles']} 处相同长片段"
                f"（占较短章 {d['containment']:.0%}）"
            )
            add_issue(BAD, "cross_chapter_duplicate", dup_msg)
            for cid in (d["a"], d["b"]):
                row = by_id.get(cid)  # type: ignore[assignment]
                if row is not None:
                    issue_codes = row["issue_codes"]
                    if not isinstance(issue_codes, list):
                        issue_codes = []
                        row["issue_codes"] = issue_codes
                    if "duplicate" not in issue_codes:
                        issue_codes.append("duplicate")
                    row["status"] = BAD
                    chapter_issues.setdefault(cid, []).append(
                        {"severity": BAD, "code": "cross_chapter_duplicate",
                         "chapter_id": cid, "message": dup_msg, "evidence": ""}
                    )
                    row["health_score"] = chapter_health_score(
                        row, chapter_issues.get(cid, []))
                    row["health_grade"] = health_grade(int(row["health_score"]))  # type: ignore[call-overload]

    if progress.get("qc_issues"):
        add_issue(
            WARN,
            "qc_issue",
            "重构阶段质检记录："
            + "；".join(str(x) for x in progress["qc_issues"][:MAX_QC_ISSUE_PREVIEW]),
        )
    if progress.get("pre_stitch_issues"):
        add_issue(
            WARN,
            "pre_stitch_qc",
            "批次质检仍有未消除的问题："
            + "；".join(
                str(i.get("message") if isinstance(i, dict) else i)
                for i in progress["pre_stitch_issues"][:MAX_QC_ISSUE_PREVIEW]
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
            for x in progress["stitch_failures"][:MAX_STITCH_FAILURE_PREVIEW]
            if isinstance(x, dict)
        )
        add_issue(
            WARN,
            "stitch_skipped",
            f"{len(progress['stitch_failures'])} 处缝合失败已跳过（未应用的补丁保持原文），"
            f"建议人工检查：{preview}",
        )
    # 缝合改动率：观测值，用来回答「这次缝合到底动了多少字、动得有多狠」。
    # 只有平均改动率越过提醒阈值才产生一条 warn；平时它只是报告里的一组数字。
    stitch = stitch_metrics(progress.get("stitch_anchors") or [])
    if (
        stitch["measured"]
        and stitch["mean"] is not None
        and stitch["mean"] > STITCH_WARN_MEAN_RATE
    ):
        add_issue(
            WARN,
            "stitch_bulk_rewrite",
            f"{stitch['measured']} 处缝合补丁平均改动率 {stitch['mean']:.0%}"
            f"（提醒阈值 {STITCH_WARN_MEAN_RATE:.0%}：多数边界不只是补一句过渡），"
            f"建议对照抽读改动最大的{len(stitch['worst'])} 处",
        )
    if progress.get("config_drift"):
        add_issue(
            WARN,
            "config_drift",
            "续跑时配置不一致："
            + "；".join(str(x) for x in progress["config_drift"][:MAX_QC_ISSUE_PREVIEW]),
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

    # #13：健康分聚合与指标快照。issues_total 与 issue_code_counts 对**全量**问题计数，
    # 不受报告 issues 列表 MAX_ISSUES_REPORT 截断影响，供真机分布积累后回填阈值。
    scores = [r["health_score"] for r in rows]
    avg_health = round(sum(scores) / len(scores), 1) if scores else None
    health_good = sum(s >= HEALTH_GOOD_MIN for s in scores)
    health_warn = sum(HEALTH_WARN_MIN <= s < HEALTH_GOOD_MIN for s in scores)
    health_bad = sum(s < HEALTH_WARN_MIN for s in scores)
    code_counts: dict[str, int] = {}
    for i in issues:
        code_counts[i["code"]] = code_counts.get(i["code"], 0) + 1

    # #3：单章重生成次数进摘要（与 progress.regen_count 同源，供抽读定位反复驳回的章节）。
    regen_count = progress.get("regen_count") or {}
    regen_total = sum(int(v or 0) for v in regen_count.values())
    regen_chapters = sum(1 for v in regen_count.values() if int(v or 0) > 0)

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
            "regen_total": regen_total,
            "regen_chapters": regen_chapters,
            "pre_stitch_issues": len(progress.get("pre_stitch_issues") or []),
            "stitch_failures": len(progress.get("stitch_failures") or []),
            "stitch_patches": stitch["applied_patches"],
            "stitch_measured": stitch["measured"],
            "stitch_exposed_chars": stitch["exposed_chars"],
            "stitch_exposed_ratio": (
                round(stitch["exposed_chars"] / total_orig, 3) if total_orig else None
            ),
            "stitch_mean_rate": stitch["mean"],
            "stitch_p90_rate": stitch["p90"],
            "stitch_max_rate": stitch["max"],
            "issues_by_severity": counts,
            "issues_total": len(issues),
            "issue_code_counts": code_counts,
            "avg_health": avg_health,
            "health_trend": [r["health_score"] for r in rows],
            "health_good": health_good,
            "health_warn": health_warn,
            "health_bad": health_bad,
        },
        "issues": issues[:MAX_ISSUES_REPORT],
        "issues_truncated": len(issues) > MAX_ISSUES_REPORT,
        "chapters": rows,
        "duplicates": duplicates,
        "stitch": stitch,
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
            "stitch_metric": (
                "改动率 = 缝合窗口内原文句子没有被原样保留的比例（分句对照，忽略换行与分段）；"
                "0 = 只新增过渡未动原文，1 = 整段换写法。它是观测口径，不判断改写质量"
            ),
            "stitch_gate": (
                f"缝合窗口封顶 {STITCH_MAX_WINDOW_CHARS} 字；最小改动闸门"
                + (
                    f"已开启（改动率 > {STITCH_MAX_CHANGE_RATE:.0%} 的补丁被拒绝，该交界保留原文）"
                    if (getattr(req, "refactor_gates", {}) or {}).get(
                        "stitch_minimal_change", True)
                    else "已关闭（仅受替换段长度比 0.5~3.0 约束）"
                )
            ),
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
