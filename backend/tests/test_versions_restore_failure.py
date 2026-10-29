"""versions.restore 的失败路径（R2 / B1.3）。

今天的代码在 `finally` 里无条件删掉 `._restore_old`——那里面装着用户**当前**的
成品（第二次 move 失败时它是唯一副本）。三条用例各盯一种真实错法：

1. 第二次 move 抛 PermissionError（Word 正开着 03_final 里的 docx，而应用自己
   就有「打开成品」按钮）⇒ 成品不能消失，必须放回原位。
2. `shutil.move` 在目标目录已存在时**不抛错，而是把源嵌进去**（我实测过：返回
   `<dst>/tmpdir`）。所以「没抛异常 = 替换成功」这个判据本身是错的。
3. 恢复前的 `snapshot()` 失败时只 warning 返回 None ⇒ `._restore_old` 就是唯一副本，
   此时即使替换成功也绝不能删它。
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from core import versions


def _layout(tmp_path: Path) -> tuple[Path, str]:
    """造一个有当前成品 + 一个可恢复旧版本的输出目录，返回 (输出目录, 版本名)。"""
    out = tmp_path / "book"
    final = out / "03_final"
    final.mkdir(parents=True)
    (final / "重构终稿.txt").write_text("UNIQUE-CURRENT", encoding="utf-8")
    root = out / "_versions" / "20260101_000000_旧版"
    root.mkdir(parents=True)
    (root / "重构终稿.txt").write_text("TO-BE-RESTORED", encoding="utf-8")
    return out, "20260101_000000_旧版"


def test_second_move_failure_keeps_current_deliverables(tmp_path, monkeypatch) -> None:
    out, vid = _layout(tmp_path)
    real_move = shutil.move
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:            # 把新内容挪进 03_final 那一步被占用挡住
            raise PermissionError(13, "Access is denied")
        return real_move(src, dst)

    monkeypatch.setattr(shutil, "move", flaky)
    with pytest.raises(PermissionError):
        versions.restore(out, vid)

    final = out / "03_final"
    assert (final / "重构终稿.txt").read_text(encoding="utf-8") == "UNIQUE-CURRENT", (
        "恢复失败把用户当前成品弄丢了")
    assert not list(out.glob("._restore_old*")), "回滚后不该留下备份残留"


def test_nesting_is_treated_as_failure(tmp_path, monkeypatch) -> None:
    out, vid = _layout(tmp_path)
    real_move = shutil.move
    calls = {"n": 0}

    def move_with_race(src, dst):
        calls["n"] += 1
        if calls["n"] == 1:
            r = real_move(src, dst)          # 03_final → ._restore_old_*
            Path(dst).parent.joinpath("03_final").mkdir()   # 另一个写者当场重建 03_final
            Path(dst).parent.joinpath("03_final", "抢占.txt").write_text("RACE", encoding="utf-8")
            return r
        return real_move(src, dst)           # 这次会「无声嵌进」已存在的 03_final

    monkeypatch.setattr(shutil, "move", move_with_race)
    with pytest.raises(RuntimeError, match="没有落到原位"):
        versions.restore(out, vid)          # 嵌套必须当成失败，而不是「没抛错就是成功」

    final = out / "03_final"
    names = sorted(p.name for p in final.iterdir())
    assert "重构终稿.txt" in names, f"旧成品该被放回原位，实际 03_final 里是：{names}"
    assert (final / "重构终稿.txt").read_text(encoding="utf-8") == "UNIQUE-CURRENT"
    assert not [n for n in names if n.startswith("._restore_tmp")], (
        f"新内容被嵌进成品目录却当成成功：{names}")


def test_snapshot_failure_parks_backup_instead_of_deleting_it(tmp_path, monkeypatch) -> None:
    out, vid = _layout(tmp_path)
    real_copytree = shutil.copytree

    def snapshot_fails(src, dst, *a, **kw):
        if "_versions" in str(dst):          # 只有恢复前那次快照失败
            raise OSError(13, "copytree blocked")
        return real_copytree(src, dst, *a, **kw)

    monkeypatch.setattr(shutil, "copytree", snapshot_fails)
    result = versions.restore(out, vid)      # 替换本身是成功的
    assert result["restored"] == vid
    assert result.get("snapshot_before_restore") is None

    parked = list(out.glob("._restore_old*"))
    assert parked, "快照失败时 ._restore_old 是唯一副本，成功也不能删"
    assert (parked[0] / "重构终稿.txt").read_text(encoding="utf-8") == "UNIQUE-CURRENT"


def test_clear_sweep_removes_restore_residue_only_for_our_dirs(tmp_path):
    """B1.3 ③收口：救急副本不进 `_versions/`（会被 _prune 当旧版本删），停在输出目录根上，
    由「清空输出目录」按**前缀 + 只认目录**回收——用户自己的同名前缀文件不能碰。"""
    from routes.output import _clear_artifacts, _summarize

    out = tmp_path / "book"
    out.mkdir()
    (out / "progress.json").write_text("{}", encoding="utf-8")
    parked = out / "._restore_old_20261029_010101"
    parked.mkdir()
    (parked / "重构终稿.txt").write_text("UNIQUE-CURRENT", encoding="utf-8")
    (out / "._restore_tmp_20261029_010102").mkdir()
    (out / "我的笔记.txt").write_text("用户的文件", encoding="utf-8")
    (out / "._restore_old_readme.txt").write_text("同名前缀的用户文件", encoding="utf-8")

    listed = _summarize(str(out))["artifacts"]
    assert parked.name in listed, "救急副本必须在输出检查里看得见"

    removed, failed = _clear_artifacts(out)

    assert failed == []
    assert parked.name in removed and "._restore_tmp_20261029_010102" in removed
    assert not parked.exists()
    assert (out / "我的笔记.txt").read_text(encoding="utf-8") == "用户的文件"
    assert (out / "._restore_old_readme.txt").exists(), "前缀匹配必须只认目录，不能吃掉用户的文件"
