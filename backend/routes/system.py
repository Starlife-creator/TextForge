"""系统 / 后端自身配置的可视化接口。

背景：此前端口、日志、重启、模型端点探测全都没有任何 UI 入口，
用户只能靠改文件 + 看 console 排查。这里把「后端自身」和「模型服务」
两类配置全部暴露为 HTTP 接口，供前端「系统与后端设置」面板调用。
"""
import os, sys, time, platform, json, asyncio, subprocess
from pathlib import Path
from fastapi import APIRouter, HTTPException, Body, Query
import httpx
from routes import state
from utils.appdir import data_dir
import logging

router = APIRouter()
logger = logging.getLogger("textforge")

START_TIME = time.time()
VERSION = "8.8"


def _app_paths() -> dict:
    """数据目录下的固定布局。走 appdir，与日志/历史/端口文件同源。"""
    d = data_dir()
    log_dir = d / "logs"
    return {"APP_DIR": d, "LOG_DIR": log_dir,
            "LOG_PATH": log_dir / "backend.log", "PORT_FILE": d / "port.json"}


def __getattr__(name):
    """按需解析 APP_DIR/LOG_DIR/LOG_PATH/PORT_FILE（PEP 562）。

    不做成 import 期常量：logging_setup 是在调用时才解析目录的，若这里提前
    定死，两边会在测试改 APPDATA 或便携包注入 TEXTFORGE_DATA_DIR 时**算出
    不同路径**，界面显示的日志路径就会指向一个永远不存在的文件。
    """
    paths = _app_paths()
    if name in paths:
        return paths[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _port_file() -> dict:
    """读取 port.json（一次），供 _port/_pid/system_info 共用，避免每个请求读多遍。"""
    try:
        data = json.loads(PORT_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _port() -> int:
    try:
        return int(_port_file().get("port") or 0)
    except (TypeError, ValueError):
        return 0


def _pid() -> int:
    try:
        return int(_port_file().get("pid") or 0)
    except (TypeError, ValueError):
        return 0


# ---------- 后端自身 ----------

@router.get("/api/system/info")
async def system_info():
    """后端运行信息：端口 / PID / 运行时长 / 关键文件位置。"""
    try:
        st = LOG_PATH.stat()
        log_size, log_mtime, log_exists = st.st_size, st.st_mtime, True
    except OSError:
        log_size, log_mtime, log_exists = 0, 0, False
    pf = _port_file()
    return {
        "version": VERSION,
        "pid": os.getpid(),
        "port": int(pf.get("port") or 0),
        "started_at": START_TIME,
        "uptime_sec": round(time.time() - START_TIME, 1),
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "cwd": os.getcwd(),
        "platform": f"{platform.system()} {platform.release()}",
        "debug": os.environ.get("TEXTFORGE_DEBUG", "0") == "1",
        "app_dir": str(APP_DIR),
        "port_file": str(PORT_FILE),
        "port_file_exists": PORT_FILE.exists(),
        "log_path": str(LOG_PATH),
        "log_exists": log_exists,
        "log_size": log_size,
        "log_mtime": log_mtime,
        "pipeline_running": state.pipeline_running,
        "paused": not state.pause_event.is_set(),
        "run_id": state.current_run_id,
        "output_path": state.current_output_path,
        "subscribers": {k: len(v) for k, v in _sub_counts().items()},
    }


def _sub_counts():
    from routes.events import _subscribers
    return dict(_subscribers)


@router.get("/api/system/logs")
async def system_logs(lines: int = Query(200, ge=10, le=5000), offset: int = Query(0, ge=0)):
    """按行增量读取 backend.log。

    offset 是**已消费行数**（从文件头数起）：
      offset=0 → 从头取 lines 行（首次加载 / 重新加载）
      offset=N → 只返回第 N 行之后的增量
    """
    if not LOG_PATH.exists():
        return {"path": str(LOG_PATH), "lines": [], "total": 0, "offset": 0, "exists": False}
    try:
        # 10MB 上限，直接全量读入内存后切片；日志量级下开销可忽略
        raw = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        raise HTTPException(500, f"读取日志失败: {e}")
    total = len(raw)
    # 注意：这里必须是 min(offset, total)。写成 total - offset 时，
    # offset=0（首次加载）会得到 start=total → 返回空数组，日志面板永远显示「暂无日志」。
    start = min(offset, total)
    return {
        "path": str(LOG_PATH),
        "lines": raw[start:start + lines],
        "total": total,
        "offset": start + min(len(raw) - start, lines),
        "size": LOG_PATH.stat().st_size,
        "exists": True,
    }


@router.post("/api/system/open")
async def system_open(payload: dict = Body(...)):
    """在系统文件管理器打开 TextForge 目录或日志文件。"""
    target = payload.get("target") or "app_dir"
    if target == "app_dir":
        p = APP_DIR
    elif target == "logs_dir":
        p = LOG_DIR
    elif target == "log_file":
        p = LOG_PATH
    else:
        raise HTTPException(422, f"未知 target: {target}")
    if not p.exists():
        raise HTTPException(404, f"路径不存在: {p}")
    try:
        if sys.platform == "win32":
            os.startfile(str(p))          # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])
    except Exception as e:
        raise HTTPException(500, f"打开失败: {e}")
    logger.info(f"[system] 打开 {target}: {p}")
    return {"status": "opened", "path": str(p)}


@router.post("/api/system/shutdown")
async def system_shutdown(payload: dict = Body(default={})):
    """后端自停（供前端「停止后端」/ 重启前清理孤儿进程使用）。
    流水线运行中默认拒绝，避免静默打断正在跑的批次。"""
    force = bool(payload.get("force"))
    if state.pipeline_running and not force:
        raise HTTPException(409, "流水线运行中，请先停止流水线或勾选强制关闭")

    async def _bye():
        logger.info("[system] 收到停机指令，进程即将退出")
        await asyncio.sleep(0.25)
        os._exit(0)

    asyncio.create_task(_bye())
    return {"status": "stopping"}


# ---------- 模型服务 ----------

def _models_candidates(api_url: str) -> list[str]:
    """从「API URL」推导模型列表端点的候选地址（按优先级）。

    用户常把 `/models`（列表端点）误填进 API URL —— 那是错的，对话端点才是
    `/chat/completions`。这里两种写法都能反推出正确的候选列表，
    探测时逐个尝试直到某个返回模型数组。
    """
    url = (api_url or "").strip().rstrip("/")
    if not url:
        return []
    cands: list[str] = []
    if url.endswith("/models"):
        cands.append(url)
    else:
        for suffix in ("/chat/completions", "/completions"):
            if url.endswith(suffix):
                cands.append(url[: -len(suffix)] + "/models")
                break
        else:
            cands.append(url + "/models")
    # 看到 /models 收尾说明那是列表端点；看到 /v1 之类前缀时补 /models 也常见
    seen, out = set(), []
    for c in cands:
        if c not in seen:
            seen.add(c); out.append(c)
    return out


@router.post("/api/system/models")
async def system_models(payload: dict = Body(...)):
    """探测模型列表。由后端代发，绕开浏览器 CORS —— LM Studio 默认拒绝带 Origin 的跨源请求。

    api_url/api_key 走 POST body 而非 query string：query 会落进访问日志、浏览器历史、
    Referer 与各种中间代理，等于把密钥写在明信片上（redact 过滤器也管不到 URL）。
    """
    api_url = (payload.get("api_url") or "").strip()
    api_key = (payload.get("api_key") or "").strip()
    if not api_url:
        raise HTTPException(422, "缺少 api_url")
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    tried, errors = [], []
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=8.0, read=15.0, write=8.0, pool=8.0),
                                 follow_redirects=True) as client:
        for cand in _models_candidates(api_url):
            tried.append(cand)
            try:
                resp = await client.get(cand, headers=headers)
            except httpx.HTTPError as e:
                errors.append(f"{cand} → {type(e).__name__}: {e}")
                continue
            if resp.status_code != 200:
                errors.append(f"{cand} → HTTP {resp.status_code}")
                continue
            try:
                data = resp.json()
            except json.JSONDecodeError:
                errors.append(f"{cand} → 响应不是 JSON")
                continue
            models = data.get("data") if isinstance(data, dict) else data
            if not isinstance(models, list):
                errors.append(f"{cand} → 未找到模型数组")
                continue
            names = []
            for m in models:
                if isinstance(m, str):
                    names.append(m)
                elif isinstance(m, dict) and m.get("id"):
                    names.append(str(m["id"]))
            logger.info(f"[system] 模型探测成功 {cand}，共 {len(names)} 个")
            return {"ok": True, "endpoint": cand, "models": names, "tried": tried}
        logger.warning(f"[system] 模型探测失败: {errors}")
        return {"ok": False, "endpoint": None, "models": [], "tried": tried, "errors": errors}


