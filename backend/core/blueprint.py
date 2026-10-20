"""phase2 的上下文构建：让全书摘要都参与蓝图，而不是只截取开头。

旧实现是 `all_summaries[:max_total]`：300 章的书，后 200 章对蓝图完全不可见，
而蓝图又会作为重构阶段的系统提示词 —— 结局、后期人物转变、后半段的逻辑断层
全都无从体现。这里改成按预算压缩：

1. 摘要总长未超预算：原样使用，零额外调用（与旧行为一致）。
2. 超预算：按连续区间分段，每段调一次模型压成「分卷脉络」，仍超则递归压缩。
   首段与末段一定参与，不会出现「只保留开头」。
3. 压缩结果按内容哈希缓存在 _diagnostics/blueprint_map/，续跑/重跑不重复付费。
4. 极端情况（单段仍超长 / 压缩不收敛）改为等距抽样，并在段内保留头尾，
   用「（中略）」明确标出缺口，绝不静默只留开头。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable
from pathlib import Path

from utils.atomic import atomic_write_json

logger = logging.getLogger("textforge")

SEP = "\n\n---\n\n"
CLIP_MARK = "\n……（中略）……\n"

MapCall = Callable[[str, int], Awaitable[str]]
ProgressFn = Callable[[int, int, int], None]


def _batch_no(path: Path) -> int:
    m = re.search(r"(\d+)", path.stem)
    return int(m.group(1)) if m else 0


def summary_files(output_path: str | Path) -> list[Path]:
    """按批号而不是文件名字典序排序：batch_100 必须排在 batch_99 之后。"""
    return sorted(
        (Path(output_path) / "01_summaries").glob("batch_*.txt"),
        key=lambda p: (_batch_no(p), p.name),
    )


def read_summaries(output_path: str | Path) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for f in summary_files(output_path):
        try:
            text = f.read_text(encoding="utf-8").strip()
        except OSError as e:
            logger.warning(f"[blueprint] 读取摘要失败 {f}: {e}")
            continue
        if text:
            pairs.append((f.stem, text))
    return pairs


def joined_len(items: list[str]) -> int:
    return sum(len(x) for x in items) + (len(SEP) * (len(items) - 1) if len(items) > 1 else 0)


def chunk_by_budget(items: list[str], budget: int) -> list[list[str]]:
    """把连续条目切成若干段，每段拼接后不超过 budget（单条超长时独占一段）。"""
    chunks: list[list[str]] = []
    cur: list[str] = []
    size = 0
    for item in items:
        add = len(item) + (len(SEP) if cur else 0)
        if cur and size + add > budget:
            chunks.append(cur)
            cur = [item]
            size = len(item)
        else:
            cur.append(item)
            size += add
    if cur:
        chunks.append(cur)
    return chunks


def clip_text(text: str, limit: int) -> str:
    """保留头尾、明确标注中略。limit 是最终长度上限。"""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= len(CLIP_MARK) + 8:
        return text[:limit]
    head = int((limit - len(CLIP_MARK)) * 0.65)
    tail = limit - len(CLIP_MARK) - head
    return text[:head] + CLIP_MARK + (text[-tail:] if tail > 0 else "")


def sample_evenly(items: list[str], budget: int) -> list[str]:
    """等距抽掉中间条目，首条与末条始终保留；仍超预算则对每条夹取头尾。"""
    if not items:
        return []
    if joined_len(items) <= budget:
        return list(items)
    idx = list(range(len(items)))
    while kept_len(items, idx) > budget and len(idx) > 2:
        new_idx = [idx[0], *idx[1:-1:2], idx[-1]]
        if len(new_idx) >= len(idx):
            mid = len(idx) // 2
            new_idx = idx[:mid] + idx[mid + 1:]
        idx = new_idx
    per_item = max(80, budget // len(idx) - len(SEP))
    return [clip_text(items[i], per_item) for i in idx]


def kept_len(items: list[str], idx: list[int]) -> int:
    return sum(len(items[i]) for i in idx) + len(SEP) * (len(idx) - 1)


def build_map_prompt(text: str, first: int, last: int, target_chars: int) -> str:
    # `【蓝图分卷摘要任务】` 是 mock/测试识别这一阶段调用的唯一标记。
    return f"""你是一位小说总编。下面按时间顺序给出同一部长篇小说的第 {first} 至第 {last} 段连续批次摘要。
