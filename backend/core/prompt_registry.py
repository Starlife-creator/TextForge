"""#7 提示词版本注册表骨架：按阶段维护模板条目，替代散落各文件的全局串。

现状：提示词仍内联在 splitter / pipeline / refactor / stitch 等模块，本注册表先把
「阶段 → 版本号 / 更新日期 / 说明 / 来源位置」收口成单一事实源；后续把内联正文逐条
外置为模板文件时，加载器从本注册表取条目，模板缺失即启动失败（fail-fast），不再
静默退回内联旧文本。

兼容：`core.prompt_version.PROMPT_VERSION` 继续由本模块 re-export，既有测试与旧
progress 的全局版本比对不受影响。
"""

from __future__ import annotations

#: 受管阶段顺序；新增阶段必须同时补 REGISTRY 条目，load_registry() 会 fail-fast。
STAGES: tuple[str, ...] = (
    "diagnose",   # phase0 拆分判定 / 断层诊断提示词
    "blueprint",  # 全书蓝图（Story Bible / 分卷压缩汇总）提示词
    "refactor",   # 单章重构与波次质检提示词
    "stitch",     # 跨边界过渡缝合补丁提示词
    "split",      # 拆书预览 / 人工确认后的切割判定
    "regen",      # 单章重生成兜底提示词
)

#: 全局兼容版本号：取蓝图阶段版本，与旧 progress 的 prompt_version 字段口径一致。
PROMPT_VERSION = "2026-10-20.hierarchical-blueprint.1"

REGISTRY: dict[str, dict] = {
    "diagnose": {
        "stage": "diagnose",
        "version": "2026-10-20.diagnose.1",
        "updated": "2026-10-20",
        "description": "章节识别置信度判定与断层诊断提示词（规则识别失败时的 AI 兜底判定）",
        "source": "core/splitter.py; core/chapter_ai.py",
    },
    "blueprint": {
        "stage": "blueprint",
        "version": PROMPT_VERSION,
        "updated": "2026-10-20",
        "description": "全书主线 / 人物弧光 / 断层缝合 / 基调方向的蓝图汇总提示词（hierarchical 分卷压缩）",
        "source": "core/pipeline.py; core/blueprint.py",
    },
    "refactor": {
        "stage": "refactor",
        "version": "2026-10-20.refactor.1",
        "updated": "2026-10-20",
        "description": "单章重构、连续性保持与波次质检提示词",
        "source": "core/refactor.py",
    },
    "stitch": {
        "stage": "stitch",
        "version": "2026-10-20.stitch.1",
        "updated": "2026-10-20",
        "description": "相邻章节边界过渡句缝合补丁提示词（最小改动闸门配合）",
        "source": "core/stitch.py",
    },
    "split": {
        "stage": "split",
        "version": "2026-10-20.split.1",
        "updated": "2026-10-20",
        "description": "拆书预览门确认后的单章切割与自定义标记重拆判定",
        "source": "core/splitter.py; routes/pipeline_runner.py",
    },
    "regen": {
        "stage": "regen",
        "version": "2026-10-20.regen.1",
        "updated": "2026-10-20",
        "description": "质检不通过章节的单章重生成兜底提示词",
        "source": "core/refactor.py",
    },
}


def get_prompt_entry(stage: str) -> dict:
    """取某阶段注册表条目；阶段未登记即 KeyError（fail-fast，不回退内联文本）。"""
    try:
        return REGISTRY[stage]
    except KeyError as exc:
        raise KeyError(f"prompt 注册表缺少阶段条目: {stage!r}") from exc


def get_prompt_version(stage: str) -> str:
    """取某阶段当前版本号。"""
    return get_prompt_entry(stage)["version"]


def stage_versions() -> dict[str, str]:
    """返回 {stage: version} 快照，供 init_progress 落盘与 _config_drift 逐阶段比对。"""
    return {stage: REGISTRY[stage]["version"] for stage in STAGES}


def load_registry() -> dict[str, dict]:
    """启动期校验：六个受管阶段条目齐全；缺任一阶段即 RuntimeError，列出缺失项。

    后续模板外置为文件后，这里同时校验模板文件可读；当前骨架阶段校验先行落地。
    """
    missing = [stage for stage in STAGES if stage not in REGISTRY]
    if missing:
        raise RuntimeError(f"prompt 注册表条目缺失，启动中止: {', '.join(missing)}")
    return REGISTRY