@router.post("/api/system/llm_check")
async def system_llm_check(payload: dict = Body(...)):
    """用最小请求验证「API URL + 模型名」是否真的能对话（流式，max_tokens=8）。"""
    api_url = (payload.get("api_url") or "").strip()
    model = (payload.get("model") or "").strip()
    api_key = (payload.get("api_key") or "").strip()
    proxy = (payload.get("proxy") or "").strip() or None
    ssl_verify = payload.get("ssl_verify", True)
    if not api_url:
        raise HTTPException(422, "缺少 api_url")
    if not model:
        raise HTTPException(422, "缺少 model")
    if not api_url.endswith("/chat/completions"):
        raise HTTPException(422, "API URL 必须是 .../chat/completions 对话端点，不能是 /models 列表端点")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body = {"model": model, "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 8, "stream": False}
    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=45.0, write=10.0, pool=10.0),
                                     verify=bool(ssl_verify), proxy=proxy,
                                     follow_redirects=True) as client:
            resp = await client.post(api_url, json=body, headers=headers)
    except httpx.HTTPError as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "elapsed_ms": round((time.time()-t0)*1000)}
    if resp.status_code != 200:
        return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:300]}",
                "elapsed_ms": round((time.time() - t0) * 1000)}
    text = ""
    model_echo = model
    try:
        data = resp.json()
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
        model_echo = data.get("model") or model
    except (json.JSONDecodeError, KeyError, IndexError, AttributeError):
        text = resp.text[:200]
    logger.info(f"[system] 模型连通性检查 ok model={model}")
    # 顺带探测真实上下文窗口（探不到不影响连通性结论，绝不阻塞）
    ctx = await _detect_context(api_url, model, api_key, proxy, ssl_verify)
    return {"ok": True, "elapsed_ms": round((time.time() - t0) * 1000),
            "reply": text[:120], "model": model_echo,
            "context_limit": ctx["limit"], "context_source": ctx["source"]}


