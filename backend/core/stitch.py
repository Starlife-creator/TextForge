import asyncio, json, re, hashlib, unicodedata, logging
from pathlib import Path
from utils.atomic import atomic_write_text, atomic_write_json
from core.budget import stitch_window_chars
from core.quality import change_rate

logger = logging.getLogger("textforge")

# STITCH_MAX_WINDOW_CHARS / STITCH_MAX_CHANGE_RATE 的口径说明见 core/budget.py
# 与本文件 _stitch_common 里的闸门注释；数值只在一处定义，预估与真跑共用。

def sha256_of(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode('utf-8')).hexdigest()

def parse_stitch_result(text: str) -> tuple[str, str]:
    m = re.search(
        r'={2,}\s*PREV_END\s*={2,}\s*([\s\S]*?)\s*={2,}\s*NEXT_START\s*={2,}\s*([\s\S]*)',
        text,
    )
    if not m: raise ValueError("缝合输出缺少标记")
    return m.group(1).strip(), m.group(2).strip()


MIN_REPLACEMENT_RATIO = 0.5
MAX_REPLACEMENT_RATIO = 3.0

# 「只补过渡」闸门：原文句子被保留的比例低于这个数就拒绝该补丁。
# 0.35 是**设计取值而非实测值**（真实分布需要拿真模型跑一轮才有）；取偏松的一侧，
# 因为拒绝的代价只是保留原文交界 —— 与既有的缝合降级完全同一条路径。
STITCH_MAX_CHANGE_RATE = 0.35


def check_replacement_length(original: str, replacement: str) -> None:
    """防止模型只返回几句过渡就覆盖掉整段正文（会造成静默截断）。

    提示词要求「完整返回两整段」，所以替换段不应远短于原文。短原文不设限，
    避免对本来就只有一两句的章尾/章首误判。
    """
    if not replacement.strip():
        raise ValueError("缝合输出中的替换段为空")
    if len(original) >= 200:
        ratio = len(replacement) / len(original)
        if ratio < MIN_REPLACEMENT_RATIO:
            raise ValueError(
                f"缝合替换段过短：原文 {len(original)} 字，仅返回 {len(replacement)} 字"
                f"（{ratio:.0%}），可能只返回了过渡句")
        if ratio > MAX_REPLACEMENT_RATIO:
            raise ValueError(
                f"缝合替换段异常膨胀：原文 {len(original)} 字，返回 {len(replacement)} 字"
                f"（{ratio:.0%}）")

def build_stitch_prompt(prev_seg, next_seg, blueprint, author_style, stitch_output_limit):
    return f"""你是一位极其精细的文字缝合师。你拥有全局的剧情视野，并且需要修补两段重构稿之间的断层。
用户会提供两段已经重构好的正文（前文结尾 + 后文开头）。这两段之间因为没有前情提要，可能存在严重的情绪跳跃、场景突兀或逻辑断裂。请你充分利用全局设定，修复这个交界处。

要求：
1. 只改写交界处需要过渡的句子；除过渡句外，其余原文**原样保留**，不要重写整章。
2. 必须参考【全局设定】，保证剧情逻辑、人物语气、情感基调的平滑过渡。
3. 严格保留原文的 Markdown 格式标记。
4. 必须**完整**输出改写后的【前文结尾】整段与【后文开头】整段，不能只输出新增的过渡句，
   也不能省略或概括原文内容。
5. 输出总字数不超过 {stitch_output_limit} 字。
6. 输出的内容必须严格使用以下机器可读标记包裹：
===PREV_END===
（完整的前文结尾段，含原有过半内容）
===NEXT_START===
（完整的后文开头段，含原有过半内容）

【全局设定（必看）】
{blueprint}

【作者特殊风格要求】
{author_style}

【前文结尾】
{prev_seg}

【后文开头】
{next_seg}
"""

def _segment_already_applied(text: str, patch: dict) -> bool:
    """v8.8 恢复辅助：崩溃可能发生在补丁已应用但游标未持久化时。
    通过判断目标位置当前内容是否已是 replacement，识别"已应用"状态，避免哈希不匹配直接失败。"""
    replacement = s_patch_replacement(patch)
    if patch['end'] >= len(text) or len(text) - patch['start'] < len(replacement):
        return False
    return text[patch['start']:patch['start'] + len(replacement)] == replacement

