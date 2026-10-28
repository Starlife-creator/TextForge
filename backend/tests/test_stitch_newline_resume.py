"""B4（review A4.2）：跨版本续跑时，before_hash 按 CRLF 文本算、文件已是 LF，
apply_stitches 不能抛 ValueError，补丁必须被应用。

旧 progress 是 Windows/CRLF 环境算的锚点哈希；新版 read_text 用 universal newlines
读入后变成 LF，原样哈希不匹配。双向行尾归一后应正确应用。
把 stitch.py 里的 seg_lf/seg_crlf 归一化删掉，本测试必须抛 ValueError。
"""
from __future__ import annotations

from pathlib import Path

from core.stitch import apply_stitches, sha256_of


def test_stitch_cross_newline_resume(tmp_path: Path) -> None:
    chapter_dir = tmp_path / "02_workspace" / "reconstructed"
    chapter_dir.mkdir(parents=True)
    # 文件实际是 LF（新代码读入就是 LF）；锚点跨两行，确保 CRLF 归一真的被执行。
    body = "开篇段落。\n中间需要缝合的旧句。\n结尾段落。\n"
    (chapter_dir / "chapter_1.txt").write_text(body, encoding="utf-8")

    target = "中间需要缝合的旧句。\n结尾段落。"
    start = body.index(target)
    end = start + len(target)
    # 旧锚点哈希是在 CRLF 版本上算的（\n → \r\n）
    crlf_target = target.replace("\n", "\r\n")
    old_hash = sha256_of(crlf_target)

    patches = {
        "02_workspace/reconstructed/chapter_1.txt": [
            {"file": "02_workspace/reconstructed/chapter_1.txt",
             "start": start, "end": end,
             "before_hash": old_hash,
             "replacement": "经过缝合后的新句。"},
        ]
    }
    # 不抛即过
    apply_stitches(tmp_path, patches)
    out = (chapter_dir / "chapter_1.txt").read_text(encoding="utf-8")
    assert "经过缝合后的新句。" in out
    assert "中间需要缝合的旧句。" not in out
