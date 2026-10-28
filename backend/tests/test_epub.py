"""#20 EPUB 最小闭环：成员清单、mimetype 存储、导航目录、粗斜体映射与转义回归。"""

import io
import zipfile

from core.epub import build_epub, md_to_html, write_epub


def _chapters():
    return [
        {"id": "1", "title": "第一章 启程", "text": "他推开门走进风雪。\n\n**重点段落**：风像刀割脸。\n\n*心情沉重*。"},
        {"id": "2", "title": "第二章 重逢", "text": "巷口的灯还亮着，旧友站在树下。"},
        {"id": "3", "title": "第三章 空章", "text": ""},
    ]


def _unzip(blob: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        return {name: zf.read(name) for name in zf.namelist()}


def test_epub_member_layout_and_mimetype_first_stored():
    blob = build_epub(_chapters(), title="测试书")
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        names = zf.namelist()
        assert names[0] == "mimetype"
        info = zf.getinfo("mimetype")
        assert info.compress_type == zipfile.ZIP_STORED
        assert zf.read("mimetype") == b"application/epub+zip"
    for required in ("META-INF/container.xml", "OEBPS/content.opf", "OEBPS/nav.xhtml"):
        assert required in names


def test_epub_skips_empty_chapter_and_toc_lists_titles():
    blob = build_epub(_chapters(), title="测试书")
    members = _unzip(blob)
    nav = members["OEBPS/nav.xhtml"].decode("utf-8")
    assert "第一章 启程" in nav
    assert "第二章 重逢" in nav
    # 空章不进目录，也不生成正文文件
    assert "第三章 空章" not in nav
    assert "OEBPS/chapter_3.xhtml" not in members
    assert "OEBPS/chapter_1.xhtml" in members
    assert "OEBPS/chapter_2.xhtml" in members


def test_epub_opf_metadata_and_spine_match_chapter_count():
    blob = build_epub(_chapters(), title="测试书", author="作者甲")
    opf = _unzip(blob)["OEBPS/content.opf"].decode("utf-8")
    assert "<dc:title>测试书</dc:title>" in opf
    assert "<dc:creator>作者甲</dc:creator>" in opf
    assert opf.count("<itemref") == 2


def test_markdown_bold_italic_mapped_to_html():
    html_body = md_to_html("**粗体一句**，*斜体半句*。")
    assert "<strong>粗体一句</strong>" in html_body
    assert "<em>斜体半句</em>" in html_body


def test_epub_escapes_html_injection():
    blob = build_epub(
        [{"id": "1", "title": "标", "text": "<script>alert(1)</script>正文"}],
        title="安全书",
    )
    chapter = _unzip(blob)["OEBPS/chapter_1.xhtml"].decode("utf-8")
    assert "<script>alert(1)</script>" not in chapter
    assert "&lt;script&gt;" in chapter


def test_write_epub_writes_file(tmp_path):
    target = tmp_path / "book.epub"
    write_epub(target, _chapters(), title="测试书")
    assert target.exists()
    blob = target.read_bytes()
    assert blob[:2] == b"PK"
    members = _unzip(blob)
    assert "OEBPS/content.opf" in members