def s_patch_replacement(patch: dict) -> str:
    return patch['replacement']

def apply_stitches(output_path: Path, stitches_by_file: dict) -> None:
    """v8.8 幂等版：哈希不匹配时先判断补丁是否已应用（恢复场景），已应用则跳过，否则才报错。"""
    for file_rel, patches in stitches_by_file.items():
        file_path = output_path / file_rel
        text = file_path.read_text(encoding='utf-8')
        text = unicodedata.normalize('NFC', text)

        sorted_patches = sorted(patches, key=lambda s: s['start'], reverse=True)
        for i in range(len(sorted_patches) - 1):
            curr = sorted_patches[i]
            next_ = sorted_patches[i + 1]
            if next_['end'] > curr['start']:
                raise ValueError(f"缝合补丁重叠: {next_} 与 {curr}")

        for s in sorted_patches:
            current_segment = text[s['start']:s['end']]
            # 跨版本续跑：旧 progress 的 before_hash 可能是在 CRLF 文本上算的，
            # 而 read_text(universal newlines) 已把 CRLF 折成 LF。双向归一：
            # 原样、LF 版、CRLF 版各算一次哈希，任一匹配即应用；都不匹配才走
            # 「已应用」兜底或抛错——避免一次行尾差异就整轮缝合失败。
            seg_lf = current_segment.replace("\r\n", "\n").replace("\r", "\n")
            seg_crlf = seg_lf.replace("\n", "\r\n")
            seg_hash = sha256_of(current_segment)
            if (seg_hash == s['before_hash']
                    or sha256_of(seg_lf) == s['before_hash']
                    or sha256_of(seg_crlf) == s['before_hash']):
                text = text[:s['start']] + s['replacement'] + text[s['end']:]
                s['applied'] = True
            elif _segment_already_applied(text, s):
                # 崩溃恢复：补丁此前已写入，仅补标记
                logger.info(f"[stitch] 检测到补丁已应用（恢复），跳过：{file_rel} [{s['start']}:{s['end']}]")
                s['applied'] = True
            else:
                raise ValueError(f"缝合补丁哈希不匹配且内容非已替换状态: {s}")

        atomic_write_text(file_path, text)
        logger.info(f"[stitch] 已应用补丁到 {file_rel}（{len(patches)} 个补丁）")

def _stitch_retry_hook(payload: dict, exc: Exception) -> None:
    """把「标记缺失/替换段过短/整段重写」的错因写回下一次请求，而不是原样重发碰运气。"""
    msg = str(exc)
    if "改动过大" in msg:
        # 长度比 0.5~3.0 已经挡不住整段重写（重写整章的长度是合规的），
        # 所以纠正提示必须换成「照抄」口径，否则重试大概率还是同一份重写稿。
        correction = (
            "请重新输出：把【前文结尾】与【后文开头】里的原句**逐字照抄**，"
            "只在两段的交界处新增一两句过渡，不要替换或改写任何原有句子；"
            "并严格用 ===PREV_END=== 与 ===NEXT_START=== 包裹。"
        )
    else:
        correction = (
            "请重新输出：必须**完整**返回【前文结尾】与【后文开头】两整段（不是只返回过渡句），"
            "并严格用 ===PREV_END=== 与 ===NEXT_START=== 包裹；除新增过渡句外，其余文字原样保留。"
        )
    payload.setdefault("messages", []).append({
        "role": "user",
        "content": f"你上一次的缝合输出不合格：{msg[:200]}\n{correction}",
    })
    if "temperature" in payload:
        payload["temperature"] = max(0.0, min(payload["temperature"], 0.4))
    logger.info(f"[stitch] 注入契约纠正提示：{str(exc)[:120]}")


