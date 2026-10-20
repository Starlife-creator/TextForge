import asyncio, re, logging, html, hashlib
from collections import deque
from pathlib import Path
from utils.atomic import atomic_write_text, atomic_write_json
from routes import state
from routes.events import sse_emit
from core.stitch import stitch_pipeline
from core.api_client import (call_with_retry, RetryableBusinessError,
                             ContextOverflowError, pick_endpoint)
from core.budget import output_token_reserve
from core.batching import split_batch_in_progress as _split_batch_in_progress
from utils.tokens import estimate_tokens

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
    from core.api_client import make_httpx_client, pick_endpoint
    ep_ref = pick_endpoint(req, "refactor")
    ep_stitch = pick_endpoint(req, "stitch")
    payload_base = {
        "output_path": req.output_path,
        "api_url": ep_stitch["api_url"],
        "blueprint": blueprint_for_stitch,
        "author_style": req.author_style,
        "payload": {"model": ep_stitch["model"], "temperature": req.temperatures.stitch, "stream": True},
        "progress": progress,      # v8.8：供 apply_batch_stitch 映射虚拟章父章
        "skip_smooth": bool(gates.get("skip_stitch_if_smooth")),
    }
    
    # 断点恢复：逐章验收门开启时，若上次停在 phase3_accept 且该批未验收，
    # 必须重新推送预览。原实现里批次在 _refactor_and_split 内就被标成 phase3_done，
    # 而下面只筛 phase1_done 的批次 → 恢复时该批被直接跳过，用户根本没看过就进了缝合。
    if gates.get("require_chapter_accept"):
        pend = progress.get('accept_pending_batch')
        done = progress.get('accepted_batches') or []
        if progress.get('current_phase') == 'phase3_accept' and pend is not None and pend not in done:
            pending_batch = next((b for b in progress['batches'] if b.get('batch_id') == pend), None)
            if pending_batch is None:
                logger.warning(f"[accept] 恢复失败：progress.json 里的 accept_pending_batch={pend} 不在 batches 中")
            else:
                logger.info(f"[accept] 恢复：批次 {pend} 尚未验收，重新推送预览")
                await _wait_batch_accept(progress, progress_path, pending_batch, output_path, run_id)

    # 含 phase3_done：恢复时跳过已完成的批次（否则会重复计费并覆盖已产出稿）
    completed_batches = [b for b in progress['batches'] if b['status'] in ('phase1_done', 'phase3_done')]

    async with make_httpx_client(req, api_url=ep_ref["api_url"], api_key=ep_ref["api_key"]) as client:
        # 阶段 3-1：无重叠重构
        #
        # 用 deque 而不是 for：当某批触发「上下文超限」或「分隔符契约失败」时，
        # 要把该批**对半拆细**再重新入队重跑，而不是让整轮报废。
        # 这正是本地小模型/小显存（LM Studio OOM、8448 窗口）下最主要的可用性保障：
        # 配置填错（或模型比预期弱）时，代价应当是「批次更多、更慢」，而不是「报错、跑不完」。
        queue = deque(b for b in completed_batches if b['status'] != 'phase3_done')
        while queue:
            batch = queue.popleft()
            if batch['status'] == 'phase3_done':
                continue
            if state.stop_requested: raise asyncio.CancelledError()
            await _wait_pause()

            # 自适应上限：一旦发现过「撑爆」的批次，就把后续批次也预先拆到同尺度，
            # 避免每批都先浪费一次失败调用（本地模型一次失败可能就是几分钟）。
            cap = progress.get('adaptive_max_chapters')
            if cap and len(batch['chapter_ids']) > cap:
                parts = _split_batch_in_progress(progress, batch, reason="adaptive",
                                                 detail=f"参考已失败批次，单批章数上限降为 {cap}",
                                                 run_id=run_id)
                if parts:
                    queue.extendleft(reversed(parts))
                    async with state.progress_lock:
                        atomic_write_json(progress_path, progress)
                    continue

            try:
                output_files = await _refactor_and_split(
                    client, req, output_path, recon_dir, blueprint_text,
                    progress, batch, run_id, ep_ref)
            except ContextOverflowError as e:
                # 第 2 层：上下文超限 → 对半拆批重跑
                parts = _split_batch_in_progress(
                    progress, batch, reason="overflow",
                    detail=(f"模型上限 {e.available} token" if e.available else "超出模型上下文窗口"),
                    run_id=run_id)
                async with state.progress_lock:
                    atomic_write_json(progress_path, progress)
                if not parts:
                    # 只含 1 章（或 1 个不可拆的虚拟组）仍超限：给出可执行的建议
                    raise RuntimeError(
                        f"单章仍超出模型上下文：批次 {batch['batch_id']} 章节={batch['chapter_ids']}，"
                        + (f"模型上限约 {e.available} token。" if e.available else "模型上下文不足。")
                        + "请调大模型上下文长度，或在导入时把该章拆成更小的文件。") from e
                queue.extendleft(reversed(parts))
                continue
            except ChapterBreakMissingError as e:
                # 第 4 层：小模型在长上下文里维持不住分隔符契约 → 同样降级拆批，
                # 让每批章数更少、要吐的分隔符更少，而不是重发同样大小的请求碰运气。
                parts = _split_batch_in_progress(
                    progress, batch, reason="contract", detail=str(e)[:120], run_id=run_id)
                async with state.progress_lock:
                    atomic_write_json(progress_path, progress)
                if not parts:
                    raise  # 单章还失败：交给上层报错（此时拆无可拆）
                queue.extendleft(reversed(parts))
                continue

            async with state.progress_lock:
                atomic_write_json(progress_path, progress)
            logger.info(f"[phase3] batch {batch['batch_id']} 完成 文件={output_files}")
            sse_emit("batch_done", {"batch_id": batch['batch_id'], "output_files": output_files}, run_id)
            # 逐章验收门：开启时每个批次重构稿产出后暂停，等待用户验收再继续
            if gates.get("require_chapter_accept"):
                await _wait_batch_accept(progress, progress_path, batch, output_path, run_id)
            # 铁律8：批次间保持串行间隔，避免连续请求触发云端限流（间隔可配置，缺省 2s）
            if queue:
                await asyncio.sleep(getattr(req, "batch_interval_sec", 2.0))
    
    # 阶段 3-2：缝合（用缝合阶段的独立端点 client）
    # 注意：必须与上面的重构 client 平级，否则重构的连接池会在整个缝合阶段空占
    async with make_httpx_client(req, api_url=ep_stitch["api_url"], api_key=ep_stitch["api_key"]) as stitch_client:
        await stitch_pipeline(output_path, progress, stitch_client, payload_base, sse_emit, run_id, chars)
    
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
            await state.absorb_gate_result(progress, progress_path, ['_qc_force'])
            progress['current_phase'] = 'phase3-1'
            async with state.progress_lock:
                atomic_write_json(progress_path, progress)

    progress['current_phase'] = 'phase4'
    async with state.progress_lock:
        atomic_write_json(progress_path, progress)

