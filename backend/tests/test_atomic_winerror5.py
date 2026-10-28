"""B1（review A1）：atomic_write_text 在并发读写下不能抛 PermissionError。

Windows 上杀软/资源管理器/另一线程正打开目标文件时，os.replace 撞 PermissionError
[WinError 5]。_replace_with_retry 必须在短退避窗口内重试成功，写线程与读线程零异常，
且最后一次写入被读到。把 _replace_with_retry 退回裸 os.replace，本测试在 Windows 上
必须变红。
"""
from __future__ import annotations

import threading
from pathlib import Path

from utils.atomic import atomic_write_text


def test_atomic_write_surconcurrent_read_no_error(tmp_path: Path) -> None:
    target = tmp_path / "Story_Bible.md"
    target.write_text("v0", encoding="utf-8")
    errors: list[Exception] = []

    def writer() -> None:
        try:
            for i in range(50):
                atomic_write_text(target, f"version-{i}\n")
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    def reader() -> None:
        try:
            for _ in range(50):
                _ = target.read_text(encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    tw = threading.Thread(target=writer)
    tr = threading.Thread(target=reader)
    tw.start(); tr.start()
    tw.join(); tr.join()

    assert not errors, f"并发读写抛错：{errors!r}"
    assert target.read_text(encoding="utf-8").startswith("version-")
