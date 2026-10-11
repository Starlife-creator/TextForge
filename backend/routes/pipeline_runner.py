import asyncio, json, time, logging
from pathlib import Path
from routes import state
from routes.events import sse_emit
from core.api_client import FatalAPIError, EmptyContentError, RetryableBusinessError, RateLimitError
from core.splitter import phase0_split, phase1_diagnose
from core.pipeline import phase2_blueprint
from core.refactor import phase3_refactor_stitch
from core.finalize import phase4_finalize
from utils.atomic import atomic_write_json

logger = logging.getLogger("textforge")

# 说明：phase0..phase4 均为幂等/可恢复。每个 phase 开头判断 current_phase 跳过已完成部分，
# 批次级恢复见各 phase 内对 batch status 的判断。

async def run_pipeline(req, run_id: str):
    state.current_req = req  # A1：单章重生成复用鉴权（仅内存）
    progress_path = Path(req.output_path) / "progress.json"
    progress = None  # v8.8：finally 防护，极端情况下 _load_or_init_progress 之前异常也不引发 NameError
    logger.info(f"[run_pipeline] 开始 run_id={run_id} input={req.input_path}")
    # 统一心跳：原先只有各个「等待循环」在写 last_heartbeat，导致跑 LLM 批次期间
    # 心跳停更，前端 90s 僵尸检测误报「后端假死」，用户会点停止毁掉正在跑的任务。
    # 后台任务在 await 期间持续运行，能覆盖整个流水线生命周期。
    hb = asyncio.create_task(_heartbeat_loop(progress_path))
    try:
        # v8.8 修正：断点恢复。若存在未完成的 progress.json，恢复而非覆盖。
        progress = _load_or_init_progress(req, run_id, progress_path)
        # v8.8：run_start 不携带 total_chapters（phase0 前尚不知道），由 phase0_done 携带
        sse_emit("run_start", {"run_id": run_id, "input_mode": req.input_mode, "resumed": progress["resumed"]}, run_id)
        logger.info(f"[run_pipeline] progress phase={progress['current_phase']} resumed={progress['resumed']}")

        await phase0_split(req, progress, progress_path, run_id)
        if state.stop_requested: raise asyncio.CancelledError()
        await check_pause(run_id)

        # P0.3 可选拆书预览门：review_split=True 时在此等待用户确认拆分
        await wait_split_confirm(req, progress, progress_path, run_id)

        await phase1_diagnose(req, progress, progress_path, run_id)
        if state.stop_requested: raise asyncio.CancelledError()
        await check_pause(run_id)

        # fix_gaps 闭环：仅 diagnose_json 已配且重构模式为 fix_gaps 时，phase1 后等待勾选断层
        await wait_fix_review(req, progress, progress_path, run_id)

        await phase2_blueprint(req, progress, progress_path, run_id)
        await check_pause(run_id)

        await phase3_refactor_stitch(req, progress, progress_path, run_id)
        if state.stop_requested: raise asyncio.CancelledError()

        # phase4 含 docx 渲染与大量文件写盘，放入线程池避免阻塞事件循环（SSE/控制接口保持响应）
        await asyncio.to_thread(phase4_finalize, Path(req.output_path), progress, req)
        # 必须在发 done 之前就复位并落盘：否则前端收到 done 就认为结束，但
        # /api/start 仍会因 pipeline_running=True 拒掉重跑（409），
        # 凭空多出「界面说完成、按钮点不动」的窗口；磁盘状态也会滞后于事件。
        state.pipeline_running = False
        try:
            progress["pipeline_running"] = False
            progress["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            async with state.progress_lock:
                atomic_write_json(progress_path, progress)
            logger.info(f"[run_pipeline] 进度已落盘 finished_at={progress['finished_at']}")
        except Exception:
            logger.exception("[run_pipeline] 完成前写 progress.json 失败")
        sse_emit("done", {"output_dir": req.output_path, "novel_name": progress["novel_name"]}, run_id)
        logger.info(f"[run_pipeline] 完成 run_id={run_id}")
        await asyncio.sleep(1)
    
    except FatalAPIError as e:
        logger.error(f"[run_pipeline] FatalAPIError kind={e.kind} code={e.code} msg={e.message[:200]}")
        sse_emit("error", {"kind": e.kind, "code": str(e.code), "message": e.message}, run_id)
        await log_error(progress_path, e)
    except EmptyContentError as e:
        logger.error(f"[run_pipeline] EmptyContentError msg={str(e)[:200]}")
        sse_emit("error", {"kind": "empty_content", "code": "empty", "message": str(e)}, run_id)
        await log_error(progress_path, e)
    except RateLimitError as e:
        logger.error(f"[run_pipeline] 429 限流重试用尽 retry_after={e.retry_after}")
        sse_emit("error", {"kind": "rate_limit", "code": 429,
            "message": f"API 持续限流（429），请稍后再试或降低并发。"}, run_id)
        await log_error(progress_path, e)
    except RetryableBusinessError as e:
        # v8.8 修正：重试后用尽的业务格式错误，保留进度以便下次恢复
        logger.error(f"[run_pipeline] RetryableBusinessError 重试用尽 msg={str(e)[:300]}")
        sse_emit("error", {"kind": "business", "code": "retry_exhausted", "message": str(e)[:500]}, run_id)
        await log_error(progress_path, e)
    except state.GateReadError as e:
        # 闸门回读失败：进度仍停在闸门处、用户选择仍写在 progress.json 里，
        # 重新开始会原样恢复该弹窗。所以必须说清「可恢复」，而不是抛一句文件系统错误。
        logger.error(f"[run_pipeline] 闸门回读失败 run_id={run_id}: {e}")
        sse_emit("error", {"kind": "gate_read_failed", "code": "progress_unreadable",
                           "message": str(e)}, run_id)
        await log_error(progress_path, e)
    except asyncio.CancelledError:
        logger.info(f"[run_pipeline] 已停止 run_id={run_id}")
        sse_emit("stopped", {"incomplete": True}, run_id)
    except Exception as e:
        logger.exception(f"[run_pipeline] 未捕获异常 run_id={run_id}")
        sse_emit("error", {"kind": "unknown", "code": "exception", "message": str(e)[:500]}, run_id)
        await log_error(progress_path, e)
    finally:
        hb.cancel()
        # 只清理「自己」持有的全局状态。
        # 新 run 可能已经接手（用户在上一轮 done 后 1s 内重跑就会发生），
        # 此时迟到的 finally 会把 current_output_path / pipeline_task 清成 None，
        # 于是新 run 的 pause/stop/blueprint/accept 等全部返回 409 no_active_run，
        # 用户被永久卡在第一个闸门上。
        if state.current_run_id == run_id:
            state.pipeline_running = False   # 幂等；正常路径已在发 done 前复位
            state.current_output_path = None
            state.pipeline_task = None
        else:
            logger.info(f"[run_pipeline] run_id={run_id} 已被更新的 run 接管，跳过全局状态清理")
        # progress.json 始终要写：它属于本轮自己的输出目录
        if progress is not None:
            try:
                progress["pipeline_running"] = False
                progress["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                async with state.progress_lock:
                    atomic_write_json(progress_path, progress)
                logger.info(f"[run_pipeline] 进度已落盘 finished_at={progress['finished_at']}")
            except Exception:
                logger.exception("[run_pipeline] finally 写 progress.json 失败")


async def _heartbeat_loop(progress_path: Path, interval: float = 15.0):
    """后台心跳：整个 run 期间（含 LLM 调用）刷新 last_heartbeat。
    只改这一个键，不碰其它进度字段，避免与各阶段的写盘互相覆盖。

    间隔 15s + 紧凑写 + 不 fsync：progress.json 在缝合阶段可能因锚点内嵌全文而
    达到数十 MB，用 indent=2 + fsync 每 10s 全量重写会在事件循环上造成数十毫秒停顿，
    足以让 SSE 心跳抖动、pause/stop 等控制端点排队。15s 对 90s 死狗看门狗仍有 6 倍余量。
    """
    while True:
        await asyncio.sleep(interval)
        try:
            async with state.progress_lock:
                if not progress_path.exists():
                    continue
                progress = json.loads(progress_path.read_text(encoding='utf-8'))
                progress["last_heartbeat"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                atomic_write_json(progress_path, progress, pretty=False)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[heartbeat] 写入失败（忽略）: {e}")

def _load_or_init_progress(req, run_id, progress_path):
    """v8.8 新增：存在未完成 progress 则恢复，否则新建。"""
    if progress_path.exists():
        try:
            existing = json.loads(progress_path.read_text(encoding='utf-8'))
            if existing.get('current_phase') and existing.get('current_phase') != 'phase4':
                # 恢复：更新 run_id（匹配本次 SSE），保持工作产物
                existing['run_id'] = run_id
                existing['pipeline_running'] = True
                existing['resumed'] = True
                logger.info(f"[run_pipeline] 恢复进度 phase={existing['current_phase']}")
                return existing
        except Exception:
            pass
    progress = init_progress(req, run_id)
    progress["resumed"] = False
    return progress

async def wait_split_confirm(req, progress, progress_path, run_id):
    """P0.3 可选拆书预览门：默认关闭（review_split=False）直接放行，保持旧行为。
    开启时 phase0 后暂停，推送章节预览并等待用户确认后再进 phase1。"""
    if not getattr(req, "review_split", False):
        return
    progress['current_phase'] = 'phase0_confirm'
    async with state.progress_lock:
        atomic_write_json(progress_path, progress)
    chapters = [
        {k: c.get(k) for k in ("id", "filename", "is_empty", "is_virtual")}
        for c in progress['chapters']
    ]
    # 先 clear 再推送：闸门必须先「上膛」再让用户看到弹窗，否则用户在
    # SSE→HTTP 往返的几毫秒内点确认，动作会被紧随的 clear() 抹掉，流水线永久卡死。
    state.split_confirm.clear()
    sse_emit("split_ready", {"chapters": chapters}, run_id)
    logger.info(f"[P0.3] 拆书预览等待确认 run_id={run_id}")
    while not state.split_confirm.is_set():
        if state.stop_requested: raise asyncio.CancelledError()
        await asyncio.sleep(1.0)
    progress['current_phase'] = 'phase1'
    async with state.progress_lock:
        atomic_write_json(progress_path, progress)

async def wait_fix_review(req, progress, progress_path, run_id):
    """fix_gaps 闭环：仅 diagnose_json 已配置且重构模式为 fix_gaps 时，
    phase1 后暂停，推送诊断断层清单并等待用户勾选确认后再进 phase2。
    默认（未配 diagnose_json 或非 fix_gaps）直接放行，保持旧行为。"""
    if not getattr(req, "diagnose_json", None):
        return
    if progress.get('refactor_mode') != 'fix_gaps':
        return
    gaps = _collect_diagnosed_gaps(req.diagnose_json)
    progress['current_phase'] = 'phase1_fixreview'
    async with state.progress_lock:
        atomic_write_json(progress_path, progress)
    # 同 wait_split_confirm：先 clear 再推送，否则确认会被紧随的 clear() 抹掉。
    state.fix_review_confirm.clear()
    sse_emit("fix_ready", {"gaps": gaps, "count": len(gaps)}, run_id)
    logger.info(f"[fix_review] 断层勾选等待确认（{len(gaps)} 项）run_id={run_id}")
    while not state.fix_review_confirm.is_set():
        if state.stop_requested: raise asyncio.CancelledError()
        await asyncio.sleep(1.0)
    # 用户勾选的断层清单由控制端点写入 progress.json，这里并回内存（否则会被本循环的写盘覆盖）
    await state.absorb_gate_result(progress, progress_path, ['fix_list'])
    logger.info(f"[fix_review] 已吸收用户勾选 fix_list={progress.get('fix_list')}")
    progress['current_phase'] = 'phase2'
    async with state.progress_lock:
        atomic_write_json(progress_path, progress)

def _collect_diagnosed_gaps(diag_path: str) -> list:
    """读取 diagnose_json 目录下诊断批次文件的 gaps，汇总为前端可选清单。"""
    diag_dir = Path(diag_path)
    gaps = []
    if not diag_dir.is_dir():
        return gaps
    for f in sorted(diag_dir.glob("diagnose_*.json")):
        try:
            obj = json.loads(f.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            continue
        for g in (obj.get("gaps") if isinstance(obj, dict) else None) or []:
            if isinstance(g, dict):
                gaps.append({
                    "id": g.get("id"), "batch_id": obj.get("batch_id"),
                    "type": g.get("type"), "severity": g.get("severity"),
                    "location": g.get("location"),
                    "description": g.get("description"), "suggestion": g.get("suggestion"),
                })
    return gaps

async def check_pause(run_id):
    """v8.8 修正：暂停状态仅用内存事件，不写 progress.json；进入/退出推事件。"""
    was_paused = not state.pause_event.is_set()
    if was_paused:
        sse_emit("paused", {}, run_id)
    while not state.pause_event.is_set():
        if state.stop_requested: raise asyncio.CancelledError()
        await asyncio.sleep(1.0)
    if was_paused:
        sse_emit("resumed", {}, run_id)

def init_progress(req, run_id):
    novel_name = req.novel_name or (
        Path(req.input_path).parent.name if req.input_mode == "single_file" else Path(req.input_path).name
    ) or "重构终稿"
    return {
        "schema_version": "8.8", "run_id": run_id, "input_mode": req.input_mode,
        "input_format": req.input_format, "input_path": req.input_path, "output_path": req.output_path,
        "api_config": {"api_url": req.api_url, "model": req.model, "context_window": req.context_window},
        "novel_name": novel_name, "rich_text": req.rich_text,
        # 优化方案增量：重构档位与门开关（缺省 full_rewrite，旧 run 无字段即按此恢复）
        "refactor_mode": getattr(req, "refactor_mode", "full_rewrite"),
        "refactor_gates": getattr(req, "refactor_gates", {}),
        "forbidden_canon": getattr(req, "forbidden_canon", []),
        "name_map": getattr(req, "name_map", {}),
        "fix_list": getattr(req, "fix_list", []),
        "diagnose_json": getattr(req, "diagnose_json", None),
        "models": getattr(req, "models", {}),
        "chapters": [], "batches": [], "virtual_chapter_map": [], "cross_batch_virtual": [],
        "stitch_anchors": [], "current_phase": "phase0", "sub_step": None,
        "blueprint_confirmed": False, "blueprint_user_edited": False,
        "current_processing_batch": 0, "processed_batches": [], "completed_files": [],
        "error_logs": [], "resumed": False,
        # v8.8 缝合幂等游标：已完成 merge 的虚拟章父章 id、已完成批次间缝合的边界 key
        "stitch_virtual_done": [], "stitch_batch_done": [],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "estimated": False},
        "pipeline_running": True, "last_heartbeat": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

async def log_error(progress_path: Path, error):
    """追加一条错误到 progress.json。

    必须与 _heartbeat_loop 共用 progress_lock：不加锁时会出现
    「心跳读到旧值 → log_error 读改写 → 心跳把旧值写回」把 error_logs 抹掉，
    刷新页面后就再也看不到失败原因。
    """
    try:
        async with state.progress_lock:
            progress = json.loads(progress_path.read_text(encoding='utf-8'))
            progress.setdefault("error_logs", []).append({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "kind": getattr(error, "kind", "unknown"),
                "code": str(getattr(error, "code", "unknown")),
                "message": str(error)[:500],
            })
            progress["error_logs"] = progress["error_logs"][-100:]
            atomic_write_json(progress_path, progress)
    except Exception:
        pass
