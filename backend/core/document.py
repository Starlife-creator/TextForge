import re
from docx import Document
from pathlib import Path
from typing import Any
import tempfile, os, logging

logger = logging.getLogger("textforge")

# Markdown 标题与小说章标题。富文本模式只把这些行升级为标题样式，
# 不改变任何正文文字；识别失败时仍按普通段落输出。
_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_CHAPTER_TITLE_RE = re.compile(
    r"^\s*(?:"
    r"第[0-9零一二三四五六七八九十百千万两]+[章节回卷部篇集折]"
    r"|序章|楔子|引子|序幕|尾声|终章|尾章|终篇|番外|外传|间章|后记|跋"
    r"|chapter\s*\d+"
    r"|\d{1,3}[.、．]\s*\S"
    r")",
    re.IGNORECASE,
)
_INLINE_RE = re.compile(r"(\*\*[^*\n]+\*\*|__[^_\n]+__|\*[^*\n]+\*|_[^_\n]+_)")


def _add_inline(p, text: str) -> None:
    """渲染 **粗体** / *斜体*；其余文字原样写入。"""
    for part in _INLINE_RE.split(text):
        if not part:
            continue
        bold = italic = False
        if (part.startswith("**") and part.endswith("**")) or (
            part.startswith("__") and part.endswith("__")
        ):
            bold, part = True, part[2:-2]
        elif (part.startswith("*") and part.endswith("*")) or (
            part.startswith("_") and part.endswith("_")
        ):
            italic, part = True, part[1:-1]
        if part:
            run = p.add_run(part)
            run.bold = bold or None
            run.italic = italic or None


def _append_rich_chapter(doc: Any, text: str) -> None:
    """按空行分段、识别标题与粗斜体，追加一章内容。"""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    buf: list[str] = []
    first_nonempty = True

    def flush() -> None:
        nonlocal buf
        chunk = "\n".join(buf).strip("\n")
        buf = []
        if not chunk.strip():
            return
        p = doc.add_paragraph()
        for i, line in enumerate(chunk.split("\n")):
            if i:
                p.add_run().add_break()
            _add_inline(p, line)

    for line in lines:
        m = _HEADING_RE.match(line)
        if m:
            flush()
            doc.add_heading(m.group(2).strip(), level=min(3, len(m.group(1))))
            first_nonempty = False
            continue
        if not line.strip():
            flush()
            continue
        if first_nonempty:
            first_nonempty = False
            # 标题行通常很短；加长度限制，避免把「1. 他推开门……」这类正文误判成章节标题
            if len(line.strip()) <= 60 and _CHAPTER_TITLE_RE.match(line):
                flush()
                # 章标题统一用一级标题，便于 Word 导航窗格按章跳转
                doc.add_heading(line.strip(), level=1)
                continue
        buf.append(line)
    flush()


def _save_docx(doc: Any, output_path: str) -> None:
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=out.parent, prefix=out.stem + "_", suffix=".docx")
    try:
        # 原实现先 os.close(fd) 再 doc.save(tmp)：fdopen 失败时 fd 泄漏，
        # 且 Windows 上对已关闭句柄 unlink 会失败，留下孤儿临时文件
        with os.fdopen(fd, "wb") as f:
            doc.save(f)
        from utils.atomic import replace_with_retry
        replace_with_retry(tmp, out)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass
        raise


def write_text_docx(text: str, output_path: str, *, rich: bool = False,
                    title: str | None = None):
    """普通 docx 输出；rich=True 时启用标题/粗斜体渲染。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if rich:
        doc = Document()
        if title:
            doc.core_properties.title = title
        _append_rich_chapter(doc, text)
        _save_docx(doc, output_path)
        return

    doc = Document()
    written = 0
    for para in text.split("\n\n"):
        para = para.strip("\n")
        # 跳过全空段："\n\n\n\n".split("\n\n") 会产出 ['', '']，照写就是空段落
        if not para.strip():
            continue
        p = doc.add_paragraph()
        lines = para.split("\n")
        for i, line in enumerate(lines):
            # 原实现每行末尾都 add_break()，导致每个段落结尾多出一个空行；
            # 改成「行与行之间」才换行，段落尾部不再拖尾
            if i:
                p.add_run().add_break()
            if line:
                p.add_run(line)
        written += 1
    if written == 0:
        # 跳过空段之后，全空白输入会产出一个零段落的空 docx，必须留痕
        logger.warning(f"[docx] 输入全为空白，生成的是空文档: {output_path}")
    _save_docx(doc, output_path)


def write_chapters_docx(texts: list[str], output_path: str, *, rich: bool = False,
                        title: str | None = None):
    """按章输出 docx。富文本模式下每章另起一页；普通模式保持旧的 --- 分隔。"""
    if not rich:
        write_text_docx("\n\n---\n\n".join(texts), output_path)
        return
    doc = Document()
    if title:
        doc.core_properties.title = title
    for i, text in enumerate(texts):
        _append_rich_chapter(doc, text)
        if i < len(texts) - 1:
            doc.add_page_break()
    _save_docx(doc, output_path)
