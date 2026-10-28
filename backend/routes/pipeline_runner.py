import asyncio, json, time, logging, hashlib
from pathlib import Path
from routes import state
from routes.events import sse_emit
from core.api_client import FatalAPIError, EmptyContentError, RetryableBusinessError, RateLimitError
from core.splitter import phase0_split, phase1_diagnose
from core.pipeline import phase2_blueprint
from core.refactor import phase3_refactor_stitch
from core.finalize import phase4_finalize
from utils.atomic import atomic_write_json
from utils.redact import redact
from utils.version import VERSION
from core.prompt_registry import PROMPT_VERSION, stage_versions, load_registry

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
    # 心跳只在内存维护（state.last_heartbeat），不再为此重写 progress.json ——
    # 那会在长时段运行里造成上百 GB 的原地覆写。界面从 /api/status 读内存值，
    # 比读盘更准（没有 fsync 延迟），僵死告警不受影响。
    state.last_heartbeat = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    hb_ref: list = []  # 心跳登记历史用的内存引用，_load_or_init_progress 后填入
    hb = asyncio.create_task(_heartbeat_loop(progress_path, hb_ref))
    try:
        # v8.8 修正：断点恢复。若存在未完成的 progress.json，恢复而非覆盖。
        progress = _load_or_init_progress(req, run_id, progress_path)
        hb_ref.append(progress)
        # v8.8：run_start 不携带 total_chapters（phase0 前尚不知道），由 phase0_done 携带
        sse_emit("run_start", {"run_id": run_id, "input_mode": req.input_mode, "resumed": progress["resumed"]}, run_id)
        logger.info(f"[run_pipeline] progress phase={progress['current_phase']} resumed={progress['resumed']}")
        _history_record(progress_path, progress)   # 任务历史：起跑即登记

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
            # 到这里 phase4_finalize 已成功返回，才是真正完成
            progress["completed"] = True
            progress["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            async with state.progress_lock:
                atomic_write_json(progress_path, progress)
            logger.info(f"[run_pipeline] 进度已落盘 finished_at={progress['finished_at']}")
            # 记录历史必须在 done 事件之前，确保前端/测试拉取历史时已是完成态
            _history_record(progress_path, progress)
        except Exception:
            logger.exception("[run_pipeline] 完成前写 progress.json 失败")
        sse_emit("done", {"output_dir": req.output_path, "novel_name": progress["novel_name"]}, run_id)
        logger.info(f"[run_pipeline] 完成 run_id={run_id}")
        await asyncio.sleep(1)
    
    except FatalAPIError as e:
        logger.error(f"[run_pipeline] FatalAPIError kind={e.kind} code={e.code} msg={e.message[:200]}")
        sse_emit("error", {"kind": e.kind, "code": str(e.code), "message": e.message}, run_id)
        await log_error(progress_path, e, progress)
    except EmptyContentError as e:
        logger.error(f"[run_pipeline] EmptyContentError msg={str(e)[:200]}")
        sse_emit("error", {"kind": "empty_content", "code": "empty", "message": str(e)}, run_id)
        await log_error(progress_path, e, progress)
    except RateLimitError as e:
        logger.error(f"[run_pipeline] 429 限流重试用尽 retry_after={e.retry_after}")
        sse_emit("error", {"kind": "rate_limit", "code": 429,
            "message": "API 持续限流（429），请稍后再试或降低并发。"}, run_id)
        await log_error(progress_path, e, progress)
    except RetryableBusinessError as e:
        # v8.8 修正：重试后用尽的业务格式错误，保留进度以便下次恢复
        logger.error(f"[run_pipeline] RetryableBusinessError 重试用尽 msg={str(e)[:300]}")
        sse_emit("error", {"kind": "business", "code": "retry_exhausted", "message": str(e)[:500]}, run_id)
        await log_error(progress_path, e, progress)
    except state.GateReadError as e:
        # 闸门回读失败：进度仍停在闸门处、用户选择仍写在 progress.json 里，
        # 重新开始会原样恢复该弹窗。所以必须说清「可恢复」，而不是抛一句文件系统错误。
        logger.error(f"[run_pipeline] 闸门回读失败 run_id={run_id}: {e}")
        sse_emit("error", {"kind": "gate_read_failed", "code": "progress_unreadable",
                           "message": str(e)}, run_id)
        await log_error(progress_path, e, progress)
    except state.GateTimeoutError as e:
        # #2 闸门超时：事件丢失/前端未弹窗导致的永久挂起由超时兜底，安全暂停并留痕。
        logger.warning(f"[run_pipeline] 闸门超时暂停 run_id={run_id}: {e}")
        sse_emit("gate_timeout", {"gate": progress.get("gate_timeout_gate") if progress else None,
                                  "waiting_seconds": progress.get("gate_wait_seconds") if progress else None,
                                  "message": str(e)}, run_id)
        await log_error(progress_path, e, progress)
    except asyncio.CancelledError:
        logger.info(f"[run_pipeline] 已停止 run_id={run_id}")
        sse_emit("stopped", {"incomplete": True}, run_id)
    except Exception as e:
        logger.exception(f"[run_pipeline] 未捕获异常 run_id={run_id}")
        sse_emit("error", {"kind": "unknown", "code": "exception", "message": str(e)[:500]}, run_id)
        await log_error(progress_path, e, progress)
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
            # 安全：清理内存中的 API Key，避免运行结束后长期留在内存
            state.current_req = None
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
                # 完成态已在 try 块中记录历史；失败/中止态需要在此补记
                if not progress.get("completed"):
                    _history_record(progress_path, progress)
            except Exception:
                logger.exception("[run_pipeline] finally 写 progress.json 失败")


async def _heartbeat_loop(progress_path: Path, progress_ref: list | None = None, interval: float = 15.0):
    """后台心跳：run 在跑就刷新心跳 + 登记任务历史。

    心跳时间戳**只写内存**（state.last_heartbeat），不再重写 progress.json。
    原先每 15s 会把整个 progress.json 读出来、改这一个字段、再全量写回 + fsync；
    而 progress.json 在缝合阶段可达数十 MB（锚点内嵌全文），对磁盘是实打实的
    写放大，对 CPU 则是每 15s 解析+序列化几十 MB JSON。
    last_heartbeat 的唯一用途是前端判断「是否僵死」（pipeline_running 且心跳
    超 90s 则报警），不参与任何恢复决策，因此完全不必落盘 ——
    界面从 /api/status 拿到的是内存值，反而更准（没有 fsync 延迟）。
    阶段推进、批次完成等**真正的状态变化**仍照常写 progress.json。

    历史登记不再重读 progress.json：用 progress_ref（run_pipeline 载入后填入的
    单元素列表）拿内存引用，避免每 15s 全量解析几十 MB JSON 还持锁。

    仅在关键状态变化时写入历史：计算关键字段的签名，仅当签名变化时才落盘，
    避免每 15s 重复写入相同的 runs.json（附带 fsync）。
    """
    import hashlib
    last_sig: str | None = None

    def _history_signature(progress: dict) -> str:
        """计算历史关键字段的签名，仅这些字段变化才触发落盘。"""
        # 用 _meta_from_progress 中的字段，排除 last_error（可能波动）和时间戳
        key_fields = (
            progress.get("current_phase"),
            progress.get("batches_done"),
            progress.get("batches_total"),
            progress.get("finished"),
            progress.get("finished_at"),
            progress.get("chapter_detection"),
            tuple(sorted(progress.get("config_drift", []))),
        )
        return hashlib.md5(json.dumps(key_fields, ensure_ascii=False).encode()).hexdigest()

    while True:
        await asyncio.sleep(interval)
        try:
            state.last_heartbeat = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            if progress_ref:
                sig = _history_signature(progress_ref[0])
                if sig != last_sig:
                    _history_record(progress_path, progress_ref[0])
                    last_sig = sig
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[heartbeat] 写入失败（忽略）: {e}")


def _history_record(progress_path: Path, progress: dict) -> None:
    """把当前进度同步进任务历史注册表（失败绝不影响流水线）。"""
    try:
        from routes.history import record_progress
        record_progress(progress_path, progress)
    except Exception as e:
        logger.warning(f"[history] 记录失败（忽略）: {e}")

def _cfg_brief(value) -> str:
    """把配置值压成一行短文本，避免把整份人名映射/模型表塞进提示。

    P0-1：序列化前先递归脱敏——分阶段模型表 models 可携带 api_key/token，
    否则明文会进入 config_drift 差异文案、progress 与状态回传。
    """
    value = redact(value)
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    else:
        text = str(value)
    return text if len(text) <= 120 else text[:117] + "..."


def _fingerprint(value) -> str:
    """结构指纹：敏感键（api_key/token 等）取 sha256(str(v))[:8]，非敏感键取原值。

    与 _cfg_brief 的区别：brief 先 redact 成 *** 再比，于是「只换密钥不换模型」
    两侧都是 ***、比较相等、漂移漏报。指纹保留敏感键的「不同」（哈希不同即可检出），
    但哈希是单向的——明文既不落盘也不回传，满足 P0-1。
    """
    from utils.redact import _is_sensitive_key
    def walk(v):
        if isinstance(v, dict):
            return {
                k: (hashlib.sha256(str(val).encode("utf-8")).hexdigest()[:8]
                    if _is_sensitive_key(k) else walk(val))
                for k, val in v.items()
            }
        if isinstance(v, list):
            return [walk(x) for x in v]
        return v
    return json.dumps(walk(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _config_drift(existing: dict, req) -> list[str]:
    """续跑时配置是否与上次不同。返回人类可读的差异描述（空列表=一致）。

    必须提示而不是静默沿用：前几批是用旧模型/旧窗口生成的，后几批用新配置，
    会出现「同一本书前后风格/精度不一致」，而用户完全不知情。

    不能只比模型/地址/窗口：重构档位、保护门、禁用设定、人名映射、分阶段模型
    同样会改变后续批次的产出。旧 progress 没有的字段直接跳过，保持兼容。
    """
    old = (existing.get("api_config") or {})
    diffs = []
    for key, cur, label in (
        ("model", req.model, "模型"),
        ("api_url", req.api_url, "API 地址"),
        ("context_window", req.context_window, "上下文窗口"),
    ):
        prev = old.get(key)
        if prev is not None and str(prev) != str(cur):
            diffs.append(f"{label}：{prev} → {cur}")
    if existing.get("input_path") and req.input_path and \
            str(existing["input_path"]) != str(req.input_path):
        diffs.append(f"输入路径已变更（{existing['input_path']} → {req.input_path}）")

    # #7：逐阶段比对提示词版本；新 progress 存 prompt_versions 映射。
    prev_versions = existing.get("prompt_versions") or {}
    current_versions = stage_versions()
    if prev_versions:
        for stage, cur in current_versions.items():
            prev = prev_versions.get(stage)
            if prev is not None and str(prev) != str(cur):
                diffs.append(f"提示词·{stage}：{prev} → {cur}")
    else:
        # 旧 progress 只有全局串：保留兼容比对；没有该字段则跳过（无法判断）。
        prev_pv = existing.get("prompt_version")
        if prev_pv and str(prev_pv) != PROMPT_VERSION:
            diffs.append(f"提示词版本：{prev_pv} → {PROMPT_VERSION}")

    # 其余会影响产出的字段：只比较旧 progress 里确实存在的键
    for key, label in (
        ("input_format", "输入格式"),
        ("input_mode", "输入模式"),
        ("refactor_mode", "重构档位"),
        ("refactor_gates", "保护门设置"),
        ("forbidden_canon", "禁用设定"),
        ("name_map", "人名映射"),
        ("models", "分阶段模型"),
        ("diagnose_json", "断层清单路径"),
        ("max_concurrency", "并发批次数"),
    ):
        if key not in existing:
            continue
        prev, cur = existing.get(key), getattr(req, key, None)
        if _fingerprint(prev) != _fingerprint(cur):
            diffs.append(f"{label}：{_cfg_brief(prev)} → {_cfg_brief(cur)}")
    return diffs


def _load_or_init_progress(req, run_id, progress_path):
    """存在未完成 progress 则恢复，否则新建。

    **读失败绝不能当成"没有进度"**：原本 `except Exception: pass` 会让一次偶发读
    失败（文件被占用 / 杀软扫描 / 崩溃半截文件）直接重建并覆盖用户的进度，
    表现为"重开像什么都没保存"。现在读不了就明确报错并停下，交由用户处理。
    """
    from utils.progress_io import (
        ProgressUnreadable, is_completed, migrate_progress_file,
    )
    existing = None
    if progress_path.exists():
        # 只读一次盘：migrate_progress_file 内部已 json.loads，不再先 try_read_progress
        # 预检一遍数十 MB 文件（#14 消除事件循环里的重复解析）。文件损坏按
        # ProgressUnreadable 上抛，不静默重建。
        try:
            existing, _migrations = migrate_progress_file(progress_path)
        except ProgressUnreadable as e:
            raise RuntimeError(str(e)) from e
    if existing is not None:
        # 旧判据是 current_phase != 'phase4'，但 phase4 在 finalize 之前就写入，
        # 渲染失败时会被误判为「已完成、不可续跑」。现在只认显式 completed。
        if existing.get('current_phase') and not is_completed(existing):
            # 恢复：更新 run_id（匹配本次 SSE），保持工作产物
            existing['run_id'] = run_id
            existing['pipeline_running'] = True
            existing['resumed'] = True
            existing['completed'] = False
            drifts = _config_drift(existing, req)
            if drifts:
                # 记进 progress 供界面展示；不阻断（用户可能是有意换的）。
                # 直接覆盖本次漂移：迁移回填的空列表会挡住 setdefault，旧漂移不应残留。
                existing["config_drift"] = drifts
                logger.warning(f"[run_pipeline] 续跑配置与上次不一致：{drifts}")
            logger.info(f"[run_pipeline] 恢复进度 phase={existing['current_phase']}")
            return existing
    progress = init_progress(req, run_id)
    progress["resumed"] = False
    return progress

async def wait_split_confirm(req, progress, progress_path, run_id):
    """拆书预览门。

    触发条件（任一）：用户显式开启 review_split；或 phase0 的识别置信度不为高
    （未识别到章节 / 用了 AI 兜底 / 章节长度极不均匀等）—— 这种事**必须**让用户
    先看到拆分结果，否则错了要等整本书重跑完才发现。

    在门内支持「按自定义标记重新拆分」：规则和 AI 都搞不定的创新排版，
    用户可以粘贴真实的标题行，原地重拆并重新预览，直到确认。
    """
    det = progress.get('chapter_detection') or {}
    if not (getattr(req, "review_split", False) or det.get("needs_review")):
        return

    def _push_preview():
        chapters = [
            {k: c.get(k) for k in ("id", "filename", "title", "is_empty", "is_virtual")}
            for c in progress.get('chapters', [])
        ]
        # 先 clear 再推送：闸门必须先「上膛」再让用户看到弹窗，否则用户在
        # SSE→HTTP 往返的几毫秒内点确认，动作会被紧随的 clear() 抹掉，流水线永久卡死。
        state.split_confirm.clear()
        state.split_redo.clear()
        sse_emit("split_ready", {"chapters": chapters, "detection": det}, run_id)

    progress['current_phase'] = 'phase0_confirm'
    async with state.progress_lock:
        atomic_write_json(progress_path, progress)
    logger.info(f"[phase0] 拆书预览等待确认 run_id={run_id}（{det.get('reason') or det.get('mode')}）")

    while True:
        _push_preview()
        await state.wait_gate(
            state.split_confirm, alternative=state.split_redo,
            gate=state.GATE_SPLIT, progress=progress, progress_path=progress_path,
            tick=0.5, release_events=(state.split_redo,))
        if state.split_confirm.is_set():
            break
        # 用户要求按自定义标记重新拆分：原地重拆后重新预览（不退出本门）
        markers = list(state.split_redo_markers or [])
        req.chapter_markers = markers
        try:
            from core.splitter import resplit_single_file
            await resplit_single_file(req, progress, progress_path, run_id)
            det = progress.get('chapter_detection') or det
            sse_emit("split_ready", {"chapters": [
                {k: c.get(k) for k in ("id", "filename", "title", "is_empty", "is_virtual")}
                for c in progress.get('chapters', [])], "detection": det, "redone": True}, run_id)
            logger.info(f"[phase0] 已按自定义标记重拆：{len(progress.get('chapters', []))} 章")
        except Exception as e:
            # 重拆失败不终止整轮：报错后继续停在本门，让用户改标记再试
            logger.warning(f"[phase0] 重新拆分失败: {str(e)[:200]}")
            sse_emit("error", {"kind": "warning", "code": "split_redo_failed",
                               "message": str(e)[:300]}, run_id)

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
    await state.wait_gate(
        state.fix_review_confirm, gate=state.GATE_FIX,
        progress=progress, progress_path=progress_path, tick=1.0)
    # 用户勾选的断层清单由控制端点写入 progress.json，这里并回内存（否则会被本循环的写盘覆盖）
    await state.absorb_gate_result(progress, progress_path, ['fix_list'])
    logger.info(f"[fix_review] 已吸收用户勾选 fix_list={progress.get('fix_list')}")
    progress['current_phase'] = 'phase2'
    async with state.progress_lock:
        atomic_write_json(progress_path, progress)

def _collect_diagnosed_gaps(diag_path: str) -> list:
    """读取 diagnose_json 目录下诊断批次文件的 gaps，汇总为前端可选清单。"""
    diag_dir = Path(diag_path)
    gaps: list[dict] = []
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
    # #7：启动期校验注册表条目齐全；缺失即 fail-fast，不静默退回内联旧文本。
    load_registry()
    novel_name = req.novel_name or (
        Path(req.input_path).parent.name if req.input_mode == "single_file" else Path(req.input_path).name
    ) or "重构终稿"
    return {
        "schema_version": VERSION, "prompt_version": PROMPT_VERSION,
        # #7：各阶段实际使用版本随 run 落盘，续跑时按阶段比对漂移。
        "prompt_versions": stage_versions(), "run_id": run_id,
        "input_mode": req.input_mode,
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
        # P0-1：progress.json 不落盘明文 api_key/token；密钥仅留存于本次运行内存。
        "models": redact(getattr(req, "models", {})),
        "max_concurrency": getattr(req, "max_concurrency", 1),
        "auto_rewrite_count": 0,
        "auto_rewritten_batches": [],
        "auto_rewrite_log": [],
        "batch_qc": {},
        "pre_stitch_issues": [],
        "stitch_failures": [],
        "chapters": [], "batches": [], "virtual_chapter_map": [], "cross_batch_virtual": [],
        "stitch_anchors": [], "current_phase": "phase0", "sub_step": None,
        "blueprint_confirmed": False, "blueprint_user_edited": False,
        "current_processing_batch": 0, "processed_batches": [], "completed_files": [],
        "error_logs": [], "resumed": False,
        # 只有 phase4_finalize 成功返回后才置 true；finished_at 只表示「本轮结束」，
        # 失败/停止同样会写，不能拿来判断完成。
        "completed": False,
        # v8.8 缝合幂等游标：已完成 merge 的虚拟章父章 id、已完成批次间缝合的边界 key
        "stitch_virtual_done": [], "stitch_batch_done": [],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "estimated": False},
        "pipeline_running": True, "last_heartbeat": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

async def log_error(progress_path: Path, error, progress: dict | None = None):
    """追加一条错误到 progress.json（并同步到内存 progress）。

    必须与 _heartbeat_loop 共用 progress_lock：不加锁时会出现
    「心跳读到旧值 → log_error 读改写 → 心跳把旧值写回」把 error_logs 抹掉，
    刷新页面后就再也看不到失败原因。

    **必须同时写内存 progress**：run_pipeline 的 finally 会拿内存 progress 整份覆盖
    磁盘（写 pipeline_running/finished_at）。若只写磁盘，那条错误会被紧随其后的
    覆盖抹掉 —— 用户只看到「失败了」却看不到原因，刷新后更是彻底消失。
    """
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "kind": getattr(error, "kind", "unknown"),
        "code": str(getattr(error, "code", "unknown")),
        "message": str(error)[:500],
    }
    if progress is not None:
        progress.setdefault("error_logs", []).append(entry)
        progress["error_logs"] = progress["error_logs"][-100:]
    try:
        async with state.progress_lock:
            from utils.progress_io import read_progress
            disk = read_progress(progress_path)
            disk.setdefault("error_logs", []).append(entry)
            disk["error_logs"] = disk["error_logs"][-100:]
            atomic_write_json(progress_path, disk)
    except Exception as e:
        # 不能静默：写盘失败时 run_pipeline 的 finally 会用内存副本覆盖 progress.json，
        # 届时错误原因会彻底丢失，界面只显示「失败」却无因。
        logger.warning(f"[log_error] 错误事件落盘失败（原因可能丢失）: {e}")
