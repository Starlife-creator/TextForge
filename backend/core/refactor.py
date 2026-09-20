import asyncio, json, time, logging, html, hashlib
from pathlib import Path
from utils.atomic import atomic_write_text, atomic_write_json
from routes import state
from routes.events import sse_emit
from core.stitch import stitch_pipeline
from core.api_client import call_with_retry, RetryableBusinessError, pick_model

logger = logging.getLogger("textforge")

class ChapterBreakMissingError(RetryableBusinessError):
    """v8.8 修正：继承 RetryableBusinessError，才能进入 call_with_retry 的重试路径。"""
    pass

async def phase3_refactor_stitch(req, progress, progress_path, run_id):
    output_path = Path(req.output_path)
    recon_dir = output_path / "02_workspace/reconstructed"
    recon_dir.mkdir(parents=True, exist_ok=True)
    
    blueprint_text = (output_path / "Story_Bible.md").read_text(encoding='utf-8')
    chars = req.context_window * 1.5
    blueprint_for_stitch = blueprint_text[:int(chars * 0.10)]

    gates = getattr(req, "refactor_gates", {}) or {}
    payload_base = {
        "output_path": req.output_path,
        "api_url": req.api_url,
        "blueprint": blueprint_for_stitch,
        "author_style": req.author_style,
        "payload": {"model": pick_model(req, "stitch"), "temperature": req.temperatures.stitch, "stream": True},
        "progress": progress,      # v8.8：供 apply_batch_stitch 映射虚拟章父章
        "skip_smooth": bool(gates.get("skip_stitch_if_smooth")),
    }
    
    completed_batches = [b for b in progress['batches'] if b['status'] == 'phase1_done']
    
    from core.api_client import make_httpx_client
    async with make_httpx_client(req) as client:
        # 阶段 3-1：无重叠重构
        pending_batches = len([b for b in completed_batches if b['status'] != 'phase3_done'])
        for batch in completed_batches:
            # v8.8 修正：恢复时跳过已 phase3_done 的批次
            if batch['status'] == 'phase3_done':
                continue
            if state.stop_requested: raise asyncio.CancelledError()
            await _wait_pause(progress, progress_path)
            
            chapter_ids = batch['chapter_ids']
            output_files = await _refactor_and_split(
                client, req, output_path, recon_dir, blueprint_text, chars,
                progress, batch, run_id)
            
            async with state.progress_lock:
                atomic_write_json(progress_path, progress)
            logger.info(f"[phase3] batch {batch['batch_id']} 完成 文件={output_files}")
            sse_emit("batch_done", {"batch_id": batch['batch_id'], "output_files": output_files}, run_id)
            # 逐章验收门：开启时每个批次重构稿产出后暂停，等待用户验收再继续
            if gates.get("require_chapter_accept"):
                await _wait_batch_accept(progress, progress_path, batch, output_path, run_id)
            # 铁律8：批次间保持串行间隔，避免连续请求触发云端限流（间隔可配置，缺省 2s）
            pending_batches -= 1
            if pending_batches > 0:
                await asyncio.sleep(getattr(req, "batch_interval_sec", 2.0))
        
        # 阶段 3-2：缝合（同一个 client）
        await stitch_pipeline(output_path, progress, client, payload_base, sse_emit, run_id, chars)
    
    # P1.6 左右对照：仅开启时按章生成「原文 | 重构后」对照文档（默认关；由既有文件派生，幂等）
    if getattr(req, "compare_output", False):
        stats = _write_comparison_outputs(output_path, progress)
        logger.info(f"[comparison] 生成完成 written={stats['written']} skipped={stats['skipped']}")
        sse_emit("comparison_done", {"written": stats["written"], "skipped": stats["skipped"]}, run_id)

    # P1.7 章快照：缝合完成后快照每个逻辑章的最终稿到 snapshots/final（默认关）
    if getattr(req, "chapter_snapshots", False):
        n = _write_snapshot_finals(output_path, progress)
        logger.info(f"[snapshot] 缝合终稿快照完成 final={n}")

    # 质检拦截导出门：未通过则在 phase3_qc 等待用户决策（强制导出或停止），其后再进 phase4
    if gates.get("qc_block_export"):
        issues = _run_qc(output_path, progress)
        if issues:
            progress['qc_issues'] = issues
            progress['current_phase'] = 'phase3_qc'
            async with state.progress_lock:
                atomic_write_json(progress_path, progress)
            sse_emit("qc_failed", {"issues": issues}, run_id)
            await _wait_qc_confirm(progress, progress_path, run_id)
            progress['_qc_force'] = True
            progress['current_phase'] = 'phase2'
            async with state.progress_lock:
                atomic_write_json(progress_path, progress)

    progress['current_phase'] = 'phase4'
    async with state.progress_lock:
        atomic_write_json(progress_path, progress)

