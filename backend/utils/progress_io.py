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
from pathlib import Path


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