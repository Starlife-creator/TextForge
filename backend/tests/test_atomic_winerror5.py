"""B1（review A1）：Windows 上 os.replace 撞共享冲突必须退避重试，且不能吞掉真错误。

原判据是「一个写线程 + 一个读线程各 50 次，双方零异常」。本机三段实测不支持它：
  - 原写法 20 次 pytest 红 3 次，异常**全部来自读线程**的 io.open（替换瞬间的共享
    冲突）——「读永不阻塞」不是原子写能保证的属性，写进断言只得到一条随机红的闸门；
  - 中间一版让读线程无间隔 hammer：14 轮里 6 轮写侧最终失败，其中 1 轮连第一次写
    都落不了盘。原因是 os.replace 需要目标的 DELETE 权限，而被持续占用的目标几乎
    不可能在 ~620ms 的重试预算内出现空隙——那是测试自己造出来的条件。
所以拆成两层：写侧「瞬时被挡 → 退避 → 最终成功」和非 PermissionError 必须原样上抛，
用注入式确定性验证（这两条才是 replace_with_retry 的真正判据）；并发用例把读侧回到
真实节拍（2ms 一次，对应 /api/status 轮询），钉「写侧零异常 + 读到的永远是某一次
完整写入 + 终态是最后一次写」。
"""
from __future__ import annotations

import os
import threading
import time

import pytest

from utils.atomic import atomic_write_text, replace_with_retry


def _flaky_replace(monkeypatch, fail_times: int):
    """让前 fail_times 次 os.replace 抛 Windows 共享冲突，之后走真实替换。"""
    real = os.replace
    calls = {"n": 0}

    def fake(src, dst, **kw):
        calls["n"] += 1
        if calls["n"] <= fail_times:
            raise PermissionError(13, "拒绝访问。")
        return real(src, dst, **kw)

    monkeypatch.setattr(os, "replace", fake)
    return calls


def test_replace_with_retry_survives_transient_permission_error(tmp_path, monkeypatch):
    """退回裸 os.replace ⇒ 本用例红（注入的 PermissionError 直接上抛）。"""
    src = tmp_path / "a.tmp"
    dst = tmp_path / "Story_Bible.md"
    src.write_text("version-7\n", encoding="utf-8")
    calls = _flaky_replace(monkeypatch, fail_times=2)

    replace_with_retry(str(src), dst)

    assert calls["n"] == 3, f"只调了 {calls['n']} 次，退避没有发生"
    assert dst.read_text(encoding="utf-8") == "version-7\n"
    assert not src.exists()


def test_replace_with_retry_does_not_swallow_other_errors(tmp_path, monkeypatch):
    """FileNotFoundError 是「目标盘/目录没了」这类真问题，重试掩盖它等于制造数据丢失。"""
    calls = {"n": 0}

    def fake(src, dst, **kw):        # noqa: ARG001 - 替身必须接住真实签名
        calls["n"] += 1
        raise FileNotFoundError(2, "系统找不到指定的路径。")

    monkeypatch.setattr(os, "replace", fake)
    with pytest.raises(FileNotFoundError):
        replace_with_retry(str(tmp_path / "x.tmp"), tmp_path / "Story_Bible.md")
    assert calls["n"] == 1, "非 PermissionError 也被重试了：真错误会被退避吞掉"


def test_atomic_write_text_lands_through_the_retry_path(tmp_path, monkeypatch):
    """接线检查：生产入口 atomic_write_text 必须真的走重试版 replace。
    把它内部改成裸 os.replace ⇒ 本用例红。"""
    target = tmp_path / "Story_Bible.md"
    target.write_text("v0", encoding="utf-8")
    calls = _flaky_replace(monkeypatch, fail_times=1)

    atomic_write_text(target, "version-42\n")

    assert calls["n"] == 2
    assert target.read_text(encoding="utf-8") == "version-42\n"
    assert list(tmp_path.glob("*.tmp")) == [], "失败路径外的临时文件没清掉"


def test_concurrent_write_and_read_never_yields_torn_content(tmp_path):
    """并发钉两件事：读到的永远是某一次完整写入（原子性），且真实节拍下写侧零异常。

    读线程按 /api/status 的量级限速（每 2ms 一次）。实测：无间隔 hammer 时目标
    几乎 100% 时间被读句柄占着，而 os.replace 需要目标的 DELETE 权限，于是会
    **持续**失败，~620ms 重试预算必然打满（本机 14 轮里 6 轮，其中 1 轮连第一次
    写都落不了盘）。那是测试自己造出来的条件，不是产品的读写形态；把它写进断言
    只会得到一条随机红的闸门。瞬时被挡的那一轮读跳过即可，不计入失败。
    """
    target = tmp_path / "Story_Bible.md"
    target.write_text("v0", encoding="utf-8")
    valid = {"v0"} | {f"version-{i}\n" for i in range(30)}
    observed: list[str] = []
    writer_errors: list[Exception] = []
    stop = threading.Event()

    def writer() -> None:
        try:
            for i in range(30):
                atomic_write_text(target, f"version-{i}\n")
        except Exception as e:  # noqa: BLE001
            writer_errors.append(e)
        finally:
            stop.set()

    def reader() -> None:
        while not stop.is_set():
            try:
                observed.append(target.read_text(encoding="utf-8"))
            except (PermissionError, FileNotFoundError):
                pass                    # 替换瞬间被挡：本轮跳过，继续按节拍读
            time.sleep(0.002)

    tw = threading.Thread(target=writer)
    tr = threading.Thread(target=reader)
    tw.start(); tr.start()
    tw.join(); tr.join()

    assert not writer_errors, f"写线程最终失败（重试没兜住）：{writer_errors!r}"
    assert observed, "读线程一次都没读到，本用例没测到任何东西"
    torn = [v for v in observed if v not in valid]
    assert not torn, f"读到半截内容（原子性被破）：{torn[:3]!r}"
    assert target.read_text(encoding="utf-8") == "version-29\n"
