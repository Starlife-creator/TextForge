from pydantic import BaseModel, Field
from typing import Literal, Optional, List, Dict

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
    context_window: int = Field(ge=8000)
    author_style: str = "出版级文学重构，文笔凝练，注重画面感"
    novel_name: Optional[str] = None
    rich_text: bool = False
    ssl_verify: bool = True
    proxy: Optional[str] = None
    temperatures: Temperatures = Temperatures()

    # ---- 优化方案增量字段（默认值 = 旧版行为，无字段即 full_rewrite + 门全关） ----
    # P1.1 重构模式：缺省 full_rewrite（= 原「全部重构」极端档，行为不变）
    refactor_mode: Literal["full_rewrite", "fidelity", "fix_gaps", "reskin"] = "full_rewrite"
    # P1 门开关：缺省全关（不改变旧行为）
    refactor_gates: Dict[str, bool] = Field(default_factory=lambda: {
        "lock_names": False, "lock_plot": False, "inject_forbidden": False,
        "require_chapter_accept": False, "skip_stitch_if_smooth": False,
        "qc_block_export": False,
    })
    # P0.2 可选的禁改清单（仅非 full_rewrite 注入）
    forbidden_canon: List[str] = Field(default_factory=list)
    # P1.3 reskin 人名/设定映射表 {旧名: 新名}
    name_map: Dict[str, str] = Field(default_factory=dict)
    # P1.5 fix_gaps 时勾选的断层清单（诊断输出的断层 id/描述）
    fix_list: List[str] = Field(default_factory=list)
    # P1.4 诊断输出路径（可选，与 batch_XX.txt 并存）
    diagnose_json: Optional[str] = None
    # P4.1 分阶段模型，缺省 = 单一 model
    models: Dict[str, str] = Field(default_factory=dict)