async def _refactor_and_split(client, req, output_path, recon_dir, blueprint_text, chars,
                              progress, batch, run_id):
    """v8.8 新增：将 '调用 + 按章拆分' 包成一个可重试单元。
    这样 ChapterBreakMissingError 由 call_with_retry 捕获并重试（而非在返回后丢失）。"""
    chapter_ids = batch['chapter_ids']
    batch_content = "\n\n".join(
        (output_path / progress['chapters'][_find_idx(progress['chapters'], cid)]['split_file']).read_text(encoding='utf-8', newline='')
        for cid in chapter_ids
    )
    prompt = _build_refactor_prompt(
        req.refactor_mode,
        blueprint_text, req.author_style, batch_content,
        forbidden_canon=getattr(req, "forbidden_canon", []) or [],
        name_map=getattr(req, "name_map", {}) or {},
        fix_list=progress.get('fix_list') or getattr(req, "fix_list", []) or [],
        gates=getattr(req, "refactor_gates", {}) or {},
    )
    payload = {
        "model": pick_model(req, "refactor"), "messages": [{"role": "user", "content": prompt}],
        "temperature": req.temperatures.refactor,
        "max_tokens": int(len(batch_content) / 1.5),
        "stream": True,
    }
    logger.info(f"[phase3] batch {batch['batch_id']} 开始 章节={chapter_ids}")
    sse_emit("batch_start", {"batch_id": batch['batch_id'], "chapter_ids": chapter_ids}, run_id)
    
    # call_with_retry 收到 ChapterBreakMissingError 会按 RetryableBusinessError 重试 1 次
    result, usage, _ = await call_with_retry(client, req.api_url, payload, sse_emit, run_id)
    chapter_texts = _split_refactored_by_chapters(result, chapter_ids)
    
    output_files = []
    for cid, ctext in chapter_texts.items():
        rel = f"02_workspace/reconstructed/chapter_{cid}.txt"
        atomic_write_text(recon_dir / f"chapter_{cid}.txt", ctext)
        # P1.7 章快照：重构初稿镜像到 snapshots/drafts（默认关）
        if getattr(req, "chapter_snapshots", False):
            draft_dir = output_path / "02_workspace/snapshots/drafts"
            draft_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_text(draft_dir / f"chapter_{cid}.txt", ctext)
        if rel not in progress['completed_files']:
            progress['completed_files'].append(rel)
        output_files.append(rel)
    
    for v in progress['virtual_chapter_map']:
        if v['virtual_id'] in chapter_ids:
            v['output_file'] = f"02_workspace/reconstructed/chapter_{v['virtual_id']}.txt"
            v['batch_id'] = batch['batch_id']
    
    from core.splitter import _accum_usage
    _accum_usage(progress, usage)
    batch['status'] = 'phase3_done'
    batch['output_files'] = output_files
    return output_files

def _find_idx(chapters, cid):
    for i, c in enumerate(chapters):
        if c['id'] == cid: return i
    raise KeyError(cid)

