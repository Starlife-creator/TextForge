"""#20 EPUB 导出最小闭环：纯标准库 zipfile 构建 EPUB 3，不依赖 ebooklib。

成员清单：
- `mimetype`（第一个条目、不压缩，EPUB 规范强制）
- `META-INF/container.xml`（指向 content.opf）
- `OEBPS/content.opf`（元数据 + manifest + spine）
- `OEBPS/nav.xhtml`（章节导航目录）
- `OEBPS/chapter_N.xhtml`（逐章正文；Markdown 粗体/斜体映射到 HTML）

空文本章节不进目录与正文；正文先 HTML 转义再做行内格式替换，杜绝注入。
"""

from __future__ import annotations

import html
import re
import uuid
import zipfile
from pathlib import Path

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*([^*\n]+?)\*(?!\*)")
_UNDERLINE_ITALIC_RE = re.compile(r"_([^_\n]+?)_")

# XML 1.0 合法字符：#x9 / #xA / #xD / #x21-#xD7FF / #xE000-#xFFFD。
# 其余控制字符（含 \x00-\x08 \x0b \x0c \x0e-\x1f）会让阅读器解析 chapter.xhtml
# 时抛 ExpatError。OCR / 老 novel 文本里 \x0c（换页）很常见，必须写盘前剥掉。
_XML_ILLEGAL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _sanitize_xml_text(text: str) -> str:
    """剥除 XML 1.0 非法控制字符；保留 \t \n \r。"""
    return _XML_ILLEGAL_RE.sub("", text)


def md_to_html(text: str) -> str:
    """把极简 Markdown 正文转成 HTML 段落；粗体 **x**、斜体 *x* / _x_。

    先整体转义，再替换行内标记；行内换行保留为 <br/>。
    """
    text = _sanitize_xml_text(text or "")
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    blocks: list[str] = []
    for para in paragraphs:
        escaped = html.escape(para)
        escaped = _BOLD_RE.sub(r"<strong>\1</strong>", escaped)
        escaped = _ITALIC_RE.sub(r"<em>\1</em>", escaped)
        escaped = _UNDERLINE_ITALIC_RE.sub(r"<em>\1</em>", escaped)
        escaped = escaped.replace("\n", "<br/>")
        blocks.append(f"<p>{escaped}</p>")
    return "\n".join(blocks)


def _chapter_title(ch: dict, index: int) -> str:
    return str(ch.get("title") or ch.get("filename") or f"第 {index} 章")


def _content_opf(valid: list[dict], title: str, author: str, book_id: str) -> str:
    manifest_items = [
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" '
        'properties="nav"/>'
    ]
    spine_refs: list[str] = []
    for i, _ch in enumerate(valid, start=1):
        cid = f"ch{i}"
        manifest_items.append(
            f'<item id="{cid}" href="chapter_{i}.xhtml" '
            'media-type="application/xhtml+xml"/>'
        )
        spine_refs.append(f'<itemref idref="{cid}"/>')
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
        'unique-identifier="bookid">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        f'    <dc:identifier id="bookid">{html.escape(book_id)}</dc:identifier>\n'
        f'    <dc:title>{html.escape(title)}</dc:title>\n'
        '    <dc:language>zh</dc:language>\n'
        f'    <dc:creator>{html.escape(author)}</dc:creator>\n'
        '  </metadata>\n'
        '  <manifest>\n    ' + "\n    ".join(manifest_items) + "\n  </manifest>\n"
        '  <spine>\n    ' + "\n    ".join(spine_refs) + "\n  </spine>\n"
        '</package>'
    )


def _nav_xhtml(valid: list[dict]) -> str:
    items = []
    for i, ch in enumerate(valid, start=1):
        items.append(
            f'      <li><a href="chapter_{i}.xhtml">'
            f"{html.escape(_chapter_title(ch, i))}</a></li>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:epub="http://www.idpf.org/2007/ops">\n'
        "  <head><title>目录</title></head>\n"
        "  <body>\n"
        '    <nav epub:type="toc">\n      <h1>目录</h1>\n      <ol>\n'
        + "\n".join(items)
        + "\n      </ol>\n    </nav>\n  </body>\n</html>"
    )


def _chapter_xhtml(ch: dict, index: int) -> str:
    title = _chapter_title(ch, index)
    body = md_to_html(str(ch.get("text") or ""))
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml">\n'
        f"  <head><title>{html.escape(title)}</title></head>\n"
        f"  <body><h2>{html.escape(title)}</h2>\n{body}\n  </body>\n</html>"
    )


def build_epub(chapters: list[dict], *, title: str, author: str = "") -> bytes:
    """构建 EPUB 字节流。chapters: [{id, title, text}]；空文本章节自动跳过。

    全部章节正文为空时显式报错（生成空 spine 违反 EPUB 规范，阅读器会拒开）。
    """
    valid = [c for c in chapters if str(c.get("text") or "").strip()]
    if not valid:
        raise ValueError("没有非空章节可导出：全书正文为空，拒绝生成空 EPUB")
    title = _sanitize_xml_text(title or "")
    author = _sanitize_xml_text(author or "")
    book_id = f"urn:uuid:{uuid.uuid4()}"

    container_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<container version="1.0" '
        'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
        '  <rootfile full-path="OEBPS/content.opf" '
        'media-type="application/oebps-package+xml"/>\n</container>'
    )
    members: list[tuple[str, bytes]] = [
        ("mimetype", b"application/epub+zip"),
        ("META-INF/container.xml", container_xml.encode("utf-8")),
        ("OEBPS/content.opf",
         _content_opf(valid, title, author, book_id).encode("utf-8")),
        ("OEBPS/nav.xhtml", _nav_xhtml(valid).encode("utf-8")),
    ]
    for i, ch in enumerate(valid, start=1):
        members.append(
            (f"OEBPS/chapter_{i}.xhtml", _chapter_xhtml(ch, i).encode("utf-8"))
        )

    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        # mimetype 必须是第一个条目且不压缩（EPUB 3 规范要求）
        first, first_bytes = members[0]
        zf.writestr(first, first_bytes, compress_type=zipfile.ZIP_STORED)
        for name, data in members[1:]:
            zf.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED)
    return buf.getvalue()


def write_epub(path: Path | str, chapters: list[dict], *, title: str,
               author: str = "") -> Path:
    """把 EPUB 写到指定路径并返回 Path。"""
    path = Path(path)
    path.write_bytes(build_epub(chapters, title=title, author=author))
    return path
