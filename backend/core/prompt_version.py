"""全局提示词版本号（#7：单一事实源在 core.prompt_registry，本模块仅向后兼容 re-export）。

提示词一改，续跑时「前几批用旧提示词、后几批用新提示词」就会造成风格/精度不一致。
写进 progress.json 后由 _config_drift 逐阶段比对并提示用户；只提示，不阻断。
"""

from core.prompt_registry import PROMPT_VERSION, stage_versions  # noqa: F401