def _split_refactored_by_chapters(text: str, chapter_ids: list) -> dict:
    """只按 ===CHAPTER_BREAK=== 精确分隔；数量不符时抛 ChapterBreakMissingError。"""
    if len(chapter_ids) == 1:
        return {chapter_ids[0]: text}
    parts = [p.strip() for p in text.split("===CHAPTER_BREAK===")]
    parts = [p for p in parts if p]
    if len(parts) == len(chapter_ids):
        return {cid: p for cid, p in zip(chapter_ids, parts)}
    raise ChapterBreakMissingError(
        f"重构输出缺少 ===CHAPTER_BREAK=== 分隔符：需要 {len(chapter_ids)-1} 个，"
        f"实际得到 {len(parts)-1 if parts else 0} 段（期望 {len(chapter_ids)} 段）。"
        f"请检查提示词或重试。"
    )

def _build_refactor_prompt(mode, blueprint, author_style, batch_content, *,
                           forbidden_canon=(), name_map=None, fix_list=(), gates=None):
    """按重构档位选择提示词。缺省/未知 mode 一律回退 full_rewrite（原行为）。
    档位提示词构建后，若启用了任何「保护门」(lock_names/lock_plot/inject_forbidden)，
    追加一段强制规则；全关时完全不加，旧行为不变。"""
    mode = mode or "full_rewrite"
    if mode == "fidelity":
        prompt = _prompt_fidelity(blueprint, author_style, batch_content, forbidden_canon, gates)
    elif mode == "fix_gaps":
        prompt = _prompt_fix_gaps(blueprint, author_style, batch_content, forbidden_canon, fix_list, gates)
    elif mode == "reskin":
        prompt = _prompt_reskin(blueprint, author_style, batch_content, name_map)
    else:
        prompt = _prompt_full_rewrite(blueprint, author_style, batch_content)
    gate_block = _render_gate_rules(gates, forbidden_canon)
    if gate_block:
        prompt += "\n\n【本次已启用的保护门（强制遵守）】\n" + gate_block
    return prompt


def _render_gate_rules(gates, forbidden_canon):
    """把已开启的提示词级保护门渲染为追加规则段。全关返回空字符串（不改旧行为）。"""
    gates = gates or {}
    rules = []
    if gates.get("lock_names"):
        rules.append("- 锁定人名、地名与专有名词：一律原样保留，不得改动、替换或省略（确需调整请另用 *改动说明* 标注）。")
    if gates.get("lock_plot"):
        rules.append("- 锁定剧情骨架与关键事件顺序：不得增删、重排关键情节，仅优化表述与过渡。")
    if gates.get("inject_forbidden"):
        items = [x for x in (forbidden_canon or []) if str(x).strip()]
        if items:
            rules.append("- 禁止修改以下内容（原样保留，不得替换/删除/改写）：")
            rules += [f"  * {x}" for x in items]
    return "\n".join(rules)


# 统一的格式契约：Markdown 保留、标题行保留、===CHAPTER_BREAK=== 分隔、直接输出正文。
_COMMON_TAIL = """4. 格式保留：严格保留原文的 Markdown 格式标记。保留原标题行。
5. **章节分隔**：如果本批次包含多章，请在每章之间用 `===CHAPTER_BREAK===` 单独一行分隔，便于后续处理。
6. 直接输出正文：绝对不要输出任何解释、说明或问候语。"""


def _prompt_full_rewrite(blueprint, author_style, batch_content):
    """原「全部重构」极端档：蓝图是方向不是宪法，允许推翻细节、改人名、整段重写。"""
    return f"""你是一位小说主笔。请严格按照用户提供的【全局设定与重构方向】，对【待处理正文】进行重构优化。
要求：
1. 保留剧情骨架：核心剧情、人物基础人设、世界观设定不得篡改。
2. 修复内部断层：如果本批次章节内部存在逻辑跳跃，请合理补充过渡段落。
3. 去AI味与优化文笔：用具体动作展示情绪，打破单调句式，让文字富有画面感。
{_COMMON_TAIL}
【全局设定与重构方向】
{blueprint}

【作者特殊风格要求】
{author_style}

【待处理正文】
{batch_content}
"""


