from pydantic import BaseModel, Field
from typing import Literal, Optional

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
