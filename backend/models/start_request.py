from pydantic import BaseModel, Field
from typing import Literal, Optional, List, Dict, Union

class Temperatures(BaseModel):
    diagnose: float = 0.3
    blueprint: float = 0.5
    refactor: float = 0.8
    stitch: float = 0.6

class StartRequest(BaseModel):
    input_mode: Literal["single_file", "multi_file"]
    input_path: str
    output_path: str
    input_format: Literal["docx", "txt", "md"]
    api_url: str
    api_key: str = ""
    model: str
    context_window: int = Field(ge=8000, le=10_000_000)
    author_style: str = "出版级文学重构，文笔凝练，注重画面感"
    novel_name: Optional[str] = None
    rich_text: bool = False
    ssl_verify: bool = True
    proxy: Optional[str] = None
    temperatures: Temperatures = Temperatures()
    # B1 限流参数：批次间串行间隔（秒），避免连续请求触发云端限流。
    # 上限 300s：不封顶的话一次手误（999999）会让流水线在批次之间「活着」地
    # 傻等 11 天，只有 15s 一次的心跳能证明它还活着。
    batch_interval_sec: float = Field(default=2.0, ge=0, le=300)
    # 有界并发：默认 1（串行）。云端可调高；本地小模型并发容易 OOM，不建议。
    max_concurrency: int = Field(default=1, ge=1, le=4)

    # ---- 优化方案增量字段（默认值 = 旧版行为，无字段即 full_rewrite + 门全关） ----
    # P1.1 重构模式：缺省 full_rewrite（= 原「全部重构」极端档，行为不变）
    refactor_mode: Literal["full_rewrite", "fidelity", "fix_gaps", "reskin"] = "full_rewrite"
    # P1 门开关：缺省全关（不改变旧行为）
    # 例外：stitch_minimal_change 默认开。它拒绝的是「整段重写交界处」，
    # 而拒绝后的降级路径与缝合失败完全一致（保留原文交界），所以不会产出更差正文。
    refactor_gates: Dict[str, bool] = Field(default_factory=lambda: {
        "lock_names": False, "lock_plot": False, "inject_forbidden": False,
        "require_chapter_accept": False, "skip_stitch_if_smooth": False,
        "qc_block_export": False, "auto_rewrite_bad_batch": True,
        "stitch_minimal_change": True,
    })
    # P0.2 可选的禁改清单（仅非 full_rewrite 注入）
    forbidden_canon: List[str] = Field(default_factory=list, max_length=5000)
    # P1.3 reskin 人名/设定映射表 {旧名: 新名}
    name_map: Dict[str, str] = Field(default_factory=dict)
    # P1.5 fix_gaps 时勾选的断层清单（诊断输出的断层 id/描述）
    fix_list: List[str] = Field(default_factory=list, max_length=5000)
    # P1.4 诊断输出路径（可选，与 batch_XX.txt 并存）
    diagnose_json: Optional[str] = None
    # P1.6 左右对照：phase3 后按章生成「原文 | 重构后」Markdown 对照（默认关）
    compare_output: bool = False
    # #20 可选 EPUB 导出：成品目录额外产出 .epub（导航目录 + 粗斜体映射），默认关。
    export_epub: bool = False
    # P1.7 章快照：重构初稿(drafts)与缝合终稿(final)按逻辑章快照到
    # 02_workspace/snapshots/（原文在 split 目录，不再重复存）（默认关）
    chapter_snapshots: bool = False
    # P0.3 可选拆书预览门：默认 False（phase0 后直接进 phase1，保持旧行为）
    review_split: bool = False
    # P0.5 拆章增强：
    #   ai_split：规则识别置信度低时，允许调用 LLM 从候选行里挑章节边界（默认开）。
    #     只有低置信才触发，正常小说零额外调用。
    #   chapter_markers：用户自定义的章节标题行（精确匹配），优先级最高，
    #     用于规则与 AI 都搞不定的「创新排版」——这是最终兜底手段。
    ai_split: bool = True
    chapter_markers: List[str] = Field(default_factory=list, max_length=10000)
    # P4.1 分阶段模型：值可为模型名 str，或 dict{model, api_url?, api_key?}（A2 多服务商）
    models: Dict[str, Union[str, Dict[str, str]]] = Field(default_factory=dict)
