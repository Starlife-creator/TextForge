import re, hashlib, unicodedata, logging
from pathlib import Path
from utils.atomic import atomic_write_text, atomic_write_json

logger = logging.getLogger("textforge")

def sha256_of(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode('utf-8')).hexdigest()

def parse_stitch_result(text: str) -> tuple[str, str]:
    m = re.search(
        r'={2,}\s*PREV_END\s*={2,}\s*([\s\S]*?)\s*={2,}\s*NEXT_START\s*={2,}\s*([\s\S]*)',
        text,
    )
    if not m: raise ValueError("缝合输出缺少标记")
    return m.group(1).strip(), m.group(2).strip()

def build_stitch_prompt(prev_seg, next_seg, blueprint, author_style, stitch_output_limit):
    return f"""你是一位极其精细的文字缝合师。你拥有全局的剧情视野，并且需要修补两段重构稿之间的断层。
用户会提供两段已经重构好的正文（前文结尾 + 后文开头）。这两段之间因为没有前情提要，可能存在严重的情绪跳跃、场景突兀或逻辑断裂。请你充分利用全局设定，修复这个交界处。

要求：
1. 只重写交界处的过渡段落，不需要重写整章。
2. 必须参考【全局设定】，保证剧情逻辑、人物语气、情感基调的平滑过渡。
3. 严格保留原文的 Markdown 格式标记。
4. 直接输出修改后的【前文结尾修改段】和【后文开头修改段】。
5. 输出总字数不超过 {stitch_output_limit} 字。
6. 输出的内容必须严格使用以下机器可读标记包裹：
===PREV_END===
（重写后的前文结尾段）
===NEXT_START===
（重写后的后文开头段）

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
        text = file_path.read_text(encoding='utf-8', newline='')
        text = unicodedata.normalize('NFC', text)

        sorted_patches = sorted(patches, key=lambda s: s['start'], reverse=True)
        for i in range(len(sorted_patches) - 1):
            curr = sorted_patches[i]
            next_ = sorted_patches[i + 1]
            if next_['end'] > curr['start']:
                raise ValueError(f"缝合补丁重叠: {next_} 与 {curr}")

        for s in sorted_patches:
            current_segment = text[s['start']:s['end']]
            if sha256_of(current_segment) == s['before_hash']:
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

async def _stitch_common(prev_file_rel, next_file_rel, output_path, payload_base, client, sse_emit_fn, run_id, chars):
    """返回两个补丁（前文结尾 + 后文开头）。"""
    from core.api_client import call_with_retry
    prev_file = output_path / prev_file_rel
    next_file = output_path / next_file_rel
    prev_text = unicodedata.normalize('NFC', prev_file.read_text(encoding='utf-8', newline=''))
    next_text = unicodedata.normalize('NFC', next_file.read_text(encoding='utf-8', newline=''))
    
    stitch_extract_limit = int(chars * 0.075)
    prev_seg = prev_text[-stitch_extract_limit:]
    next_seg = next_text[:stitch_extract_limit]
    stitch_output_limit = int(stitch_extract_limit * 1.2)
    
    prompt = build_stitch_prompt(prev_seg, next_seg, payload_base['blueprint'], payload_base['author_style'], stitch_output_limit)
    payload = {**payload_base['payload'], "messages": [{"role": "user", "content": prompt}], "max_tokens": stitch_output_limit * 2}
    result, _, _ = await call_with_retry(client, payload_base['api_url'], payload, sse_emit_fn, run_id)
    prev_repl, next_repl = parse_stitch_result(result)
    
    return [
        {
            "file": prev_file_rel,
            "start": max(0, len(prev_text) - len(prev_seg)),
            "end": len(prev_text),
            "original": prev_seg, "replacement": prev_repl,
            "before_hash": sha256_of(prev_seg), "applied": False,
        },
        {
            "file": next_file_rel,
            "start": 0,
            "end": len(next_seg),
            "original": next_seg, "replacement": next_repl,
            "before_hash": sha256_of(next_seg), "applied": False,
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

async def apply_virtual_stitch(prev_v, next_v, client, payload_base, sse_emit_fn, run_id, chars):
    patches = await _stitch_common(prev_v['output_file'], next_v['output_file'],
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
            head = unicodedata.normalize('NFC', next_file.read_text(encoding='utf-8', newline=''))[:400]
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
    merged = "\n\n".join((output_path / v['output_file']).read_text(encoding='utf-8', newline='') for v in virtuals_sorted)
    original_id = virtuals_sorted[0]['original_id']
    merged_file = output_path / "02_workspace/reconstructed" / f"chapter_{original_id}.txt"
    atomic_write_text(merged_file, merged)
    for ch in progress['chapters']:
        if ch['id'] == original_id:
            ch['hash'] = sha256_of(merged)
            break
    rel = str(merged_file.relative_to(output_path)).replace('\\', '/')
    logger.info(f"[stitch] 虚拟章合并完成 {original_id} -> {rel}")
    return rel

async def stitch_pipeline(output_path, progress, client, payload_base, sse_emit_fn, run_id, chars):
    from routes import state
    completed_batches = [b for b in progress['batches'] if b['status'] == 'phase3_done']
    virtual_done = progress.setdefault('stitch_virtual_done', [])
    batch_done = progress.setdefault('stitch_batch_done', [])

    # v8.8 修正：阶段 1 处理【所有】带虚拟章的 original_id（不论是否跨批次）
    # 幂等：merge 完成的 original_id 记入 stitch_virtual_done，恢复时整体跳过
    original_ids = sorted({v['original_id'] for v in progress['virtual_chapter_map']})
    todo_originals = [oid for oid in original_ids if oid not in virtual_done]
    sse_emit("stitch_start", {"stage": 1, "count": len(original_ids),
        "skipped": len(original_ids) - len(todo_originals)}, run_id)
    for original_id in todo_originals:
        virtuals = [v for v in progress['virtual_chapter_map'] if v['original_id'] == original_id]
        virtuals_sorted = sorted(virtuals, key=lambda v: v['order'])
        for i in range(len(virtuals_sorted) - 1):
            if state.stop_requested: raise asyncio.CancelledError()
            patches = await apply_virtual_stitch(virtuals_sorted[i], virtuals_sorted[i + 1],
                client, payload_base, sse_emit_fn, run_id, chars)
            progress['stitch_anchors'].extend(patches)
            patches_by_file = {}
            for p in patches: patches_by_file.setdefault(p['file'], []).append(p)
            apply_stitches(output_path, patches_by_file)
        # 所有虚拟章组都 merge 成父章文件（merge 本身幂等：总是用当前虚拟章内容覆盖父章）
        merge_virtual_chapters(virtuals_sorted, output_path, progress)
        virtual_done.append(original_id)
        async with state.progress_lock:
            atomic_write_json(output_path / "progress.json", progress)
    sse_emit("stitch_done", {"stage": 1}, run_id)
    
    # v8.8 修正：阶段 2 批次间缝合，不再跳过任何边界
    # 幂等：已完成的边界（"prevBid-nextBid"）记入 stitch_batch_done，恢复时跳过
    total_pairs = max(0, len(completed_batches) - 1)
    if total_pairs > 0:
        sse_emit("stitch_start", {"stage": 2, "count": total_pairs}, run_id)
    for i in range(total_pairs):
        if state.stop_requested: raise asyncio.CancelledError()
        pair_key = f"{completed_batches[i]['batch_id']}-{completed_batches[i + 1]['batch_id']}"
        if pair_key in batch_done:
            logger.info(f"[stitch] 批次边界 {pair_key} 已完成，恢复跳过")
            continue
        # v8.8：去掉 is_cross_batch_pair 跳过——虚拟章边界必须缝合
        patches = await apply_batch_stitch(completed_batches[i], completed_batches[i + 1],
            client, payload_base, sse_emit_fn, run_id, chars)
        progress['stitch_anchors'].extend(patches)
        patches_by_file = {}
        for p in patches: patches_by_file.setdefault(p['file'], []).append(p)
        apply_stitches(output_path, patches_by_file)
        batch_done.append(pair_key)
        async with state.progress_lock:
            atomic_write_json(output_path / "progress.json", progress)
    if total_pairs > 0:
        sse_emit("stitch_done", {"stage": 2}, run_id)