async def _stitch_common(prev_file_rel, next_file_rel, output_path, payload_base, client, sse_emit_fn, run_id, chars):
    """返回两个补丁（前文结尾 + 后文开头）。"""
    from core.api_client import call_with_retry, RetryableBusinessError
    prev_file = output_path / prev_file_rel
    next_file = output_path / next_file_rel
    prev_text = unicodedata.normalize('NFC', prev_file.read_text(encoding='utf-8'))
    next_text = unicodedata.normalize('NFC', next_file.read_text(encoding='utf-8'))
    
    stitch_extract_limit = stitch_window_chars(chars)
    prev_seg = prev_text[-stitch_extract_limit:]
    next_seg = next_text[:stitch_extract_limit]
    stitch_output_limit = int(stitch_extract_limit * 1.2)
    # 闸门按改动率拒绝整段重写。关闭时（旧行为）只受长度比 0.5~3.0 约束。
    minimal_change = bool(payload_base.get("minimal_change"))

    def _rate_guard(seg: str, repl: str, label: str) -> None:
        if not minimal_change:
            return
        rate = change_rate(seg, repl)
        # rate 为 None = 窗口里不足 3 句（章尾只有一两句话的短边界）：改动率没有分辨力，
        # 与长度护栏一样放行，宁漏不误杀。
        if rate is not None and rate > STITCH_MAX_CHANGE_RATE:
            raise ValueError(
                f"{label}改动过大：原文句子有 {rate:.0%} 没有被原样保留"
                f"（闸门上限 {STITCH_MAX_CHANGE_RATE:.0%}）。只允许在交界处新增/微调过渡句，"
                f"其余原文请一字不动地照抄。")
    
    prompt = build_stitch_prompt(prev_seg, next_seg, payload_base['blueprint'], payload_base['author_style'], stitch_output_limit)
    payload = {**payload_base['payload'], "messages": [{"role": "user", "content": prompt}], "max_tokens": stitch_output_limit * 2}
    parsed: dict = {}

    def _validate(text: str) -> None:
        # 契约校验必须放在 call_with_retry 的 validate 里，否则异常进不了重试分支
        # （与重构阶段历史上「分隔符数量不符从不重试」是同一个坑）。
        try:
            prev_repl, next_repl = parse_stitch_result(text)
            check_replacement_length(prev_seg, prev_repl)
            check_replacement_length(next_seg, next_repl)
            _rate_guard(prev_seg, prev_repl, "前文结尾")
            _rate_guard(next_seg, next_repl, "后文开头")
        except ValueError as e:
            raise RetryableBusinessError(str(e)) from e
        parsed["prev"], parsed["next"] = prev_repl, next_repl

    _, usage, _ = await call_with_retry(
        client, payload_base['api_url'], payload, sse_emit_fn, run_id,
        on_retry=_stitch_retry_hook, validate=_validate)
    from core.splitter import _accum_usage
    _accum_usage(payload_base.get('progress') or {}, usage)
    prev_repl, next_repl = parsed["prev"], parsed["next"]
    
    return [
        {
            "file": prev_file_rel,
            "start": max(0, len(prev_text) - len(prev_seg)),
            "end": len(prev_text),
            "original": prev_seg, "replacement": prev_repl,
            "before_hash": sha256_of(prev_seg), "applied": False,
            "change_rate": change_rate(prev_seg, prev_repl),
        },
        {
            "file": next_file_rel,
            "start": 0,
            "end": len(next_seg),
            "original": next_seg, "replacement": next_repl,
            "before_hash": sha256_of(next_seg), "applied": False,
            "change_rate": change_rate(next_seg, next_repl),
        },
    ]

def _chapter_file_for_stitch(chapter_id: str, progress: dict) -> str:
    """v8.8 新增：若章节是虚拟章，返回其父章文件；否则返回自身文件。
    保证批次间缝合对象是最终会被输出的父章文件，避免改到被 merge 前的旧虚拟章文件。"""
    for ch in progress.get('chapters', []):
        if ch['id'] == chapter_id:
            if ch.get('is_virtual'):
                return f"02_workspace/reconstructed/chapter_{ch['original_id']}.txt"
            return f"02_workspace/reconstructed/chapter_{chapter_id}.txt"
    return f"02_workspace/reconstructed/chapter_{chapter_id}.txt"

