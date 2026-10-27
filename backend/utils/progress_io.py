"""progress.json 的统一读取。

为什么需要集中读：
原实现散落各处 `json.loads(p.read_text(encoding='utf-8'))`，有两个共同缺陷：

1. **不认 BOM**：用 `utf-8` 读带 BOM 的文件会抛 JSONDecodeError。BOM 可能来自
   用户手工编辑、OneDrive 等同步工具、或外部工具改写。
2. **静默降级**：`_load_or_init_progress` 原本 `except Exception: pass` 后直接
   `init_progress()` —— 于是**任何**一次读失败（文件被占用、杀毒软件扫描、
   崩溃残留的半截文件、磁盘抖动）都会让程序当成"没有进度"，**新建一份并覆盖**，
   用户的断点续跑状态就此消失。这正是"跑了一半重开却像没保存"的成因之一。

约定：
- `try_read_progress`：文件不存在返回 None；**存在但读不了就抛**，
  绝不把"读失败"伪装成"没有进度"。
- 读取一律用 `utf-8-sig`（兼容有无 BOM）。
"""

import copy
import json
import logging
import threading
from collections import OrderedDict
from pathlib import Path

from utils.atomic import atomic_write_json

logger = logging.getLogger(__name__)


class ProgressUnreadable(RuntimeError):
    """progress.json 存在但无法解析/读取。"""


def read_progress(path: Path) -> dict:
    """读取 progress.json；失败抛 ProgressUnreadable（绝不返回空 dict 糊弄）。"""
    try:
        raw = Path(path).read_text(encoding="utf-8-sig")
    except OSError as e:
        raise ProgressUnreadable(f"progress.json 无法读取：{e}") from e
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ProgressUnreadable(
            f"progress.json 格式损坏（第 {e.lineno} 行第 {e.colno} 列）：{e.msg}。"
            f"为避免覆盖你的进度，程序不会自动重建；请修复该文件或改用新的输出目录。") from e
    if not isinstance(data, dict):
        raise ProgressUnreadable("progress.json 顶层结构不是对象，无法解析为进度")
    return data


def try_read_progress(path: Path):
    """文件不存在 → None；存在但读不了 → 抛 ProgressUnreadable。"""
    p = Path(path)
    if not p.exists():
        return None
    return read_progress(p)


def is_completed(progress: dict) -> bool:
    """判断一次 run 是否真正走完了 phase4。

    不能再用 `bool(finished_at)`：finally 在失败/停止时也会写这个时间戳，
    否则历史会把中断任务显示成「已完成」。`completed` 只允许在
    phase4_finalize 成功返回后置 true。

    旧版本 progress 没有该字段：回退到「phase4 + finished_at」以保持兼容。
    旧数据里 phase4 中途失败的情况无法区分，但新 run 一律以显式字段为准。
    """
    if not isinstance(progress, dict):
        return False
    if "completed" in progress:
        return bool(progress.get("completed"))
    return bool(progress.get("finished_at")) and progress.get("current_phase") == "phase4"


# ---------------------------------------------------------------------------
# #4 阻塞 I/O 线程化配套同步原语
#
# 事件循环里原本直接 read_text / json.loads / rmtree / unlink / restore：
# progress.json 可达数十 MB，一次 json.loads 就能在事件循环里阻塞数十毫秒，
# SSE 心跳抖动、pause/stop 端点排队。这里提供纯同步读写函数，路由层用
# asyncio.to_thread 调用，把磁盘与解析开销移出事件循环线程。
# ---------------------------------------------------------------------------

def load_progress_dict(path: Path) -> dict:
    """同步读取 progress.json 的薄封装（供 asyncio.to_thread 调用）。"""
    return read_progress(path)


def save_progress_dict(path: Path, data: dict) -> None:
    """同步原子写 progress.json 的薄封装（供 asyncio.to_thread 调用）。

    写后剔除该路径的缓存条目：write 由 progress_lock 串行化，但读侧缓存若按旧
    mtime/size 命中，会在同一秒快速连写时返回上一版内容。
    """
    atomic_write_json(Path(path), data)
    with _CACHE_LOCK:
        _progress_cache.pop(Path(path), None)


_CACHE_LOCK = threading.Lock()
# 缓存值 = ((mtime_ns, size), 解析后的 dict)；按 mtime_ns + size 双条件失效。
# 命中返回 deepcopy：经缓存的读方（routes/status.py）虽只读，但 deepcopy 让调用方
# 自由改写也不会污染缓存本体，成本约 1ms。
_progress_cache: OrderedDict[Path, tuple[tuple[int, int], dict]] = OrderedDict()
# 缓存总字节软上限：用 stat().st_size（免费）记账，超阈值按 LRU 淘汰最旧条目，
# 而不是「单文件 >4MB 就永不缓存」——后者让最大的书每次轮询都全价解析。
_CACHE_MAX_TOTAL_BYTES = 32 * 1024 * 1024
_cache_bytes = 0