def _prompt_fidelity(blueprint, author_style, batch_content, forbidden_canon, gates):
    """保真润色档：锁定人名与事件骨架，仅润色表述，逐句贴近原意。"""
    forbid = _render_forbidden(forbidden_canon, gates)
    return f"""你是一位保真度优先的小说润色师。你的任务是在【不改变任何事实的前提下】优化文字。

硬性铁律：
1. **锁定人名、地名、专有名词与事件顺序**：一律不得改动、替换为其字面，原样保留。
2. **锁定剧情骨架与逻辑走向**：不得增删情节、不得加入原文没有的关键事件。
3. {forbid}4. 只做表述润色：优化句式、去重复、提升画面感和节奏，但每句的意思贴近原文。
{_COMMON_TAIL}

【全局设定（仅作一致性参照，不推翻）】
{blueprint}

【作者特殊风格要求】
{author_style}

【待处理正文】
{batch_content}
"""


def _prompt_fix_gaps(blueprint, author_style, batch_content, forbidden_canon, fix_list, gates):
    """修断层档：锁定骨架与人名，最小改动补逻辑断层（默认修列出的断层，未列出不动）。"""
    forbid = _render_forbidden(forbidden_canon, gates)
    scope = ""
    if fix_list:
        scope = "只修复以下勾选的断层；其余内容保持原样、不要顺手改写：\n" + "\n".join(f"- {x}" for x in fix_list) + "\n\n"
    return f"""你是一位外科式修稿师。原稿主体已可信，你只负责消除指定的逻辑断层。

硬性铁律：
1. **锁定人名、地名、专有名词与事件顺序**：原样保留，不得改动。
2. **锁定剧情骨架**：不改动结构，仅在断层处做最小补充或调整。
3. {forbid}{scope}4. 修断层以「平滑合理」为度：补过渡、消矛盾、接动机，不引入新支线。
{_COMMON_TAIL}

【全局设定（仅作一致性参照，不推翻）】
{blueprint}

【作者特殊风格要求】
{author_style}

【待处理正文】
{batch_content}
"""


def _prompt_reskin(blueprint, author_style, batch_content, name_map):
    """换皮档：节拍锁定，按映射表统一替换人名/设定名。"""
    mapping = _render_name_map(name_map)
    return f"""你是一位既保留原有节拍、又把作品「换皮」的改写师。原始剧情节拍不变，仅统一更换人设/地名等外皮。

硬性铁律：
1. **保留原有剧情节拍与事件顺序**：起承转合、关键转折一一对应，不得增删主线。
2. **按映射表替换名字/设定**：任一见到的旧名一律替换为对应新名，不得遗漏或自造其它。
3. {mapping}4. 在换皮基础上顺滑文字，但不得改变人物关系与情节逻辑。
{_COMMON_TAIL}

【全局设定（用于理解框架，不推翻；实际命名以上方映射表为准）】
{blueprint}

【作者特殊风格要求】
{author_style}

【待处理正文】
{batch_content}
"""


def _render_forbidden(forbidden_canon, gates):
    """生成「禁改清单」注入段；未启用或清单为空则给占位行（保持编号连贯）。"""
    gates = gates or {}
    items = list(forbidden_canon or [])
    if not items:
        return "名词锁定：尊重原文人名与设定，非必要不修改。\n"
    lines = ["禁止修改以下内容（原样保留，不得替换/删除/改写）："]
    lines += [f"   - {x}" for x in items]
    lines.append("（如原文确需调整，请用 `*改动说明：...*` 单独标注）")
    return "\n".join(lines) + "\n"


