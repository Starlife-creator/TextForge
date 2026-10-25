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

import json
import threading
from collections import OrderedDict
from pathlib import Path

from utils.atomic import atomic_write_json


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
_progress_cache: OrderedDict[Path, tuple[tuple[int, int], dict]] = OrderedDict()
_CACHE_MAX_ENTRIES = 32


def read_progress_cached(path: Path) -> dict:
    """线程安全的 progress.json 缓存读取。

    缓存键 = 路径；失效依据 mtime + size。供路由层经 asyncio.to_thread 调用，
    避免在事件循环线程里做磁盘读取与 JSON 解析。LRU 上限 32 条，防止长进程
    跨多本书运行时缓存无限增长。
    """
    path = Path(path)
    stat = path.stat()
    stamp = (stat.st_mtime_ns, stat.st_size)
    with _CACHE_LOCK:
        cached = _progress_cache.get(path)
        if cached and cached[0] == stamp:
            _progress_cache.move_to_end(path)
            return cached[1]
    data = read_progress(path)
    with _CACHE_LOCK:
        _progress_cache[path] = (stamp, data)
        _progress_cache.move_to_end(path)
        while len(_progress_cache) > _CACHE_MAX_ENTRIES:
            _progress_cache.popitem(last=False)
    return data


def clear_progress_cache() -> None:
    """测试辅助：清空状态缓存。"""
    with _CACHE_LOCK:
        _progress_cache.clear()