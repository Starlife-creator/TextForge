import asyncio, json, time, logging
from pathlib import Path
from utils.atomic import atomic_write_text, atomic_write_json
from routes import state
from routes.events import sse_emit

logger = logging.getLogger("textforge")

async def phase2_blueprint(req, progress, progress_path, run_id):
    output_path = Path(req.output_path)
    story_bible = output_path / "Story_Bible.md"
    current_phase = progress.get('current_phase')

    # v8.8 修正：恢复分支 1 —— 蓝图已确认且阶段已推进到 phase3 之后，直接放行，禁止重新生成
    if state.phase_passed(progress, 'phase2_waiting'):
        state.blueprint_confirmed.set()
        sse_emit("phase_done", {"phase": "phase2", "resumed_skip": True}, run_id)
        logger.info(f"[phase2] 阶段已完成（current={current_phase}），恢复跳过")
        return

    # v8.8 修正：恢复分支 2 —— 蓝图已生成但等待确认期间崩溃。复用已有蓝图重新弹窗，不覆盖文件
    if current_phase == 'phase2_waiting' and story_bible.exists():
        existing_text = story_bible.read_text(encoding='utf-8')
        if len(existing_text) >= 100:
            sse_emit("blueprint_ready", {"blueprint": existing_text, "resumed": True}, run_id)
            logger.info("[phase2] 检测到待确认蓝图，恢复等待用户确认")
            state.blueprint_confirmed.clear()
            while not state.blueprint_confirmed.is_set():
                if state.stop_requested: raise asyncio.CancelledError()
                progress["last_heartbeat"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                async with state.progress_lock:
                    atomic_write_json(progress_path, progress)
                await asyncio.sleep(1.0)
            progress['current_phase'] = 'phase3-1'
            async with state.progress_lock:
                atomic_write_json(progress_path, progress)
            return

    summaries_dir = output_path / "01_summaries"
    summary_files = sorted(summaries_dir.glob("batch_*.txt"))
    all_summaries = "\n\n---\n\n".join(f.read_text(encoding='utf-8') for f in summary_files)
    
    chars = req.context_window * 1.5
    max_total = chars * 0.60
    blueprint_max = min(5000, max(2000, int(chars * 0.05)))
    
    if len(all_summaries) > max_total:
        all_summaries = all_summaries[:int(max_total)]
        sse_emit("error", {"kind": "warning", "code": "summaries_truncated",
            "message": f"摘要超长，已截断到 {int(max_total)} 字"}, run_id)
    
    prompt = f"""你是一位顶尖的小说架构师和文学总编。请将这些碎片信息汇总，生成一份高度凝练的"全流程优化建议书"。
这份建议书将作为后续AI重构的系统提示词，因此必须控制在 {blueprint_max} 字以内。
请输出以下四个部分：
1. 全书主线脉络（起承转合四大阶段的核心剧情走向）
2. 主角人物弧光（主角的性格起点、关键转变节点、最终归宿）
3. 核心逻辑漏洞与缝合方案（列出最需要修复的3-5个跨章节逻辑断层）
4. 全书基调与重构方向（文风、节奏、去AI味的具体要求）

【作者特殊风格要求】
{req.author_style}

【所有章节摘要】
{all_summaries}
"""
    
    from core.api_client import call_with_retry, make_httpx_client, pick_model
    async with make_httpx_client(req) as client:
        payload = {"model": pick_model(req, "blueprint"), "messages": [{"role": "user", "content": prompt}],
            "temperature": req.temperatures.blueprint, "max_tokens": blueprint_max * 2, "stream": True}
        result, usage, _ = await call_with_retry(client, req.api_url, payload, sse_emit, run_id)
    
    atomic_write_text(story_bible, result)
    progress['current_phase'] = 'phase2_waiting'
    async with state.progress_lock:
        atomic_write_json(progress_path, progress)
    sse_emit("blueprint_ready", {"blueprint": result, "max_chars": blueprint_max}, run_id)
    
    # blueprint_confirmed 只在此处 clear 一次
    state.blueprint_confirmed.clear()
    while not state.blueprint_confirmed.is_set():
        if state.stop_requested: raise asyncio.CancelledError()
        progress["last_heartbeat"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        async with state.progress_lock:
            atomic_write_json(progress_path, progress)
        await asyncio.sleep(1.0)
    
    blueprint_text = story_bible.read_text(encoding='utf-8')
    if len(blueprint_text) < 100:
        raise RuntimeError("蓝图过短，可能生成失败")
    progress['current_phase'] = 'phase3-1'
    async with state.progress_lock:
        atomic_write_json(progress_path, progress)