def read_progress_cached(path: Path) -> dict:
    """线程安全的 progress.json 缓存读取。

    缓存键 = 路径；失效依据 mtime + size。LRU 按总字节（stat.st_size）淘汰，
    不做二次 json.dumps 估算大小（那会在 11MB 文件上多花 34ms）。
    """
    path = Path(path)
    st = path.stat()
    stamp = (st.st_mtime_ns, st.st_size)
    with _CACHE_LOCK:
        cached = _progress_cache.get(path)
        if cached and cached[0] == stamp:
            _progress_cache.move_to_end(path)
            return copy.deepcopy(cached[1])
    data = read_progress(path)
    global _cache_bytes
    with _CACHE_LOCK:
        old = _progress_cache.get(path)
        if old:
            _cache_bytes -= old[0][1]
        _progress_cache[path] = (stamp, data)
        _progress_cache.move_to_end(path)
        _cache_bytes += st.st_size
        while _cache_bytes > _CACHE_MAX_TOTAL_BYTES and len(_progress_cache) > 1:
            _k, _v = _progress_cache.popitem(last=False)
            _cache_bytes -= _v[0][1]
    return copy.deepcopy(data)


def clear_progress_cache() -> None:
    """测试辅助：清空状态缓存。"""
    global _cache_bytes
    with _CACHE_LOCK:
        _progress_cache.clear()
        _cache_bytes = 0


# ---------------------------------------------------------------------------
# #17 progress.json 版本迁移
#
# 旧版本 progress 缺字段时，下游代码有的直接索引（KeyError）、有的 setdefault 兜底。
# 这里在续跑入口统一回填默认值并升级 schema 版本；纯函数、幂等、不覆盖已有值。
# 注意：`schema_version` 字段历史上存的是应用版本号字符串，与这里独立的整数
# progress_schema_version 不是一回事，互不替代。
# ---------------------------------------------------------------------------

PROGRESS_SCHEMA_VERSION = 2

# 缺省字段回灌表：key → 默认值。旧文件缺这些键时补回，保证下游直接索引不抛错。
_PROGRESS_DEFAULTS: dict[str, object] = {
    "chapters": [], "batches": [], "batch_qc": {}, "pre_stitch_issues": [],
    "error_logs": [], "regen_count": {}, "accepted_batches": [], "completed_files": [],
    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "estimated": False},
    "models": {}, "config_drift": [], "auto_rewritten_batches": [],
    "auto_rewrite_count": 0, "auto_rewrite_log": [], "stitch_failures": [],
    "virtual_chapter_map": [], "cross_batch_virtual": [], "stitch_anchors": [],
    "stitch_virtual_done": [], "stitch_batch_done": [], "processed_batches": [],
    "current_processing_batch": 0, "fix_list": [], "blueprint_confirmed": False,
    "blueprint_user_edited": False, "completed": False, "refactor_mode": "full_rewrite",
    "refactor_gates": {}, "forbidden_canon": [], "name_map": {},
}


def migrate_progress(data: dict) -> tuple[dict, list[str]]:
    """对内存中的 progress dict 做版本回填；返回 (迁移后 dict, 已应用迁移名列表)。

    已是当前 schema 版本时原样返回、空列表；旧文件缺字段按 `_PROGRESS_DEFAULTS`
    补回，**不覆盖已有值**。

    注意：默认值里含可变对象（list/dict），必须 deepcopy 后再挂进 data ——
    直接引用模块级 `_PROGRESS_DEFAULTS` 会让多本书共享同一对象，A 书 append
    chapters / 改 usage 会污染 B 书（人工确认闸门 accepted_batches 也会因此
    被跨书静默跳过）。
    """
    if not isinstance(data, dict):
        raise ProgressUnreadable("progress 顶层不是对象，无法迁移")
    applied: list[str] = []
    if data.get("progress_schema_version") != PROGRESS_SCHEMA_VERSION:
        applied.append(f"backfill_defaults_v{PROGRESS_SCHEMA_VERSION}")
        for key, default in _PROGRESS_DEFAULTS.items():
            if key not in data or data[key] is None:
                data[key] = copy.deepcopy(default)
        data["progress_schema_version"] = PROGRESS_SCHEMA_VERSION
    # 历史明文一次性清洗：修复前写出的 progress.json 里 models[*].api_key 等
    # 字段是明文落盘的，迁移时就地脱敏，避免跨进程重启后仍从磁盘读回明文。
    scrub_models(data)
    return data, applied


def scrub_models(data: dict) -> None:
    """就地清洗 data['models'] 里的敏感键（历史明文一次性修复）。

    models 形如 {"phase1": {"api_key": "sk-..."}, ...}；递归遍历，把命中的
    敏感键值替换为 '***'。无 models 字段时 no-op。
    """
    models = data.get("models")
    if not isinstance(models, dict):
        return
    from utils.redact import redact  # 延迟导入，避免循环依赖
    data["models"] = redact(models)


def migrate_progress_file(progress_path: Path, *, backup: bool = True
                          ) -> tuple[dict, list[str]]:
    """读盘 → 迁移 → 需要时原子写回并留 `.bak`；返回 (迁移后 dict, 迁移名列表)。

    备份只在确实发生迁移时写；备份失败不阻断迁移（原文件内容已在内存 data 中）。
    """
    path = Path(progress_path)
    raw = path.read_text(encoding="utf-8-sig")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ProgressUnreadable(f"progress.json 格式损坏，无法迁移：{e.msg}") from e
    if not isinstance(data, dict):
        raise ProgressUnreadable("progress.json 顶层结构不是对象，无法迁移")
    data, applied = migrate_progress(data)
    if applied:
        if backup:
            bak = Path(progress_path).with_suffix(Path(progress_path).suffix + ".bak")
            try:
                bak.write_text(raw, encoding="utf-8")
            except OSError:
                logger.warning(f"[progress] 迁移备份写入失败（忽略）: {bak}")
        atomic_write_json(Path(progress_path), data)
    return data, applied