import logging
from pathlib import Path
from core.budget import blueprint_chars
from utils.atomic import atomic_write_text, atomic_write_json
from routes import state
from routes.events import sse_emit
from core.prompt_registry import PROMPT_VERSION, stage_versions

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
            # 必须先 clear 再推送：否则用户在 SSE→HTTP 往返的这几毫秒里点下确认，
            # 动作会被紧随其后的 clear() 抹掉，流水线永远卡在闸门上（必现竞态）。
            state.blueprint_confirmed.clear()
            sse_emit("blueprint_ready", {"blueprint": existing_text, "resumed": True}, run_id)
            logger.info("[phase2] 检测到待确认蓝图，恢复等待用户确认")
            await state.wait_gate(
                state.blueprint_confirmed, gate=state.GATE_BLUEPRINT,
                progress=progress, progress_path=progress_path, tick=1.0)
            await state.absorb_gate_result(progress, progress_path, ['blueprint_confirmed', 'blueprint_user_edited'])
            progress['current_phase'] = 'phase3-1'
            async with state.progress_lock:
                atomic_write_json(progress_path, progress)
            return

    # 提示词版本随 run 落盘：续跑时若与上次不同，_config_drift 会按阶段提示
    # 「前几批用旧提示词、后几批用新提示词」，而不是静默混用。
    progress['prompt_version'] = PROMPT_VERSION
    progress['prompt_versions'] = stage_versions()

    # 蓝图上限与重构预留同源（core.budget.blueprint_chars）：
    # 两处各写一份时，「生成 5000 字」与「按另一套算式预留」会静默错配。
    blueprint_max = blueprint_chars(req.context_window)

    from core.blueprint import build_blueprint_context
    from core.api_client import call_with_retry, make_httpx_client, pick_endpoint
    ep = pick_endpoint(req, "blueprint")
    async with make_httpx_client(req, api_url=ep["api_url"], api_key=ep["api_key"]) as client:
        async def map_call(prompt_text: str, target_chars: int) -> str:
            # 分卷压缩不往前端推流：用户要看的是最终蓝图，不是中间产物
            payload = {"model": ep["model"], "messages": [{"role": "user", "content": prompt_text}],
                "temperature": req.temperatures.blueprint,
                "max_tokens": max(256, int(target_chars * 2)), "stream": True}
            map_result, map_usage, _ = await call_with_retry(
                client, ep["api_url"], payload, None, run_id)
            _add_usage(progress, map_usage)
            return map_result

        all_summaries, _ctx_meta = await build_blueprint_context(
            output_path=output_path, req=req, progress=progress, map_call=map_call,
            prompt_version=PROMPT_VERSION, model=ep["model"],
            emit=lambda ev, data: sse_emit(ev, data, run_id),
        )

        prompt = f"""你是一位顶尖的小说架构师和文学总编。请将这些碎片信息汇总，生成一份高度凝练的"全流程优化建议书"。
这份建议书将作为后续AI重构的系统提示词，因此必须控制在 {blueprint_max} 字以内。
请输出以下四个部分：
1. 全书主线脉络（起承转合四大阶段的核心剧情走向）
2. 主角人物弧光（主角的性格起点、关键转变节点、最终归宿）
3. 核心逻辑漏洞与缝合方案（列出最需要修复的3-5个跨章节逻辑断层）
4. 全书基调与重构方向（文风、节奏、去AI味的具体要求）
只依据下面提供的材料，不要编造材料中没有的情节或设定。

【作者特殊风格要求】
{req.author_style}

【全书章节摘要（按时间顺序；如为分卷脉络，仍覆盖全书首尾）】
{all_summaries}
"""
        payload = {"model": ep["model"], "messages": [{"role": "user", "content": prompt}],
            "temperature": req.temperatures.blueprint, "max_tokens": blueprint_max * 2, "stream": True}
        result, usage, _ = await call_with_retry(client, ep["api_url"], payload, sse_emit, run_id)
        _add_usage(progress, usage)
    
    atomic_write_text(story_bible, result)
    # 顺序至关重要：**先清标志，再落盘 phase2_waiting**。
    # 反过来就有一个窗口：progress 已经是 phase2_waiting（界面/状态都认为闸门开了），
    # 但标志还没 clear，此刻到达的确认会被紧接着的 clear() 静默抹掉 ——
    # 流水线随后永远等不到，用户界面却显示「已确认」，无任何报错。
    # 调整后：任何在「闸门可见」之后到达的确认都不会再被清掉。
    state.blueprint_confirmed.clear()
    progress['current_phase'] = 'phase2_waiting'
    async with state.progress_lock:
        atomic_write_json(progress_path, progress)
    # 同步重置暂停标志：重试失败或用户已停止的场景下，若不清会立刻被 pause 打断
    sse_emit("blueprint_ready", {"blueprint": result, "max_chars": blueprint_max}, run_id)
    logger.info("[phase2] 蓝图已生成，等待用户确认")
    await state.wait_gate(
        state.blueprint_confirmed, gate=state.GATE_BLUEPRINT,
        progress=progress, progress_path=progress_path, tick=1.0)
    # 用户是否编辑过蓝图由控制端点写入，这里并回内存
    await state.absorb_gate_result(progress, progress_path, ['blueprint_confirmed', 'blueprint_user_edited'])
    blueprint_text = story_bible.read_text(encoding='utf-8')
    if len(blueprint_text) < 100:
        raise RuntimeError("蓝图过短，可能生成失败")
    progress['current_phase'] = 'phase3-1'
    async with state.progress_lock:
        atomic_write_json(progress_path, progress)


def _add_usage(progress: dict, usage: dict | None) -> None:
    """把本次调用消耗并进 progress.usage（phase2 原先完全没记账）。"""
    if not isinstance(usage, dict):
        return
    acc = progress.setdefault(
        "usage", {"prompt_tokens": 0, "completion_tokens": 0, "estimated": False}
    )
    for key in ("prompt_tokens", "completion_tokens"):
        try:
            acc[key] = int(acc.get(key, 0) or 0) + int(usage.get(key, 0) or 0)
        except (TypeError, ValueError):
            pass