def _render_name_map(name_map):
    """生成 reskin 的映射注入段。"""
    mapping = name_map or {}
    if not mapping:
        return "无指定映射，保留原文人名与设定。\n"
    lines = ["人物/设定映射表（旧名 → 新名），全文统一替换："]
    lines += [f"   - {old} → {new}" for old, new in mapping.items()]
    lines.append("映射表之外的名字原样保留。")
    return "\n".join(lines) + "\n"

def _iter_output_chapter_ids(progress):
    """返回最终成书的逻辑章 id：空章剔除；虚拟章组折叠到父章（original_id）；去重保序。"""
    ids = []
    seen = set()
    for ch in progress.get('chapters', []):
        if ch.get('is_empty'):
            continue
        if ch.get('is_virtual') or ch.get('is_virtual_parent'):
            out_id = ch.get('original_id') or ch.get('id')
        else:
            out_id = ch.get('id')
        if out_id and out_id not in seen:
            seen.add(out_id)
            ids.append(out_id)
    return ids


def _write_snapshot_finals(output_path, progress):
    """P1.7 缝合终稿快照：缝合完成后，把每个逻辑章的最终稿复制到
    snapshots/final/。由既有文件派生，覆盖写天然幂等。"""
    output_path = Path(output_path)
    snap_dir = output_path / "02_workspace/snapshots/final"
    snap_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for out_id in _iter_output_chapter_ids(progress):
        src = output_path / "02_workspace/reconstructed" / f"chapter_{out_id}.txt"
        if not src.exists():
            logger.warning(f"[snapshot] 缺失最终稿，跳过章 {out_id}: {src.name}")
            continue
        atomic_write_text(snap_dir / f"chapter_{out_id}.txt", src.read_text(encoding='utf-8', newline=''))
        written += 1
    return written

def _write_comparison_outputs(output_path, progress):
    """P1.6 左右对照：按「逻辑章」生成原文 | 重构后 两列表格 Markdown。
    逻辑章以父章为单位：非虚拟章用自身 id；虚拟章组折叠到其 original_id（父章）输出一次。
    纯由既有文件派生（02_workspace/split 原文 + reconstructed 重构稿），天然幂等可覆盖。"""
    output_path = Path(output_path)
    comp_dir = output_path / "00_comparison"
    comp_dir.mkdir(parents=True, exist_ok=True)
    counts = {"written": 0, "skipped": 0}
    seen = set()
    for ch in progress.get('chapters', []):
        if ch.get('is_empty'):
            continue
        if ch.get('is_virtual') or ch.get('is_virtual_parent'):
            out_id = ch.get('original_id') or ch.get('id')
        else:
            out_id = ch.get('id')
        if not out_id or out_id in seen:
            continue
        seen.add(out_id)

        original_file = output_path / "02_workspace/split" / f"{out_id}.txt"
        recon_file = output_path / "02_workspace/reconstructed" / f"chapter_{out_id}.txt"
        if not (original_file.exists() and recon_file.exists()):
            counts["skipped"] += 1
            logger.warning(f"[comparison] 缺失文件，跳过章 {out_id}: {original_file.name}/{recon_file.name}")
            continue
        original = original_file.read_text(encoding='utf-8', newline='')
        reconstructed = recon_file.read_text(encoding='utf-8', newline='')
        _write_chapter_comparison(comp_dir, out_id, original, reconstructed)
        counts["written"] += 1
    return counts


def _write_chapter_comparison(comp_dir: Path, out_id, original: str, reconstructed: str) -> str:
    """写单个章的两列表格对照文件。Markdown 单元格含多段文本时用 <pre> 保留换行与空白，
    文本做 HTML 转义避免破坏表格结构。"""
    esc = lambda s: html.escape(s, quote=False)
    rel = f"chapter_{out_id}.comparison.md"
    md = (
        f"# 章节 {out_id} 左右对照\n\n"
        f"> 左侧为原文，右侧为重构后。\n\n"
        f"<table>\n<tr><th>原文</th><th>重构后</th></tr>\n"
        f"<tr><td><pre>{esc(original)}</pre></td>\n"
        f"<td><pre>{esc(reconstructed)}</pre></td></tr>\n"
        f"</table>\n"
    )
    atomic_write_text(comp_dir / rel, md)
    return rel

