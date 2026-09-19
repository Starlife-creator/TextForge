"""路径规范化与安全校验（铁律 4：os.path.normcase(Path.resolve()) 规范化比较）。"""
import os
from pathlib import Path
from urllib.parse import urlparse

class PathValidationError(ValueError):
    """输入路径/输出路径/URL 校验失败。"""

def _norm(p: Path) -> str:
    return os.path.normcase(str(p.resolve()))

def validate_request_paths(req) -> tuple[Path, Path]:
    """校验 StartRequest 的输入/输出路径与 api_url。
    返回规范化后的 (input_path, output_path)。失败抛 PathValidationError。"""
    if not req.input_path or not req.output_path:
        raise PathValidationError("输入路径和输出路径均不能为空")

    input_path = Path(req.input_path).expanduser()
    output_path = Path(req.output_path).expanduser()

    # 输入存在性与类型
    if not input_path.exists():
        raise PathValidationError(f"输入路径不存在：{input_path}")
    if req.input_mode == "single_file":
        if not input_path.is_file():
            raise PathValidationError(f"单文件模式需要一个文件：{input_path}")
        ext = input_path.suffix[1:].lower()
        if ext not in ("txt", "md", "docx"):
            raise PathValidationError(f"不支持的输入文件类型：.{ext}（仅支持 txt/md/docx）")
        if ext != req.input_format:
            raise PathValidationError(
                f"输入格式选择({req.input_format})与文件扩展名(.{ext})不一致")
    else:
        if not input_path.is_dir():
            raise PathValidationError(f"多文件模式需要一个文件夹：{input_path}")
        doc_files = [f for f in input_path.iterdir()
                     if f.is_file() and f.suffix[1:].lower() in ("txt", "md", "docx")]
        if not doc_files:
            raise PathValidationError("输入文件夹中没有 txt/md/docx 文档")

    # 输出目录：规范化比较，防止多文件模式输出写进输入目录造成递归污染
    norm_input = _norm(input_path)
    # 多文件模式输入是目录；单文件模式以其所在目录作为比较基准
    input_base = input_path if req.input_mode == "multi_file" else input_path.parent
    norm_base = _norm(input_base)
    try:
        output_path.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise PathValidationError(f"输出目录无法创建：{output_path}（{e}）")
    norm_output = _norm(output_path)

    if norm_output == norm_base:
        raise PathValidationError("输出目录不能与输入目录相同，避免覆盖原始稿件")
    if norm_output.startswith(norm_base + os.sep):
        raise PathValidationError("输出目录不能位于输入目录内部，避免下次运行时把成品当原稿扫描")

    # 输出目录可写探测
    probe = output_path / ".textforge_write_probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError as e:
        raise PathValidationError(f"输出目录不可写：{output_path}（{e}）")

    # api_url 基本合法性
    parsed = urlparse(req.api_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise PathValidationError(f"api_url 不合法：{req.api_url}")

    if not req.model or not req.model.strip():
        raise PathValidationError("模型名不能为空")

    return input_path, output_path
