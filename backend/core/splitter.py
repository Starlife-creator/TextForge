import asyncio, re, hashlib, unicodedata, time, logging, json
from pathlib import Path
from docx import Document
from utils.atomic import atomic_write_text, atomic_write_json
from utils.sort import natural_sort_key
from routes import state
from routes.events import sse_emit
import chardet

logger = logging.getLogger("textforge")

# v8.8 修正：支持 "第一章：" / "第一章:" / "第一章　" / 行尾，
# 以及无分隔符直接接标题的常见网文格式（如 "第一章初入江湖"）
CHAPTER_PATTERN = re.compile(r'^第[零一二三四五六七八九十百千万0-9两]+[章回节卷](?:[：:\s　]|$)?', re.MULTILINE)

def _read_txt_auto_encoding(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ('utf-8-sig', 'utf-8', 'gbk'):
        try: return raw.decode(enc)
        except UnicodeDecodeError: continue
    detected = chardet.detect(raw[:102400])
    return raw.decode(detected.get('encoding') or 'utf-8', errors='replace')

def _read_docx(path: Path) -> str:
    doc = Document(str(path))
    return "\n\n".join(p.text if p.text.strip() else "" for p in doc.paragraphs)

def _normalize_newlines(text: str) -> str:
    """统一换行为 LF：Windows 源文件常为 CRLF，若保留 \r 会导致后续
    split('\n\n') 段落切分失效（尤其是超长章的虚拟化分段）。"""
    return text.replace('\r\n', '\n').replace('\r', '\n')

def _read_file(path: Path, fmt: str) -> str:
    return _normalize_newlines(_read_docx(path) if fmt == "docx" else _read_txt_auto_encoding(path))

def _sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode('utf-8')).hexdigest()

def _split_by_paragraph(text: str, max_chars: int) -> list[str]:
    paras = text.split("\n\n")
    result = []
    current = ""
    for p in paras:
        if len(current) + len(p) + 2 > max_chars and current:
            result.append(current.strip())
            current = p
        else:
            current = (current + "\n\n" + p).strip() if current else p
    if current:
        result.append(current.strip())
    return result

def _split_virtual_chapters(chapters, split_dir: Path, chars: float) -> tuple[list, list]:
    """被虚拟化的原始章保留在 new_chapters，标记 is_virtual_parent=True。"""
    trigger = chars * 0.40 * 0.9
    max_virtual = chars * 0.20
    new_chapters = []
    virtual_map = []
    for ch in chapters:
        text = (split_dir / f"{ch['id']}.txt").read_text(encoding='utf-8', newline='')
        if len(text) >= trigger:
            segments = _split_by_paragraph(text, max_virtual)
            for i, seg in enumerate(segments):
                # 数字后缀而非字母：同一章切出>26段时 chr(ord('a')+i) 会生成非法 id
                vid = f"{ch['id']}_{i}"
                atomic_write_text(split_dir / f"{vid}.txt", seg)
                new_chapters.append({
                    "id": vid, "filename": f"{ch['filename']}#{i+1}",
                    "is_empty": False, "hash": _sha256(seg),
                    "split_file": f"02_workspace/split/{vid}.txt",
                    "is_virtual": True, "original_id": ch['id'], "order": i,
                })
                virtual_map.append({
                    "virtual_id": vid, "original_id": ch['id'],
                    "original_filename": ch['filename'], "order": i,
                    "batch_id": None,
                })
            ch['is_virtual_parent'] = True
            new_chapters.append(ch)
        else:
            new_chapters.append(ch)
    return new_chapters, virtual_map

