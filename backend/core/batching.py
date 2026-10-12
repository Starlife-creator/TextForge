"""批次拆分：上下文超限 / 契约失败时的降级机制。

为什么必须共享给 phase1 和 phase3：
用户把「上下文」填得比模型真实窗口大时，**第一个撞墙的是 phase1 的诊断请求** ——
它先用同一批章节（批次在 phase1 就定好了）。若只在 phase3 做拆批，
phase1 会先 400 直接终止整轮，用户根本走不到有保护的那一段。
"""

import logging
from routes.events import sse_emit

logger = logging.getLogger("textforge")

_REASON_LABEL = {
    "overflow": "上下文超限",
    "contract": "分隔符契约失败",
    "adaptive": "预拆（沿用已失败批次的规模）",
}


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