async def regen_single_chapter(req, output_path, chapter_id):
    """A1：单章重生成。复用运行中 req（含鉴权，仅内存），对单章重新走一次重构，
    覆盖 reconstructed/chapter_{chapter_id}.txt，返回新正文。用于逐章验收「驳回→重写」。"""
    from core.api_client import make_httpx_client, call_with_retry
    ep = pick_endpoint(req, "refactor")
    output_path = Path(output_path)
    split_file = output_path / "02_workspace/split" / f"{chapter_id}.txt"
    if not split_file.exists():
        raise ValueError(f"章节原始文件不存在: chapter_{chapter_id}")
    batch_content = split_file.read_text(encoding='utf-8', newline='')
    try:
        blueprint_text = (output_path / "Story_Bible.md").read_text(encoding='utf-8')
    except OSError:
        blueprint_text = ""
    gates = getattr(req, "refactor_gates", {}) or {}
    prompt = _build_refactor_prompt(
        getattr(req, "refactor_mode", "full_rewrite"),
        blueprint_text, req.author_style, batch_content,
        forbidden_canon=getattr(req, "forbidden_canon", []) or [],
        name_map=getattr(req, "name_map", {}) or {},
        fix_list=getattr(req, "fix_list", []) or [],
        gates=gates,
        n_chapters=1,   # 单章重生成：明确要求不输出分隔符
    )
    payload = {
        "model": ep["model"],
        "messages": [{"role": "user", "content": prompt}],
        "temperature": req.temperatures.refactor,
        # 与主重构路径保持一致：用 token 估算 + 预留，而不是旧的 len/1.5 字符公式
        # （后者对中文低估约 50%，会让模型被迫截断单章重生成）。
        "max_tokens": output_token_reserve(estimate_tokens(batch_content)),
        "stream": True,
    }
    logger.info(f"[regen] 单章重生成 chapter_{chapter_id} 开始")
    async with make_httpx_client(req, api_url=ep["api_url"], api_key=ep["api_key"]) as client:
        # run_id 必须传真实的当前 run：写死 "regen" 会让 sse_emit 找不到订阅者，
        # 流式预览将毫无反应（界面上仍显示上一批的文本）。
        result, usage, _ = await call_with_retry(client, ep["api_url"], payload, sse_emit,
                                                 state.current_run_id, max_retries=2)
    chapter_texts = _split_refactored_by_chapters(result, [chapter_id])
    new_text = chapter_texts[chapter_id]
    recon_dir = output_path / "02_workspace/reconstructed"
    recon_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(recon_dir / f"chapter_{chapter_id}.txt", new_text)
    logger.info(f"[regen] 单章重生成完成 chapter_{chapter_id}")
    return new_text