def _virtual_output_rel(v: dict) -> str:
    """虚拟章重构稿的相对路径。

    原实现在 _refactor_and_split 里把 output_file 写进 progress，但从没被重构过的
    虚拟章（比如 progress.json 来自批次划分之前的旧版本）就没有这个键，
    缝合阶段会以一个裸 KeyError 崩掉。这里直接由 virtual_id 推导，不再依赖缓存字段。
    """
    return f"02_workspace/reconstructed/chapter_{v['virtual_id']}.txt"

async def apply_virtual_stitch(prev_v, next_v, client, payload_base, sse_emit_fn, run_id, chars):
    patches = await _stitch_common(_virtual_output_rel(prev_v), _virtual_output_rel(next_v),
        Path(payload_base['output_path']), payload_base, client, sse_emit_fn, run_id, chars)
    for p in patches:
        p["source"] = "virtual_stitch"
        p["virtual_id"] = prev_v.get('virtual_id')
        p["batch_id"] = prev_v.get('batch_id')
    return patches

async def apply_batch_stitch(prev_batch, next_batch, client, payload_base, sse_emit_fn, run_id, chars):
    """v8.8 修正：prev 末章 / next 首章，虚拟章映射到父章文件。
    skip_stitch_if_smooth 门：若后一文件开头是硬分章断点（章节标题/分隔线），
    视为已自然衔接，跳过该处缝合调用。"""
    progress = payload_base.get('progress') or {}
    prev_last = prev_batch['chapter_ids'][-1]
    next_first = next_batch['chapter_ids'][0]
    prev_file_rel = _chapter_file_for_stitch(prev_last, progress)
    next_file_rel = _chapter_file_for_stitch(next_first, progress)
    if payload_base.get('skip_smooth'):
        next_file = Path(payload_base['output_path']) / next_file_rel
        if next_file.exists():
            head = unicodedata.normalize('NFC', next_file.read_text(encoding='utf-8'))[:400]
            if _is_hard_break(head):
                logger.info(f"[stitch] skip_smooth：边界 {prev_batch['batch_id']}->{next_batch['batch_id']} 为硬分章断点，跳过缝合")
                return []
    patches = await _stitch_common(prev_file_rel, next_file_rel,
        Path(payload_base['output_path']), payload_base, client, sse_emit_fn, run_id, chars)
    for p in patches:
        p["source"] = "batch_stitch"
        p["batch_id"] = prev_batch['batch_id']
    return patches

def _is_hard_break(text: str) -> bool:
    """判断是否硬分章断点：首个非空行为 Markdown 标题 / 中英章节标题 / 分隔线 / 编号标题。"""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line in ("---", "***", "___"):
            return True
        return bool(
            re.match(r'^#{1,6}\s+', line)
            or re.match(r'^第[0-9零一二三四五六七八九十百千万两]+[章节回卷部篇]', line)
            or re.match(r'^chapter\s*\d+', line, re.IGNORECASE)
            or re.match(r'^chapter[\s#:.]+', line, re.IGNORECASE)
            or re.match(r'^\d{1,3}[.、．]\s*\S', line)
        )
    return False

def merge_virtual_chapters(virtuals_sorted, output_path, progress):
    """v8.8 修正：用 \n\n 拼接避免粘连；合并后更新父章 hash。"""
    merged = "\n\n".join((output_path / _virtual_output_rel(v)).read_text(encoding='utf-8') for v in virtuals_sorted)
    original_id = virtuals_sorted[0]['original_id']
    merged_file = output_path / "02_workspace/reconstructed" / f"chapter_{original_id}.txt"
    atomic_write_text(merged_file, merged)
    for ch in progress.get('chapters', []):
        if ch['id'] == original_id:
            ch['hash'] = sha256_of(merged)
            break
    rel = str(merged_file.relative_to(output_path)).replace('\\', '/')
    logger.info(f"[stitch] 虚拟章合并完成 {original_id} -> {rel}")
    return rel

