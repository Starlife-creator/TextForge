"""批次拆分：上下文超限 / 契约失败时的降级机制。

为什么必须共享给 phase1 和 phase3：
用户把「上下文」填得比模型真实窗口大时，**第一个撞墙的是 phase1 的诊断请求** ——
它先用同一批章节（批次在 phase1 就定好了）。若只在 phase3 做拆批，
phase1 会先 400 直接终止整轮，用户根本走不到有保护的那一段。
"""

import logging
from routes.events import sse_emit
from core.budget import MAX_CHAPTERS_PER_BATCH

logger = logging.getLogger("textforge")

_REASON_LABEL = {
    "overflow": "上下文超限",
    "contract": "分隔符契约失败",
    "adaptive": "预拆（沿用已失败批次的规模）",
}


def pack_groups(groups, body_token_limit, max_chapters=MAX_CHAPTERS_PER_BATCH):
    """把「不可拆分组」按 token 上限贪心装批，返回 [[chapter_id, ...], ...]。

    groups 元素是 (label, chapter_ids, group_tokens)。这是 phase1 划分批次的**唯一**
    装批算法：拆批降级（split_batch_in_progress）与开跑前预估（core.estimate）都复用
    它，避免预估自己写一套、真跑的批数与它对不上。
    超过单批上限的组**只告警不报错**：上限由用户填的 context_window 推导，未必等于
    模型真实窗口；硬报错会把本来能跑完的配置判死，放行最坏由上下文超限自动拆批兜底。
    """
    batches = []
    current: list = []
    current_tokens = 0
    for label, group_ids, group_tokens in groups:
        if group_tokens > body_token_limit:
            logger.warning(
                f"[batch] 章节组 {label} 共约 {group_tokens} token，超过单批上限 "
                f"{int(body_token_limit)} token（章：{group_ids}）。该组因虚拟章保护不会被"
                f"拆分；若服务商返回上下文超限，会自动对半拆批重试。")
        if current and (current_tokens + group_tokens > body_token_limit
                        or len(current) + len(group_ids) > max_chapters):
            batches.append(current)
            current = list(group_ids)
            current_tokens = group_tokens
        else:
            current.extend(group_ids)
            current_tokens += group_tokens
    if current:
        batches.append(current)
    return batches


def chapter_groups(chapters, chapter_ids):
    """把一批章 id 按「不可拆分组」聚合：同一 original_id 的虚拟章必须留在一起。

    返回 [[cid, ...], ...]。拆分批次只能在组边界下刀 —— 否则会把一个原始章的
    多个虚拟段分到两个批次，缝合阶段就还原不回整章。
    """
    by_id = {c['id']: c for c in chapters}
    groups = []
    for cid in chapter_ids:
        ch = by_id.get(cid) or {}
        key = ch.get('original_id') if ch.get('is_virtual') else cid
        if groups and groups[-1][0] == key:
            groups[-1][1].append(cid)
        else:
            groups.append((key, [cid]))
    return [g for _, g in groups]


def split_batch_in_progress(progress, batch, *, reason, detail="", run_id=None,
                            status="phase1_done"):
    """把 progress 里某个批次原地对半拆成两个。

    返回两个新批次 dict（已插入 progress['batches']），或 None 表示**拆不动**
    （只剩一个不可拆分组），调用方据此决定报错还是继续。**不加锁**：由调用方
    在 state.progress_lock 内写盘。

    同时维护 progress['adaptive_max_chapters']：记住「多大就撑爆/出错」，
    后续批次据此预先降规模，避免每批都先失败一次（本地模型失败一次可能就是几分钟）。
    """
    groups = chapter_groups(progress.get('chapters', []), batch['chapter_ids'])
    if len(groups) < 2:
        return None
    mid = len(groups) // 2
    left = [cid for g in groups[:mid] for cid in g]
    right = [cid for g in groups[mid:] for cid in g]
    next_id = max((b.get('batch_id', 0) for b in progress['batches']), default=0) + 1
    new_batches = [
        {"batch_id": next_id, "chapter_ids": left, "status": status, "output_files": []},
        {"batch_id": next_id + 1, "chapter_ids": right, "status": status, "output_files": []},
    ]
    cap = max(len(left), len(right))
    prev = progress.get('adaptive_max_chapters')
    progress['adaptive_max_chapters'] = min(prev, cap) if prev else cap

    label = _REASON_LABEL.get(reason, reason)
    idx = progress['batches'].index(batch)
    progress['batches'][idx:idx + 1] = new_batches
    logger.warning(
        f"[batch] 批次 {batch['batch_id']} {label}，对半拆分为 "
        f"{new_batches[0]['batch_id']}({len(left)}章) + {new_batches[1]['batch_id']}({len(right)}章)"
        f"{('：' + detail) if detail else ''}")
    sse_emit("batch_split", {
        "reason": reason, "from_batch": batch['batch_id'],
        "new_batches": [b['batch_id'] for b in new_batches],
        "chapter_ids": [left, right], "detail": detail,
        "message": f"批次 {batch['batch_id']} 因「{label}」自动对半拆分重跑"
                   + (f"（{detail}）" if detail else ""),
    }, run_id)
    return new_batches