async def _refactor_and_split(client, req, output_path, recon_dir, blueprint_text,
                              progress, batch, run_id, ep=None):
    """v8.8 新增：将 '调用 + 按章拆分' 包成一个可重试单元。
    这样 ChapterBreakMissingError 由 call_with_retry 捕获并重试（而非在返回后丢失）。
    A2：ep = pick_endpoint(req,'refactor')，未传则回退全局（单厂商兼容）。"""
    ep = ep or pick_endpoint(req, "refactor")
    chapter_ids = batch['chapter_ids']
    # 批次与章节表的一致性前置校验：否则下面 _find_idx 抛错时无从判断是哪个批次出的问题
    known = {c['id'] for c in progress.get('chapters', [])}
    missing = [cid for cid in chapter_ids if cid not in known]
    if missing:
        raise RuntimeError(
            f"批次 {batch.get('batch_id')} 引用了 progress['chapters'] 中不存在的章 {missing}；"
            f"progress.json 很可能来自不兼容的旧版本，建议换一个输出目录重跑")
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
        n_chapters=len(chapter_ids),
    )
    payload = {
        "model": ep["model"], "messages": [{"role": "user", "content": prompt}],
        "temperature": req.temperatures.refactor,
        # 输出预留按 token 估（旧实现用 len/1.5，对中文低估约 50%，会写出偏小的
        # max_tokens 让模型被迫截断，正文半截就返回 → 空章/契约失败）。
        "max_tokens": output_token_reserve(estimate_tokens(batch_content)),
        "stream": True,
    }
    logger.info(f"[phase3] batch {batch['batch_id']} 开始 章节={chapter_ids}")
    sse_emit("batch_start", {"batch_id": batch['batch_id'], "chapter_ids": chapter_ids}, run_id)

    # 「分隔符数量不符」要能真正重试，必须同时满足两点，原实现**两点都没做到**：
    #   1. 契约校验放在 call_with_retry 的 validate 里。原先是在它返回**之后**才拆分，
    #      ChapterBreakMissingError 进不了 except 分支 —— 一次错就是整批报废。
    #   2. 重试必须带上错因（on_retry）。原先用完全相同的 payload 再调一次，
    #      模型不知道自己多了 2 个分隔符，只是碰运气 —— 同一批 22:31 蒙对、22:55 又错。
    _parsed: dict = {}

    def _validate(text: str) -> None:
        _parsed["chapters"] = _split_refactored_by_chapters(text, chapter_ids)

    result, usage, _ = await call_with_retry(
        client, ep["api_url"], payload, sse_emit, run_id,
        on_retry=_break_feedback(len(chapter_ids)),
        validate=_validate)
    chapter_texts = _parsed["chapters"]
    
    output_files = []
    for cid, ctext in chapter_texts.items():
        rel = f"02_workspace/reconstructed/chapter_{cid}.txt"
        atomic_write_text(recon_dir / f"chapter_{cid}.txt", ctext)
        # P1.7 章快照：重构初稿镜像到 snapshots/drafts（默认关）
        if getattr(req, "chapter_snapshots", False):
            draft_dir = output_path / "02_workspace/snapshots/drafts"
            draft_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_text(draft_dir / f"chapter_{cid}.txt", ctext)
        if rel not in progress.setdefault('completed_files', []):
            progress['completed_files'].append(rel)
        output_files.append(rel)

    for v in progress.setdefault('virtual_chapter_map', []):
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
    # 不抛裸 KeyError：那会被 run_pipeline 的兜底 except 吞成 code="exception"，
    # 消息只剩一个章号，完全看不出是哪个批次/文件出的问题。
    raise KeyError(f"章节 id={cid} 不在 progress['chapters'] 中（批次与章节表不一致）")

