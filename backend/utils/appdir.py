"""应用数据目录的唯一数据源。

为什么要有这个模块：
  历史、日志、端口文件散落在 4 个模块里各自拼 %APPDATA%\\TextForge，
  Rust 外壳侧还把同一套拼接逻辑**又抄了一遍**（backend.rs）。改一处必漏另一处，
  且两边一旦不一致，前端就永远读不到端口文件。

为什么便携版要改：
  装到 Program Files 时程序目录只读，可变状态放 %APPDATA% 是 Windows 惯例；
  但便携包解压到哪，数据就该跟到哪 —— 否则「整个文件夹拷走即用」做不到，
  且卸载/换目录会丢历史。外壳显式设 TEXTFORGE_DATA_DIR=<exe目录>/data，
  便携包因此完全自包含。

**默认仍是 %APPDATA%**（不靠「猜测 exe 目录是否可写」）：
  猜可写性会让单元测试把数据写进 venv/Scripts，彻底搞乱测试隔离；
  显式注入则让默认值行为稳定，只有打包形态才切换到便携目录。
"""
import os
from pathlib import Path

# 外壳（Tauri）用它把数据目录传给 sidecar；未设置时退回 Windows 惯例。
ENV_KEY = "TEXTFORGE_DATA_DIR"


def data_dir() -> Path:
    """返回应用数据目录（不保证已创建）。每次调用都重新解析。

    刻意做成函数而非模块级常量：测试会在运行期改环境变量做隔离，
    模块级缓存会把第一次解析的结果钉死。
    """
    override = os.environ.get(ENV_KEY)
    if override:
        return Path(override)
    base = os.environ.get("APPDATA")
    if base:
        return Path(base) / "TextForge"
    return Path.home() / ".textforge"


def ensure_data_dir() -> Path:
    """返回数据目录并确保存在。写文件前一律用这个。"""
    d = data_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        # 目录不可写（如放进只读 U 盘或 Program Files）：退回 %APPDATA%，
        # 保证程序仍可运行，只是便携的自包含性降级为「部分」。
        base = os.environ.get("APPDATA")
        fallback = (Path(base) / "TextForge") if base else (Path.home() / ".textforge")
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback
    return d