async def phase0_split(req, progress, progress_path, run_id):
    output_path = Path(req.output_path)
    split_dir = output_path / "02_workspace/split"
    split_dir.mkdir(parents=True, exist_ok=True)

    # v8.8 修正：恢复时若 phase0 已完成，跳过重拆
    if progress.get('current_phase') != 'phase0' and progress.get('chapters'):
        sse_emit("phase_done", {"phase": "phase0", "total_chapters": len(progress['chapters'])}, run_id)
        return

    input_path = Path(req.input_path)
    chapters = []
    if req.input_mode == "single_file":
        full_text = unicodedata.normalize('NFC', _read_file(input_path, req.input_format))
        matches = list(CHAPTER_PATTERN.finditer(full_text))
        if len(matches) < 2:
            for i in range(0, len(full_text), 3000):
                chapters.append({"id": str(len(chapters) + 1), "filename": f"段{len(chapters)+1:03d}.txt", "text": full_text[i:i+3000]})
        else:
            for i, m in enumerate(matches):
                start = m.start()
                end = matches[i+1].start() if i + 1 < len(matches) else len(full_text)
                chapters.append({"id": str(i + 1), "filename": f"第{i+1:03d}章.txt", "text": full_text[start:end]})
    else:
        files = sorted(
            [f for f in input_path.iterdir() if f.is_file() and f.suffix[1:].lower() in ("txt", "md", "docx")],
            key=lambda f: natural_sort_key(f.name),
        )
        for i, f in enumerate(files):
            text = _read_file(f, f.suffix[1:].lower())
            chapters.append({"id": str(i + 1), "filename": f.name, "text": unicodedata.normalize('NFC', text)})
    
    for ch in chapters:
        split_file = split_dir / f"{ch['id']}.txt"
        atomic_write_text(split_file, ch['text'])
        ch['is_empty'] = len(ch['text'].strip()) < 50
        ch['hash'] = _sha256(ch['text'])
        ch['split_file'] = f"02_workspace/split/{ch['id']}.txt"
        del ch['text']
    
    chars = req.context_window * 1.5
    chapters, virtual_map = _split_virtual_chapters(chapters, split_dir, chars)

    # 空章检查必须在推进 current_phase 之前：否则全部为空时虽报错，
    # 但恢复会带着 phase1 标记空跑各阶段，最终静默产出空文档
    non_empty = [c for c in chapters if not c['is_empty']]
    if not non_empty:
        raise RuntimeError("所有章节均为空章，请检查原始文档编码或内容")

    progress['chapters'] = chapters
    progress['virtual_chapter_map'] = virtual_map
    progress['current_phase'] = 'phase1'

    async with state.progress_lock:
        atomic_write_json(progress_path, progress)
    sse_emit("phase_done", {"phase": "phase0", "total_chapters": len(chapters)}, run_id)

async def phase1_diagnose(req, progress, progress_path, run_id):
    # v8.8 修正：恢复时若阶段已越过 phase1，直接跳过（避免重跑并把阶段标记打回 phase2）
    if state.phase_passed(progress, 'phase1'):
        sse_emit("phase_done", {"phase": "phase1", "resumed_skip": True}, run_id)
        logger.info(f"[phase1] 阶段已完成（current={progress.get('current_phase')}），恢复跳过")
        return

    output_path = Path(req.output_path)
    summaries_dir = output_path / "01_summaries"
    summaries_dir.mkdir(parents=True, exist_ok=True)
    
    chars = req.context_window * 1.5
    batch_limit = chars * 0.40
    overlap_limit = chars * 0.15
    
    batches = _load_batches(progress['chapters'], output_path, batch_limit)
    # v8.8：恢复时若 batches 已存在，保留已有 output_files/status，不重建
    if not progress['batches']:
        progress['batches'] = [{"batch_id": i + 1, "chapter_ids": b, "status": "pending",
                                 "output_files": []} for i, b in enumerate(batches)]
    
    # 重建 cross_batch_virtual（幂等）
    progress['cross_batch_virtual'] = _build_cross_batch_virtual(
        progress['chapters'], progress['batches'], progress['virtual_chapter_map'])
    # 批次构建结果立即持久化：首批失败/崩溃后 status 查询与恢复都能看到批次划分
    async with state.progress_lock:
        atomic_write_json(progress_path, progress)
    
    from core.api_client import call_with_retry, make_httpx_client, pick_model
    async with make_httpx_client(req) as client:
        prev_context = ""
        for idx, batch in enumerate(progress['batches']):
            # v8.8 修正：恢复时跳过已 phase1_done 的批次
            if batch['status'] != 'pending':
                prev_context = _build_overlap(progress['chapters'], batch['chapter_ids'], output_path, overlap_limit)
                continue
            chapter_ids = batch['chapter_ids']
            if state.stop_requested: raise asyncio.CancelledError()
            await _wait_pause(progress, progress_path)
            
            batch_content = "\n\n".join(
                (output_path / progress['chapters'][_find_idx(progress['chapters'], cid)]['split_file']).read_text(encoding='utf-8', newline='')
                for cid in chapter_ids
            )
            # P1.4：仅当设置了 diagnose_json 路径时才追加结构化 JSON 契约（缺省关）
            with_json = bool(getattr(req, "diagnose_json", None))
            prompt = _build_diagnose_prompt(prev_context, batch_content, with_json=with_json)
            payload = {"model": pick_model(req, "diagnose"), "messages": [{"role": "user", "content": prompt}],
                "temperature": req.temperatures.diagnose, "max_tokens": 2000, "stream": True}
            logger.info(f"[phase1] batch {batch['batch_id']} 开始 章节={chapter_ids}")
            sse_emit("batch_start", {"batch_id": batch['batch_id'], "chapter_ids": chapter_ids}, run_id)
            result, usage, _ = await call_with_retry(client, req.api_url, payload, sse_emit, run_id)
            summary_file = f"01_summaries/batch_{batch['batch_id']:02d}.txt"
            atomic_write_text(summaries_dir / f"batch_{batch['batch_id']:02d}.txt", result)
            # P1.4：可选结构化诊断落盘（与 batch_XX.txt 并存），解析失败不阻断流水线
            if with_json:
                diag = _parse_diag_json(result, batch['batch_id'])
                if diag is not None:
                    diag_dir = Path(req.diagnose_json)
                    diag_dir.mkdir(parents=True, exist_ok=True)
                    atomic_write_json(diag_dir / f"diagnose_{batch['batch_id']:02d}.json", diag)
                    logger.info(f"[phase1] batch {batch['batch_id']} 诊断JSON -> {diag_dir / f'diagnose_{batch['batch_id']:02d}.json'}")
                else:
                    logger.warning(f"[phase1] batch {batch['batch_id']} 未解析出诊断 JSON，跳过落盘")
            
            prev_context = _build_overlap(progress['chapters'], chapter_ids, output_path, overlap_limit)
            
            # v8.8 修正：累计 usage
            _accum_usage(progress, usage)
            batch['status'] = 'phase1_done'
            if batch['batch_id'] not in progress['processed_batches']:
                progress['processed_batches'].append(batch['batch_id'])
            async with state.progress_lock:
                atomic_write_json(progress_path, progress)
            logger.info(f"[phase1] batch {batch['batch_id']} 完成 -> {summary_file}")
            sse_emit("batch_done", {"batch_id": batch['batch_id'], "output_file": summary_file}, run_id)
            # 铁律8：批次间保持串行间隔，避免连续请求触发云端限流（间隔可配置，缺省 2s）
            if idx < len(progress['batches']) - 1:
                await asyncio.sleep(getattr(req, "batch_interval_sec", 2.0))
    
    progress['current_phase'] = 'phase2'
    async with state.progress_lock:
        atomic_write_json(progress_path, progress)