def _split_refactored_by_chapters(text: str, chapter_ids: list) -> dict:
    """按 ===CHAPTER_BREAK=== 精确分隔；数量不符或出现空章时抛 ChapterBreakMissingError。"""
    if len(chapter_ids) == 1:
        # 单章批次（每本书的最后一个批次都是）也必须走空章检查：
        # 早先这里直接 return，模型返回空内容时会静默写出一个空 chapter_*.txt，
        # 再被 document.py 的「跳过空段」吞掉 —— 最终 docx 凭空少一章且零诊断。
        t = text.strip()
        if not t:
            raise ChapterBreakMissingError(
                f"重构输出为空：章节 {chapter_ids[0]}。空章不应进入成品，请重试。")
        return {chapter_ids[0]: t}
    # 先按标记切、再比对数量，并**按位**对齐到 chapter_ids。
    # 原实现先过滤空段再比对，任一章重构为纯空白就会被丢弃 → 数量对不上 → 整轮白跑。
    raw_parts = text.split("===CHAPTER_BREAK===")
    # 容忍结尾多打的空标记（模型常犯），旧实现过滤空段后能通过，不能因此白白烧掉一次重试
    while len(raw_parts) > 1 and not raw_parts[-1].strip():
        raw_parts.pop()
    n_marks = len(raw_parts) - 1
    if n_marks != len(chapter_ids) - 1:
        raise ChapterBreakMissingError(
            f"重构输出 ===CHAPTER_BREAK=== 数量不符：需要 {len(chapter_ids)-1} 个，实际 {n_marks} 个"
            f"（切出 {len(raw_parts)} 段，期望 {len(chapter_ids)} 段）。请重试。"
        )
    aligned = [raw_parts[i].strip() for i in range(len(chapter_ids))]
    blanks = [cid for i, cid in enumerate(chapter_ids) if not aligned[i]]
    if blanks:
        raise ChapterBreakMissingError(
            f"重构输出中以下章节为空：{blanks}。空章不应进入成品，请重试。")
    return {cid: aligned[i] for i, cid in enumerate(chapter_ids)}

