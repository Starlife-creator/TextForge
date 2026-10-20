"""提示词版本号。

提示词一改，续跑时「前几批用旧提示词、后几批用新提示词」就会造成风格/精度不一致。
写进 progress.json 后由 _config_drift 比对并提示用户；只提示，不阻断。
"""

PROMPT_VERSION = "2026-10-20.hierarchical-blueprint.1"