def _accum_usage(progress, usage):
    """v8.8 新增：累计 token 用量。"""
    if not usage: return
    progress['usage']['prompt_tokens'] += usage.get('prompt_tokens', 0) or 0
    progress['usage']['completion_tokens'] += usage.get('completion_tokens', 0) or 0

def _find_idx(chapters, cid):
    for i, c in enumerate(chapters):
        if c['id'] == cid: return i
    raise KeyError(cid)

def _load_batches(chapters, output_path, batch_limit):
    """按 original_id 分组；跳过 is_virtual_parent 父章；同一组不拆散。"""
    groups = []  # list of (group_key, [chapter_dict])
    for ch in chapters:
        if ch.get('is_empty'):
            continue
        if ch.get('is_virtual_parent'):
            continue  # 父章不进批次
        if ch.get('is_virtual'):
            orig = ch.get('original_id')
            placed = False
            for gk, glist in groups:
                if gk == ('virtual', orig):
                    glist.append(ch)
                    placed = True
                    break
            if not placed:
                groups.append((('virtual', orig), [ch]))
        else:
            groups.append((('normal', ch['id']), [ch]))
    
    batches = []
    current = []
    current_chars = 0
    for gk, glist in groups:
        group_chars = sum(
            len((output_path / ch['split_file']).read_text(encoding='utf-8', newline=''))
            for ch in glist
        )
        group_ids = [ch['id'] for ch in glist]
        if current and (current_chars + group_chars > batch_limit or len(current) + len(group_ids) > 8):
            batches.append(current)
            current = list(group_ids)
            current_chars = group_chars
        else:
            current.extend(group_ids)
            current_chars += group_chars
    if current:
        batches.append(current)
    return batches

def _build_cross_batch_virtual(chapters, batches, virtual_map):
    """扫描虚拟章跨批次分布。返回 [{"original_id": "7", "batch_pairs": [[2,3]]}, ...]"""
    if not virtual_map:
        return []
    vid_to_batch = {}
    for b in batches:
        for cid in b['chapter_ids']:
            vid_to_batch[cid] = b['batch_id']
    orig_to_batches = {}
    for v in virtual_map:
        orig = v['original_id']
        bid = vid_to_batch.get(v['virtual_id'])
        if bid is not None:
            orig_to_batches.setdefault(orig, set()).add(bid)
    result = []
    for orig, bset in orig_to_batches.items():
        if len(bset) > 1:
            sorted_batches = sorted(bset)
            pairs = [[sorted_batches[i], sorted_batches[i+1]] for i in range(len(sorted_batches) - 1)]
            result.append({"original_id": orig, "batch_pairs": pairs})
    return result