def _break_feedback(n_chapters: int):
    """生成 on_retry 钩子：把「分隔符数量不符」的具体错因写回下一次请求。

    没有它时重试是完全相同的 prompt，模型不知道自己多了 2 个分隔符，
    只能碰运气 —— 同一批 22:31 蒙对、22:55 又错，10 分钟白跑。
    """
    need = max(0, n_chapters - 1)

    def hook(payload: dict, exc: Exception) -> None:
        got = None
        m = re.search(r"实际\s*(\d+)\s*个", str(exc))
        if m:
            got = int(m.group(1))
        if got is None:
            tail = (f"上一次输出不符合格式要求：{str(exc)[:200]}\n"
                    f"本批共 {n_chapters} 章，请只输出 {need} 个 ===CHAPTER_BREAK===。")
        else:
            delta = need - got
            direction = "删除多余的" if delta < 0 else "补上缺少的"
            tail = (f"你上一次的输出**不合格**：本批共 {n_chapters} 章，只需要 {need} 个 "
                    f"`===CHAPTER_BREAK===`，你却输出了 {got} 个（多了 {-delta} 个）。\n"
                    f"请重新完整输出一遍：{direction} {abs(delta)} 个分隔符；\n"
                    f"特别注意原文里的「第N章」标题要**原样保留在正文里**，"
                    f"不要额外用 ===CHAPTER_BREAK=== 重复标注。\n"
                    f"除正文外不要输出任何其它内容。")
        payload.setdefault("messages", []).append({"role": "user", "content": tail})
        # 纠错轮稍微降一点温度，避免又陷入同一种错误模式
        if "temperature" in payload:
            payload["temperature"] = max(0.0, min(payload["temperature"], 0.5))
        logger.info(f"[phase3] 注入分隔符纠正提示：{tail.splitlines()[0][:120]}")

    return hook


def _build_refactor_prompt(mode, blueprint, author_style, batch_content, *,
                           forbidden_canon=(), name_map=None, fix_list=(), gates=None,
                           n_chapters=1):
    """按重构档位选择提示词。缺省/未知 mode 一律回退 full_rewrite（原行为）。
    档位提示词构建后，若启用了任何「保护门」(lock_names/lock_plot/inject_forbidden)，
    追加一段强制规则；全关时完全不加，旧行为不变。

    n_chapters：本批实际章数，必须传真实值 —— 分隔条数契约（_common_tail）依赖它。
    """
    n_chapters = max(1, int(n_chapters or 1))
    mode = mode or "full_rewrite"
    if mode == "fidelity":
        prompt = _prompt_fidelity(blueprint, author_style, batch_content, forbidden_canon, gates, n_chapters)
    elif mode == "fix_gaps":
        prompt = _prompt_fix_gaps(blueprint, author_style, batch_content, forbidden_canon, fix_list, gates, n_chapters)
    elif mode == "reskin":
        prompt = _prompt_reskin(blueprint, author_style, batch_content, name_map, n_chapters)
    else:
        prompt = _prompt_full_rewrite(blueprint, author_style, batch_content, n_chapters)
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
#
# 分隔条数必须**显式告知**：原文里本来就有「第N章」之类的章节标题，
# 只说「每章之间分隔」时，模型常常在原标题处也插一个分隔符，
# 结果 4 章输出成 6 段（见回归用例 test_marker_count_explicit_in_prompt）。
def _common_tail(n_chapters: int) -> str:
    if n_chapters <= 1:
        return """4. 格式保留：严格保留原文的 Markdown 格式标记。保留原标题行。
5. 本批次只有 1 章：**不要输出 `===CHAPTER_BREAK===`**，整段输出即为该章正文。
6. 直接输出正文：绝对不要输出任何解释、说明或问候语。"""
    return f"""4. 格式保留：严格保留原文的 Markdown 格式标记。保留原标题行。
5. **章节分隔（关键）**：本批次共 **{n_chapters} 章**，因此必须且只能输出 **{n_chapters - 1} 个** `===CHAPTER_BREAK===`，写在相邻两章之间、各占一行。
   - 原文中已有的「第N章」等章节标题请**原样保留在正文里**，不要用 `===CHAPTER_BREAK===` 代替它，也不要在它前面另加一个。
   - 多一个或少一个都会导致整批返工，请先在心里数一遍再输出。
6. 直接输出正文：绝对不要输出任何解释、说明或问候语。"""


def _prompt_full_rewrite(blueprint, author_style, batch_content, n_chapters):
    """原「全部重构」极端档：蓝图是方向不是宪法，允许推翻细节、改人名、整段重写。"""
    return f"""你是一位小说主笔。请严格按照用户提供的【全局设定与重构方向】，对【待处理正文】进行重构优化。
要求：
1. 保留剧情骨架：核心剧情、人物基础人设、世界观设定不得篡改。
2. 修复内部断层：如果本批次章节内部存在逻辑跳跃，请合理补充过渡段落。
3. 去AI味与优化文笔：用具体动作展示情绪，打破单调句式，让文字富有画面感。
{_common_tail(n_chapters)}
【全局设定与重构方向】
{blueprint}

【作者特殊风格要求】
{author_style}

【待处理正文】
{batch_content}
"""