请把它们压缩成一段不超过 {target_chars} 字的「分卷脉络」，供最终的全书蓝图使用。
只保留：
1. 这一段的主线事件推进（谁做了什么、结果如何）；
2. 人物关系与状态的关键变化；
3. 尚未回收的伏笔、明显的逻辑断层。
不要评价文笔，不要编造摘要里没有的情节，不要输出标题或分点编号。

【蓝图分卷摘要任务】
{text}
"""


def _cache_key(text: str, model: str, version: str) -> str:
    h = hashlib.sha256()
    for part in (version, model, text):
        h.update(part.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


def _cache_path(output_path: str | Path, key: str) -> Path:
    return Path(output_path) / "_diagnostics" / "blueprint_map" / f"arc_{key[:24]}.json"


def _load_cached(path: Path) -> str | None:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    text = obj.get("text") if isinstance(obj, dict) else None
    return text if isinstance(text, str) and text.strip() else None


def _save_cached(path: Path, *, key: str, text: str, model: str) -> None:
    try:
        atomic_write_json(path, {"key": key, "model": model, "text": text})
    except OSError as e:
        logger.warning(f"[blueprint] 写压缩缓存失败（忽略）{path}: {e}")


def _clean_map_output(text: str) -> str:
    text = (text or "").strip()
    m = re.fullmatch(r"```(?:[a-zA-Z]+)?\s*([\s\S]*?)```", text)
    return m.group(1).strip() if m else text


async def _map_one(
    chunk: list[str],
    *,
    first: int,
    last: int,
    map_call: MapCall,
    output_path: Path,
    model: str,
    version: str,
    meta: dict,
) -> str:
    text = SEP.join(chunk)
    key = _cache_key(text, model, version)
    path = _cache_path(output_path, key)
    cached = _load_cached(path)
    if cached is not None:
        meta["cache_hits"] += 1
        return cached

    target = max(240, min(900, int(len(text) * 0.22)))
    prompt = build_map_prompt(text, first, last, target)
    out = _clean_map_output(await map_call(prompt, target))
    meta["map_calls"] += 1
    if not out:
        # call_with_retry 已重试过空响应；这里保底，不让一个坏响应毁掉整轮
        out = clip_text(text, max(400, target))
        meta["fallback_items"] += 1
    else:
        _save_cached(path, key=key, text=out, model=model)
    return out


async def compress_summaries(
    items: list[str],
    *,
    budget: int,
    map_call: MapCall,
    output_path: str | Path,
    model: str,
    prompt_version: str,
    on_progress: ProgressFn | None = None,
    max_rounds: int = 4,
) -> tuple[list[str], dict]:
    """把有序摘要压进 budget 字符内，返回 (压缩后条目, 统计)。"""
    items = [x.strip() for x in items if x and x.strip()]
    budget = max(400, int(budget))
    meta = {
        "budget": budget,
        "original_items": len(items),
        "original_chars": joined_len(items),
        "final_items": len(items),
        "final_chars": joined_len(items),
        "rounds": 0,
        "map_calls": 0,
        "cache_hits": 0,
        "fallback_items": 0,
        "sampled": False,
    }
    out_path = Path(output_path)
    if not items or joined_len(items) <= budget:
        return items, meta

    chunk_budget = max(1200, int(budget * 0.72))
    while joined_len(items) > budget and len(items) > 1 and meta["rounds"] < max_rounds:
        chunks = chunk_by_budget(items, chunk_budget)
        if len(chunks) <= 1:
            break
        new_items: list[str] = []
        cursor = 1
        total = len(chunks)
        for done, chunk in enumerate(chunks, start=1):
            if len(chunk) == 1:
                new_items.append(chunk[0])
            else:
                new_items.append(
                    await _map_one(
                        chunk,
                        first=cursor,
                        last=cursor + len(chunk) - 1,
                        map_call=map_call,
                        output_path=out_path,
                        model=model,
                        version=prompt_version,
                        meta=meta,
                    )
                )
            cursor += len(chunk)
            if on_progress:
                on_progress(done, total, meta["rounds"] + 1)
        if joined_len(new_items) >= joined_len(items):
            break
        items = new_items
        meta["rounds"] += 1

    if joined_len(items) > budget:
        items = sample_evenly(items, budget)
        meta["sampled"] = True

    meta["final_items"] = len(items)
    meta["final_chars"] = joined_len(items)
    return items, meta


def read_gap_digest(
    diag_dir: str | None, *, max_chars: int = 3000, max_items: int = 40
) -> dict:
    """把结构化诊断里的断层压成一份短清单，优先 high/medium。"""
    empty = {"text": "", "total": 0, "included": 0}
    if not diag_dir or max_chars <= 0:
        return empty
    d = Path(diag_dir)
    if not d.is_dir():
        return empty

    entries: list[tuple[int, str, int, str]] = []
    for f in sorted(d.glob("diagnose_*.json"), key=lambda p: (_batch_no(p), p.name)):
        try:
            obj = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(obj, dict):
            continue
        batch_id = obj.get("batch_id") or _batch_no(f)
        for g in obj.get("gaps") or []:
            if not isinstance(g, dict):
                continue
            desc = str(g.get("description") or "").strip()
            if not desc:
                continue
            sev = str(g.get("severity") or "medium").lower()
            rank = {"high": 0, "medium": 1, "low": 2}.get(sev, 1)
            loc = str(g.get("location") or "").strip()
            sug = str(g.get("suggestion") or "").strip()
            line = f"- [{sev}] 第{batch_id}批"
            if loc:
                line += f" {loc}"
            line += f"：{desc}"
            if sug:
                line += f"（建议：{sug}）"
            entries.append((rank, f.name, len(entries), line))

    if not entries:
        return empty
    entries.sort(key=lambda x: (x[0], x[1], x[2]))

    selected: list[str] = []
    used = 0
    for *_, line in entries:
        if len(selected) >= max_items:
            break
        if used + len(line) + 1 > max_chars:
            remain = max_chars - used - 1
            if remain >= 40:
                selected.append(clip_text(line, remain))
            break
        selected.append(line)
        used += len(line) + 1

    text = "【结构化断层清单】\n" + "\n".join(selected) if selected else ""
    return {"text": text, "total": len(entries), "included": len(selected)}


async def build_blueprint_context(
    *,
    output_path: str | Path,
    req,
    progress: dict,
    map_call: MapCall,
    prompt_version: str,
    model: str | None = None,
    emit: Callable[[str, dict], None] | None = None,
) -> tuple[str, dict]:
    """返回送给最终蓝图提示词的上下文，并把统计写进 progress。"""
    pairs = read_summaries(output_path)
    if not pairs:
        raise RuntimeError("未找到章节摘要（01_summaries），无法生成蓝图；请从诊断阶段续跑")

    chars = req.context_window * 1.5
    max_total = int(chars * 0.60)
    gap = read_gap_digest(
        getattr(req, "diagnose_json", None),
        max_chars=max(0, min(3000, max_total // 3)),
    )
    gap_text = gap["text"]
    budget = max(400, max_total - (len(gap_text) + 2 if gap_text else 0))

    def on_progress(done: int, total: int, round_no: int) -> None:
        if emit:
            emit("blueprint_progress", {"done": done, "total": total, "round": round_no})

    items, meta = await compress_summaries(
        [text for _, text in pairs],
        budget=budget,
        map_call=map_call,
        output_path=output_path,
        model=str(model or getattr(req, "model", "") or ""),
        prompt_version=prompt_version,
        on_progress=on_progress,
    )
    context = SEP.join(items)
    if gap_text:
        context += "\n\n" + gap_text

    meta.update(
        {
            "summaries": len(pairs),
            "first_summary": pairs[0][0],
            "last_summary": pairs[-1][0],
            "summary_budget": budget,
            "final_chars": len(context),
            "max_total": max_total,
            "gap_total": gap["total"],
            "gap_included": gap["included"],
        }
    )
    progress["blueprint_context"] = meta
    logger.info(
        f"[phase2] 蓝图上下文：摘要 {len(pairs)} 段 / {meta['original_chars']} 字 → "
        f"{meta['final_items']} 段 / {meta['final_chars']} 字；"
        f"map 调用 {meta['map_calls']} 次、缓存命中 {meta['cache_hits']} 次、"
        f"抽样={meta['sampled']}；断层 {gap['included']}/{gap['total']}"
    )
    if meta["sampled"] and emit:
        emit(
            "error",
            {
                "kind": "warning",
                "code": "summaries_sampled",
                "message": (
                    f"摘要仍超出上下文预算，已等距抽样 {meta['final_items']}/{meta['original_items']} 段"
                    "（保留了全书首尾与中略标记）"
                ),
            },
        )
    return context, meta