def _anchor_key(p: dict) -> tuple:
    """缝合锚点的唯一标识：文件 + 原文哈希。

    使用 before_hash 而非原文片段，因为 progress.json 仅保存轻量引用（含 before_hash，
    不含 original/replacement 全文），且哈希更稳定、防碰撞。
    """
    return (p.get("file"), p.get("before_hash"))

def _record_anchors(progress: dict, patches: list, output_path: Path | None = None) -> None:
    """记录缝合锚点，按指纹去重。

    锚点完整内容（原文/替换文）写入单独的 JSONL 文件（追加模式），
    progress.json 仅保留轻量引用（文件、范围、哈希、是否已应用、来源），
    避免 progress.json 膨胀到数十 MB 导致每轮写盘放大。

    output_path 为 None 时仅更新进度内存（测试/回归兼容），不落盘 JSONL。
    """
    # 1) 进度里只存轻量引用，用于去重；同时收集本次新增的完整补丁。
    # 旧实现分两轮循环，第二轮判断的 known 已在第一轮被补齐，导致 new_keys
    # 永远为空 —— JSONL 从未写入过新锚点，报告只能退回不含原文/替换文的引用。
    anchors = progress.setdefault('stitch_anchors', [])
    known = {_anchor_key(a) for a in anchors}
    new_patches = []
    for p in patches:
        k = _anchor_key(p)
        if k in known:
            continue
        known.add(k)
        new_patches.append(p)
        # 轻量引用：不含 original/replacement 全文，但要带改动率与覆盖字数 ——
        # 质量报告只读 progress.json，靠这两个字段汇总缝合的实际改写面积。
        anchors.append({
            "file": p.get("file"),
            "start": p.get("start"),
            "end": p.get("end"),
            "chars": int(p.get("end") or 0) - int(p.get("start") or 0),
            "before_hash": p.get("before_hash"),
            "change_rate": p.get("change_rate"),
            "applied": p.get("applied", False),
            "source": p.get("source", "unknown"),
        })

    # 2) 完整锚点追加到 JSONL（仅去重后的新锚点）
    if output_path is None or not new_patches:
        return
    anchors_path = output_path / "_diagnostics" / "stitch_anchors.jsonl"
    anchors_path.parent.mkdir(parents=True, exist_ok=True)
    with open(anchors_path, "a", encoding="utf-8") as f:
        for p in new_patches:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")

def _record_stitch_failure(progress, stage, boundary, exc, sse_emit_fn=None, run_id=None):
    """单处缝合失败不拖垮整轮：记录后跳过该边界，正文仍照常产出。

    缝合只是交界处的润色；因为一次标记契约失败就丢掉整本已重构好的稿子，
    代价远大于「保留原文交界」。失败会进入进度与最终质量报告，不会被静默吞掉。
    """
    failures = progress.setdefault("stitch_failures", [])
    if len(failures) >= 100:
        return
    failures.append({
        "stage": stage,
        "boundary": boundary,
        "error": f"{type(exc).__name__}: {str(exc)[:300]}",
    })
    logger.warning(f"[stitch] 边界 {boundary} 缝合失败，跳过并保留原文：{exc}")
    if len(failures) == 1 and sse_emit_fn:
        sse_emit_fn("error", {
            "kind": "warning",
            "code": "stitch_skipped",
            "message": f"缝合失败，已跳过边界 {boundary}（未应用的补丁保持原文）；成品会继续生成，详情见质量报告",
        }, run_id)


