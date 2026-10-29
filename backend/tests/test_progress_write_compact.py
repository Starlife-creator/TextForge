"""B2.3：紧凑写 progress.json 的收益来自排版，但 fsync 不能被顺手去掉。

原先 `pretty=False` 同时去掉了 indent 和 fsync —— 一个排版开关暗地里改了掉电语义。
现在 `pretty` 只管缩进、`sync` 只管 fsync，progress.json 走 pretty=False + sync=True。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from utils.atomic import atomic_write_json
from utils.progress_io import save_progress_dict

DATA = {"chapters": [{"id": "1", "title": "第一章"}], "batch_qc": {"1": []}, "usage": {"in": 5}}


@pytest.fixture
def fsync_spy(monkeypatch):
    calls: list[int] = []
    real = os.fsync

    def spy(fd):
        calls.append(fd)
        return real(fd)

    monkeypatch.setattr(os, "fsync", spy)
    return calls


def test_compact_write_still_fsyncs(tmp_path, fsync_spy):
    """把 `sync` 默认改成 False（或 pretty=False 分支不 fsync）⇒ 本用例红。"""
    p = tmp_path / "progress.json"
    atomic_write_json(p, DATA, pretty=False, sync=True)
    assert len(fsync_spy) == 1, "紧凑写悄悄省掉了 fsync：掉电语义被排版开关换了"
    assert json.loads(p.read_text(encoding="utf-8")) == DATA


def test_sync_false_is_opt_in(tmp_path, fsync_spy):
    p = tmp_path / "progress.json"
    atomic_write_json(p, DATA, pretty=False, sync=False)
    assert fsync_spy == [], "sync=False 必须只在显式传入时生效"


def test_default_path_still_fsyncs_and_pretty(tmp_path, fsync_spy):
    p = tmp_path / "report.json"
    atomic_write_json(p, DATA)
    assert len(fsync_spy) == 1
    assert p.read_text(encoding="utf-8").count("\n") > 1, "默认仍是 pretty（人读的报告）"


def test_save_progress_dict_writes_compact_and_syncs(tmp_path, fsync_spy):
    """progress.json 的唯一写入口：紧凑 + 仍然 fsync + 内容往返一致。"""
    p = tmp_path / "progress.json"
    save_progress_dict(p, DATA)
    raw = p.read_text(encoding="utf-8")
    assert len(fsync_spy) == 1
    assert "\n  " not in raw and '": ' not in raw, f"仍是缩进/默认分隔符写法：{raw[:80]!r}"
    assert json.loads(raw) == DATA


def test_no_progress_site_writes_pretty_anymore():
    """接线守卫：core/ 与 routes/ 里不许再出现「直接 atomic_write_json 写 progress」。

    新增站点必须走 save_progress_dict，否则紧凑口径会一点点漂移回去。
    """
    root = Path(__file__).resolve().parents[1]
    offenders = []
    for rel in ("core", "routes"):
        for f in (root / rel).glob("*.py"):
            text = f.read_text(encoding="utf-8")
            for line in text.splitlines():
                code = line.split("#", 1)[0]
                if "atomic_write_json(" in code and "progress" in code:
                    offenders.append(f"{f.name}: {line.strip()}")
    assert not offenders, f"这些 progress 写点绕过了统一入口：{offenders}"