async def _wait_batch_accept(progress, progress_path, batch, output_path, run_id):
    """逐章验收门：批次重构稿产出后暂停，推送该批章节预览等待用户验收，确认后继续下一批。
    未验收前 current_phase='phase3_accept'，恢复时该批仍 pending 会重新推送，幂等。"""
    output_path = Path(output_path)
    chapters = []
    for cid in batch['chapter_ids']:
        recon_file = output_path / "02_workspace/reconstructed" / f"chapter_{cid}.txt"
        preview = ""
        try:
            preview = recon_file.read_text(encoding='utf-8', newline='')[:80]
        except OSError:
            preview = ""
        chapters.append({"id": cid, "preview": preview})
    progress['current_phase'] = 'phase3_accept'
    progress['accept_pending_batch'] = batch['batch_id']
    async with state.progress_lock:
        atomic_write_json(progress_path, progress)
    sse_emit("accept_ready", {"batch_id": batch['batch_id'], "chapters": chapters,
        "accepted": progress.get('accepted_batches', [])}, run_id)
    logger.info(f"[accept] 批次 {batch['batch_id']} 待验收（{len(chapters)} 章）")
    state.accept_confirm.clear()
    while not state.accept_confirm.is_set():
        if state.stop_requested: raise asyncio.CancelledError()
        progress["last_heartbeat"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        async with state.progress_lock:
            atomic_write_json(progress_path, progress)
        await asyncio.sleep(1.0)
    progress['current_phase'] = 'phase2'
    async with state.progress_lock:
        atomic_write_json(progress_path, progress)

def _run_qc(output_path, progress):
    """质检拦截导出门：对最终逻辑章做轻量规则质检，返回问题清单（空表 = 通过）。
    规则：空章 / 疑似重复（哈希相同）/ 残留机器分隔标记 / 疑似占位未完稿。"""
    output_path = Path(output_path)
    issues = []
    hashes = {}
    for out_id in _iter_output_chapter_ids(progress):
        recon_file = output_path / "02_workspace/reconstructed" / f"chapter_{out_id}.txt"
        if not recon_file.exists():
            issues.append(f"章 {out_id}: 缺少重构稿（reconstructed/chapter_{out_id}.txt）")
            continue
        text = recon_file.read_text(encoding='utf-8', newline='').strip()
        if not text:
            issues.append(f"章 {out_id}: 为空章")
            continue
        if "===CHAPTER_BREAK===" in text:
            issues.append(f"章 {out_id}: 残留机器分隔标记 ===CHAPTER_BREAK===")
        low = text.lower()
        for kw in ("todo", "占位符", "待补", "此处省略"):
            if kw in low:
                issues.append(f"章 {out_id}: 疑似占位/未完稿（出现「{kw}」）")
                break
        h = hashlib.sha256(text.encode('utf-8')).hexdigest()
        if h in hashes:
            issues.append(f"章 {out_id}: 与第 {hashes[h]} 章内容重复（可能未重构）")
        hashes[h] = out_id
    return issues


async def _wait_qc_confirm(progress, progress_path, run_id):
    """质检决策等待：等待用户选择强制导出或停止。"""
    state.qc_confirm.clear()
    while not state.qc_confirm.is_set():
        if state.stop_requested: raise asyncio.CancelledError()
        progress["last_heartbeat"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        async with state.progress_lock:
            atomic_write_json(progress_path, progress)
        await asyncio.sleep(1.0)


async def _wait_pause(progress, progress_path):
    while not state.pause_event.is_set():
        if state.stop_requested: raise asyncio.CancelledError()
        progress["last_heartbeat"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        async with state.progress_lock:
            atomic_write_json(progress_path, progress)
        await asyncio.sleep(1.0)