def _prompt_fidelity(blueprint, author_style, batch_content, forbidden_canon, gates, n_chapters):
    """保真润色档：锁定人名与事件骨架，仅润色表述，逐句贴近原意。"""
    forbid = _render_forbidden(forbidden_canon, gates)
    return f"""你是一位保真度优先的小说润色师。你的任务是在【不改变任何事实的前提下】优化文字。

硬性铁律：
1. **锁定人名、地名、专有名词与事件顺序**：一律不得改动、替换为其字面，原样保留。
2. **锁定剧情骨架与逻辑走向**：不得增删情节、不得加入原文没有的关键事件。
3. {forbid}4. 只做表述润色：优化句式、去重复、提升画面感和节奏，但每句的意思贴近原文。
{_common_tail(n_chapters)}

【全局设定（仅作一致性参照，不推翻）】
{blueprint}

【作者特殊风格要求】
{author_style}

【待处理正文】
{batch_content}
"""


def _prompt_fix_gaps(blueprint, author_style, batch_content, forbidden_canon, fix_list, gates, n_chapters):
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
{_common_tail(n_chapters)}

【全局设定（仅作一致性参照，不推翻）】
{blueprint}

【作者特殊风格要求】
{author_style}

【待处理正文】
{batch_content}
"""


def _prompt_reskin(blueprint, author_style, batch_content, name_map, n_chapters):
    """换皮档：节拍锁定，按映射表统一替换人名/设定名。"""
    mapping = _render_name_map(name_map)
    return f"""你是一位既保留原有节拍、又把作品「换皮」的改写师。原始剧情节拍不变，仅统一更换人设/地名等外皮。

硬性铁律：
1. **保留原有剧情节拍与事件顺序**：起承转合、关键转折一一对应，不得增删主线。
2. **按映射表替换名字/设定**：任一见到的旧名一律替换为对应新名，不得遗漏或自造其它。
3. {mapping}4. 在换皮基础上顺滑文字，但不得改变人物关系与情节逻辑。
{_common_tail(n_chapters)}

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

    current_phase 固定停在 'phase3_accept'，由 phase3_refactor_stitch 开头据此判断是否需要
    重新推送（幂等）。用户点确认后由 /api/accept 写入 accepted_batches，这里并回内存；
    阶段推进到 'phase3-1'（绝不回退到 'phase2'，否则恢复时蓝图会被重新生成）。"""
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
# 先 clear 标志，再落盘 phase3_accept —— 顺序不能反。
    # 反过来就存在窗口：progress 已是 phase3_accept（界面已弹出验收框、/api/status
    # 的 resume 也已就绪），但标志尚未 clear，此刻到达的确认会被随后的 clear()
    # 静默抹掉，流水线永远停在验收门，用户那边却显示「已确认」。
    state.accept_confirm.clear()
    progress['current_phase'] = 'phase3_accept'
    progress['accept_pending_batch'] = batch['batch_id']
    async with state.progress_lock:
        atomic_write_json(progress_path, progress)
    sse_emit("accept_ready", {"batch_id": batch['batch_id'], "chapters": chapters,
        "accepted": progress.get('accepted_batches', [])}, run_id)
    logger.info(f"[accept] 批次 {batch['batch_id']} 待验收（{len(chapters)} 章）")
    while not state.accept_confirm.is_set():
        if state.stop_requested: raise asyncio.CancelledError()
        await asyncio.sleep(1.0)
    await state.absorb_gate_result(progress, progress_path, ['accepted_batches'])
    progress['current_phase'] = 'phase3-1'
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
        await asyncio.sleep(1.0)


async def _wait_pause():
    """等待用户恢复。心跳由 pipeline_runner._heartbeat_loop 统一负责，此处只轮询。"""
    while not state.pause_event.is_set():
        if state.stop_requested: raise asyncio.CancelledError()
        await asyncio.sleep(1.0)
