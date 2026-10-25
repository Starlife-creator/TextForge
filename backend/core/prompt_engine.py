"""#7 提示词模板加载注册表（对外统一入口）。

历史：v8.8 提示词内联在 splitter / pipeline / refactor / stitch，本文件曾是空 stub。
现在它是注册表加载器：按阶段取版本条目；后续内联正文逐条外置为模板文件后，
由本模块统一加载，模板缺失即 fail-fast，不再静默退回内联旧文本。

单一事实源在 `core.prompt_registry`，本模块只做对外 re-export 与启动校验入口。
"""

from core.prompt_registry import (
    PROMPT_VERSION,
    REGISTRY,
    STAGES,
    get_prompt_entry,
    get_prompt_version,
    load_registry,
    stage_versions,
)

__all__ = [
    "PROMPT_VERSION",
    "REGISTRY",
    "STAGES",
    "get_prompt_entry",
    "get_prompt_version",
    "load_registry",
    "stage_versions",
]