async def _detect_context(api_url, model, api_key, proxy, ssl_verify):
    """真实上下文窗口：先主动探测各厂商端点，再回退到「超限报错里学到」的值。"""
    from core.context_probe import probe_context_limit, observed_limit
    try:
        probe = await probe_context_limit(api_url, model, api_key=api_key or "",
                                          ssl_verify=bool(ssl_verify), proxy=proxy)
    except Exception as e:                       # 探测永远不该影响主流程
        logger.warning(f"[system] 上下文探测异常（忽略）: {e}")
        probe = None
    if probe:
        return {"limit": probe["limit"], "source": probe["source"]}
    learned = observed_limit(api_url, model)
    return {"limit": learned, "source": "overflow" if learned else None}


@router.post("/api/system/context_probe")
async def system_context_probe(payload: dict = Body(...)):
    """探测真实上下文窗口，并与用户配置值对比，给出是否「填大了」的结论。

    仅做自检与提示，绝不阻止用户开始任务 —— 探测失败时如实返回 detected=None。
    api_key 走 body：理由同 system_models（避免密钥进入 URL/日志）。
    """
    api_url = (payload.get("api_url") or "").strip()
    model = (payload.get("model") or "").strip()
    api_key = (payload.get("api_key") or "").strip()
    try:
        configured = int(payload.get("configured") or 0)
    except (TypeError, ValueError):
        configured = 0
    if not api_url:
        raise HTTPException(422, "缺少 api_url")
    ctx = await _detect_context(api_url, model, api_key,
                                None, True)
    detected, source = ctx["limit"], ctx["source"]
    mismatch = None
    if detected and configured and configured > detected:
        mismatch = (f"你设置的上文 {configured} 大于服务端实际窗口 {detected}"
                    f"（来源：{source}）。程序会按实测值自动缩小批次，"
                    f"但建议把「上下文」改成 {detected} 以获得更准确的批次划分。")
    return {"ok": True, "detected": detected, "source": source,
            "configured": configured or None, "mismatch": mismatch}