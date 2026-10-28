"""B5/M1（review）：符号链接守卫不依赖特权，靠 monkeypatch Path.is_symlink 覆盖。

Windows 上创建符号链接需要开发者模式/管理员，CI Windows 腿上原测试是 skip，
等于零覆盖。这里把 is_symlink monkeypatch 成 True，断言守卫返回空、不读文件。
文件名必须过白名单 ^chapter_[^/\\]+\\.comparison\\.md$，否则在白名单那行就被拦掉、
根本走不到 is_symlink——那是上一版空守卫的根因。去掉 routes/status.py 里的
is_symlink 判断，本测试必须变红。
"""
from __future__ import annotations

from pathlib import Path

import routes.status as status


def test_comparison_symlink_guard_returns_empty(tmp_path, monkeypatch) -> None:
    comp = tmp_path / "00_comparison"
    comp.mkdir()
    name = "chapter_1.comparison.md"
    target = comp / name
    target.write_text("# 对照稿正文", encoding="utf-8")

    real_is_symlink = Path.is_symlink

    def fake_is_symlink(self: Path) -> bool:
        if self.name == name:
            return True
        return real_is_symlink(self)

    monkeypatch.setattr(Path, "is_symlink", fake_is_symlink)

    out = status._read_comparison(comp, name)
    assert out == [], f"符号链接未被守卫拒绝：{out!r}"

    listed = status._list_comparisons(comp)
    assert all(item.get("name") != name for item in listed), \
        f"符号链接出现在列表里：{listed!r}"