def _build_overlap(chapters, prev_chapter_ids, output_path, overlap_limit):
    """回溯时按 original_id 合并虚拟章。"""
    logical = {}  # original_id -> list[(order, text)]
    for cid in prev_chapter_ids:
        ch = chapters[_find_idx(chapters, cid)]
        if ch.get('is_empty'):
            continue
        text = (output_path / ch['split_file']).read_text(encoding='utf-8', newline='')
        if ch.get('is_virtual'):
            orig = ch.get('original_id')
            order = ch.get('order', 0)
            logical.setdefault(orig, []).append((order, text))
        else:
            logical.setdefault(cid, []).append((0, text))
    
    merged = []
    for key, parts in logical.items():
        parts.sort(key=lambda x: x[0])
        merged.append((key, "".join(t for _, t in parts)))
    
    result_parts, total = [], 0
    for key, text in reversed(merged):
        if total + len(text) > overlap_limit and result_parts:
            break
        result_parts.insert(0, text)
        total += len(text)
        if len(result_parts) >= 3:
            break
    return "\n\n".join(result_parts)

def _build_diagnose_prompt(prev_context, batch_content, with_json=False):
    text = f"""你是一位拥有二十年经验的资深小说主编。你的任务是对输入的章节进行精准的"局部诊断"。
请阅读用户提供的【当前章节正文】，结合【前情提要】，输出以下三个维度的结构化诊断报告。
1. 章节摘要（逐章列出，每章150字以内）
2. 人物状态（主角及关键配角在本批次中的状态变化）
3. 断层诊断（指出这批章节之间，以及开头与上一段前情之间，是否存在情节跳跃、逻辑不连贯或动机突兀的问题。如果有，给出一句话的修改建议。如果没问题，直接写"逻辑连贯"。）

【前情提要】
{prev_context}

【当前章节正文】
{batch_content}
"""
    if not with_json:
        return text
    # P1.4：追加结构化 JSON 契约，供 fix_gaps 消费；流式返回纯文本，故要求独立 ```json 代码块
    text += """

【结构化诊断（本批次必须输出）】
请在报告末尾另起一段，输出一个可直接解析的 JSON 代码块（用 ```json 包裹，只保留一个），字段契约：
{
  "summary": "本批次章节整体摘要（150字内）",
  "gaps": [
    {
      "type": "plot_jump 或 logic_inconsistency 或 motivation_abrupt",
      "severity": "high 或 medium 或 low",
      "location": "断层所在位置（第几章 / 开头 / 结尾）",
      "description": "一句话描述断层",
      "suggestion": "一句话修改建议"
    }
  ]
}
若本批次无断层，gaps 为空数组 []。
"""
    return text


def _parse_diag_json(result: str, batch_id: int) -> dict | None:
    """P1.4：从流式纯文本中抽取 ```json 代码块并解析；失败回退查找顶层 {...}。
    返回结构化诊断对象；解析失败返回 None（可选能力，不阻断流水线）。
    同时为每个断层补稳定的 id（B{batch_id}G{i}），供 fix_gaps 的 fix_list 引用。"""
    candidate = result
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", result, re.IGNORECASE)
    if m:
        candidate = m.group(1).strip()
    try:
        start = candidate.index("{")
        end = candidate.rindex("}")
        obj = json.loads(candidate[start:end + 1])
    except (ValueError, json.JSONDecodeError):
        logger.warning(f"[phase1] batch {batch_id} 诊断 JSON 解析失败")
        return None
    if not isinstance(obj, dict):
        return None
    gaps = obj.get("gaps")
    if not isinstance(gaps, list):
        gaps = []
    normalized = []
    for i, g in enumerate(gaps):
        if not isinstance(g, dict):
            continue
        g["id"] = f"B{batch_id}G{i + 1}"
        normalized.append(g)
    obj["batch_id"] = batch_id
    obj["gaps"] = normalized
    return obj

async def _wait_pause(progress, progress_path):
    while not state.pause_event.is_set():
        if state.stop_requested: raise asyncio.CancelledError()
        progress["last_heartbeat"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        async with state.progress_lock:
            atomic_write_json(progress_path, progress)
        await asyncio.sleep(1.0)