async def stitch_pipeline(output_path, progress, client, payload_base, sse_emit_fn, run_id, chars):
    from routes import state
    completed_batches = [b for b in progress['batches'] if b['status'] == 'phase3_done']
    virtual_done = progress.setdefault('stitch_virtual_done', [])
    batch_done = progress.setdefault('stitch_batch_done', [])

    # v8.8 修正：阶段 1 处理【所有】带虚拟章的 original_id（不论是否跨批次）
    # 幂等：merge 完成的 original_id 记入 stitch_virtual_done，恢复时整体跳过
    original_ids = sorted({v['original_id'] for v in progress.setdefault('virtual_chapter_map', [])})
    todo_originals = [oid for oid in original_ids if oid not in virtual_done]
    sse_emit_fn("stitch_start", {"stage": 1, "count": len(original_ids),
        "skipped": len(original_ids) - len(todo_originals)}, run_id)
    for original_id in todo_originals:
        virtuals = [v for v in progress['virtual_chapter_map'] if v['original_id'] == original_id]
        virtuals_sorted = sorted(virtuals, key=lambda v: v['order'])
        for i in range(len(virtuals_sorted) - 1):
            if state.stop_requested: raise asyncio.CancelledError()
            boundary = f"virtual:{virtuals_sorted[i].get('virtual_id')}->{virtuals_sorted[i + 1].get('virtual_id')}"
            patches = []
            try:
                patches = await apply_virtual_stitch(virtuals_sorted[i], virtuals_sorted[i + 1],
                    client, payload_base, sse_emit_fn, run_id, chars)
                patches_by_file = {}
                for p in patches: patches_by_file.setdefault(p['file'], []).append(p)
                apply_stitches(output_path, patches_by_file)
                # 记录必须发生在应用**之后**：applied 标志是 apply_stitches 就地改写的，
                # 先记后应用会让 progress 与缝合报告把所有补丁都写成「未应用」，
                # 也让按 applied 汇总的改动率永远为空。
                _record_anchors(progress, patches, output_path)
            except Exception as e:
                # 失败分支也记录：此时 patches 可能是「已生成但未应用」或「部分应用」，
                # 两者都是成品的真实状态，丢掉就等于改过正文却毫无线索。
                _record_anchors(progress, patches, output_path)
                _record_stitch_failure(progress, 1, boundary, e, sse_emit_fn, run_id)
        # 所有虚拟章组都 merge 成父章文件（merge 本身幂等：总是用当前虚拟章内容覆盖父章）
        merge_virtual_chapters(virtuals_sorted, output_path, progress)
        virtual_done.append(original_id)
        async with state.progress_lock:
            atomic_write_json(output_path / "progress.json", progress)
    sse_emit_fn("stitch_done", {"stage": 1}, run_id)
    
    # v8.8 修正：阶段 2 批次间缝合，不再跳过任何边界
    # 幂等：已完成的边界（"prevBid-nextBid"）记入 stitch_batch_done，恢复时跳过
    total_pairs = max(0, len(completed_batches) - 1)
    if total_pairs > 0:
        sse_emit_fn("stitch_start", {"stage": 2, "count": total_pairs}, run_id)
    for i in range(total_pairs):
        if state.stop_requested: raise asyncio.CancelledError()
        pair_key = f"{completed_batches[i]['batch_id']}-{completed_batches[i + 1]['batch_id']}"
        if pair_key in batch_done:
            logger.info(f"[stitch] 批次边界 {pair_key} 已完成，恢复跳过")
            continue
        # v8.8：去掉 is_cross_batch_pair 跳过——虚拟章边界必须缝合
        patches = []
        try:
            patches = await apply_batch_stitch(completed_batches[i], completed_batches[i + 1],
                client, payload_base, sse_emit_fn, run_id, chars)
            patches_by_file = {}
            for p in patches: patches_by_file.setdefault(p['file'], []).append(p)
            apply_stitches(output_path, patches_by_file)
            _record_anchors(progress, patches, output_path)
        except Exception as e:
            _record_anchors(progress, patches, output_path)
            _record_stitch_failure(progress, 2, pair_key, e, sse_emit_fn, run_id)
        # 失败边界也记为已完成：续跑时不应反复为一个「已降级跳过」的交界烧调用。
        batch_done.append(pair_key)
        async with state.progress_lock:
            atomic_write_json(output_path / "progress.json", progress)
    if total_pairs > 0:
        sse_emit_fn("stitch_done", {"stage": 2}, run_id)
    skipped = len(progress.get("stitch_failures") or [])
    if skipped > 1:
        sse_emit_fn("error", {
            "kind": "warning",
            "code": "stitch_skipped_summary",
            "message": f"共有 {skipped} 处缝合失败已跳过（正文已保留原样），成品仍会生成；建议查看质量报告",
        }, run_id)
