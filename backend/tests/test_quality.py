"""成品质量报告的结构性检查测试。"""

import tempfile
from pathlib import Path
from types import SimpleNamespace

from core.quality import (
    STITCH_WARN_MEAN_RATE,
    assess_batch_texts,
    build_quality_report,
    change_rate,
    find_duplicate_pairs,
    has_blocking_issues,
    read_quality_report,
    stitch_metrics,
    write_quality_report,
)


def _req(**kw):
    base = dict(
        forbidden_canon=[],
        name_map={},
        refactor_mode="full_rewrite",
        model="mock-model",
        context_window=8000,
        input_mode="single_file",
        input_format="txt",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _chapter(i: int, is_empty: bool = False) -> dict:
    return {
        "id": str(i),
        "title": f"第{i}章",
        "split_file": f"02_workspace/split/{i}.txt",
        "is_empty": is_empty,
    }


def _report(tmp: str, originals: list[str], recons: list[str], *, req=None, progress=None):
    out = Path(tmp)
    (out / "02_workspace/split").mkdir(parents=True, exist_ok=True)
    chapters = []
    for i, (orig, _recon) in enumerate(zip(originals, recons, strict=False), start=1):
        ch = _chapter(i)
        (out / ch["split_file"]).write_text(orig, encoding="utf-8")
        chapters.append(ch)
    return build_quality_report(
        chapters=chapters,
        texts=recons,
        progress=progress or {},
        req=req or _req(),
        output_path=out,
    )


def test_quality_good_report():
    originals = [("原文第%d章。" % i) * 150 for i in range(1, 4)]
    recons = [("重构第%d章故事继续。" % i) * 130 for i in range(1, 4)]
    with tempfile.TemporaryDirectory() as tmp:
        report = _report(tmp, originals, recons)

    assert report["status"] == "good"
    assert report["summary"]["empty_chapters"] == 0
    assert report["summary"]["duplicate_pairs"] == 0
    assert all(row["status"] == "ok" for row in report["chapters"])
    assert report["summary"]["length_ratio"] > 0


def test_quality_detects_empty_residual_and_placeholder():
    originals = [("原文。" * 300) for _ in range(3)]
    recons = [
        "",
        "正文 ===CHAPTER_BREAK=== 残留",
        "这里 TODO 待补，还没写完。",
    ]
    with tempfile.TemporaryDirectory() as tmp:
        report = _report(tmp, originals, recons)

    codes = {i["code"] for i in report["issues"]}
    assert report["status"] == "bad"
    assert {"empty_chapter", "residual_marker", "placeholder"} <= codes
    assert report["summary"]["empty_chapters"] == 1
    assert report["summary"]["residual_markers"] == 1
    assert report["summary"]["placeholder_hits"] == 1


def test_quality_detects_cross_chapter_duplicate():
    originals = [("原文甲。" * 300), ("原文乙。" * 300)]
    same = "这是一段会被完整复制到另一章的正文。" * 40
    with tempfile.TemporaryDirectory() as tmp:
        report = _report(tmp, originals, [same, same])

    assert report["status"] == "bad"
    assert report["summary"]["duplicate_pairs"] == 1
    assert report["duplicates"][0]["containment"] > 0.9
    assert {i["code"] for i in report["issues"]} >= {"cross_chapter_duplicate"}


def test_quality_detects_forbidden_and_old_name_in_reskin():
    originals = [("原文。" * 300)]
    recon = "苏晚看见林婉儿走来，她用了禁术。" * 30
    req = _req(
        refactor_mode="reskin",
        name_map={"林婉儿": "苏晚"},
        forbidden_canon=["禁术"],
    )
    with tempfile.TemporaryDirectory() as tmp:
        report = _report(tmp, originals, [recon], req=req)

    codes = {i["code"] for i in report["issues"]}
    assert report["status"] == "bad"
    assert "forbidden_canon" in codes
    assert "old_name_residue" in codes
    assert report["summary"]["forbidden_hits"] == 1
    assert report["summary"]["old_name_hits"] == 1


def test_quality_detects_content_collapse():
    originals = [("原文。" * 800)]
    with tempfile.TemporaryDirectory() as tmp:
        report = _report(tmp, originals, ["只剩一句话。"])

    assert report["status"] == "bad"
    assert {i["code"] for i in report["issues"]} >= {"content_collapse"}


def test_duplicate_needs_more_than_one_shared_shingle():
    common = "共同片段" * 20  # 80 字：至少产生两个 60 字重叠片段
    a = common + "甲" * 500
    b = common + "乙" * 500
    assert len(find_duplicate_pairs({"1": a, "2": b})) == 1

    short = "共同片段" * 15  # 60 字：只共享一个片段，不应误报
    c = short + "甲" * 500
    d = short + "乙" * 500
    assert find_duplicate_pairs({"1": c, "2": d}) == []


def test_quality_report_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        report = _report(tmp, ["原文。" * 200], ["重构。" * 200])
        path = write_quality_report(tmp, report)
        loaded = read_quality_report(tmp)

    assert path.name == "quality_report.json"
    assert loaded is not None
    assert loaded["status"] == report["status"]
    assert loaded["summary"]["chapters"] == 1


def test_assess_batch_flags_placeholder_and_collapse():
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        split = out / "02_workspace/split"
        split.mkdir(parents=True)
        (split / "1.txt").write_text("原文内容。" * 300, encoding="utf-8")
        issues = assess_batch_texts(
            chapters=[_chapter(1)],
            chapter_ids=["1"],
            texts={"1": "TODO：这里待补。"},
            output_path=out,
            gates={"inject_forbidden": False},
        )

    codes = {i["code"] for i in issues}
    assert {"placeholder", "content_collapse"} <= codes
    assert has_blocking_issues(issues)


def test_assess_batch_flags_ai_meta_talk():
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        split = out / "02_workspace/split"
        split.mkdir(parents=True)
        original = "原文内容。" * 300
        (split / "1.txt").write_text(original, encoding="utf-8")
        issues = assess_batch_texts(
            chapters=[_chapter(1)],
            chapter_ids=["1"],
            texts={"1": original + "\n\n作为 AI，希望以上内容对你有帮助。"},
            output_path=out,
        )

    codes = {i["code"] for i in issues}
    assert "placeholder" in codes
    assert has_blocking_issues(issues)


def test_assess_batch_good_text_has_no_blocking_issue():
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        split = out / "02_workspace/split"
        split.mkdir(parents=True)
        original = "原文内容。" * 300
        (split / "1.txt").write_text(original, encoding="utf-8")
        issues = assess_batch_texts(
            chapters=[_chapter(1)],
            chapter_ids=["1"],
            texts={"1": "重构后的内容。" * 300},
            output_path=out,
        )

    assert not has_blocking_issues(issues)


def _uniq(n=90, tag="文"):
    """每行都不同的长文本：重复文本会让 shingle 集合自碰撞，改动率失真。"""
    return "".join(f"第{i:02d}{tag}内容含{i * 7 % 100}号标记。" for i in range(n))


def _anchor(rate, chars=900, *, applied=True, file="02_workspace/reconstructed/chapter_1.txt",
            source="batch_stitch"):
    return {"applied": applied, "change_rate": rate, "chars": chars,
            "file": file, "source": source}


def test_change_rate_distinguishes_untouched_from_bulk_rewrite():
    base = _uniq()
    assert change_rate(base, base) == 0.0
    cut = base.index("。", len(base) // 2) + 1
    assert change_rate(base, base[:cut] + "他停下脚步。" + base[cut:]) == 0.0, (
        "只在句边界插一句过渡、原文一句未动，改动率必须是 0（滑窗口径会在这里误报 52%）")
    # 把过渡句塞进句子中间会毁掉那一句 → 恰好多计一句，说明口径偏严不偏松
    half = len(base) // 2
    assert change_rate(base, base[:half] + "他停下脚步" + base[half:]) == round(1 / 90, 4)
    one = base.replace("第45文内容含15号标记", "第45文这里被整句改写了")
    assert one != base, "夹具没命中要改写的那句，测试本身失效"
    assert 0 < change_rate(base, one) < 0.05
    assert change_rate(base, _uniq(tag="别")) == 1.0
    # 反向断言：窗口太短必须给 None，不能伪装成「没有改动」
    assert change_rate("很短的一句。", "也就一句。") is None
    assert change_rate("", base) is None


def test_change_rate_ignores_pure_line_reflow():
    base = _uniq()
    reflowed = "\n\n".join(base[i:i + 60] for i in range(0, len(base), 60))
    assert change_rate(base, reflowed) == 0.0, "只改分段/换行不该被算成改写"


def test_stitch_metrics_counts_only_applied_and_separates_unmeasured():
    m = stitch_metrics([
        _anchor(0.1, file="a"),
        _anchor(0.8, file="b"),
        _anchor(None, chars=600, file="c"),        # 已应用但窗口太短：计入覆盖，不计入均值
        _anchor(0.95, file="d", applied=False),    # 降级跳过的补丁绝不能算成改写
    ])
    assert m["applied_patches"] == 3
    assert m["measured"] == 2
    assert m["exposed_chars"] == 2400
    assert m["mean"] == 0.45
    assert m["max"] == 0.8
    assert m["worst"][0]["file"] == "b"


def test_stitch_metrics_keeps_zero_rate_as_measured():
    """改动率 0.0 是「最理想的结果」，绝不能因为它是假值而被当成没数据丢掉。"""
    m = stitch_metrics([_anchor(0.0, file="a"), _anchor(0.0, file="b")])
    assert m["measured"] == 2
    assert m["mean"] == 0.0 and m["max"] == 0.0 and m["p90"] == 0.0


def test_stitch_metrics_p90_is_nearest_rank():
    rates = [0.05, 0.1, 0.15, 0.2, 0.9]
    m = stitch_metrics([_anchor(r, file=f"f{i}") for i, r in enumerate(rates)])
    assert m["p90"] == 0.9          # ceil(0.9*5)=5 → 最大那个
    m2 = stitch_metrics([_anchor(r, file=f"f{i}") for i, r in enumerate(rates[:-1])])
    assert m2["p90"] == 0.2         # ceil(0.9*4)=4 → 第四位，不能被离群值拉走


def test_quality_report_records_stitch_observation_without_warning():
    originals = [("原文第%d章。" % i) * 150 for i in range(1, 4)]
    recons = [("重构第%d章故事继续。" % i) * 130 for i in range(1, 4)]
    progress = {"stitch_anchors": [_anchor(0.1, file="a"), _anchor(0.2, file="b"),
                                   _anchor(0.3, file="c")]}
    with tempfile.TemporaryDirectory() as tmp:
        rep = _report(tmp, originals, recons, progress=progress)

    assert rep["stitch"]["measured"] == 3
    assert rep["stitch"]["mean"] == 0.2
    assert rep["summary"]["stitch_exposed_chars"] == 2700
    assert rep["summary"]["stitch_mean_rate"] == 0.2
    assert rep["summary"]["stitch_exposed_ratio"] == 1.0
    # 反向断言：平均改动率没越过阈值时不得产生提醒
    assert "stitch_bulk_rewrite" not in {i["code"] for i in rep["issues"]}


def test_quality_report_warns_when_stitch_rewrites_bulk():
    originals = [("原文第%d章。" % i) * 150 for i in range(1, 4)]
    recons = [("重构第%d章故事继续。" % i) * 130 for i in range(1, 4)]
    progress = {"stitch_anchors": [_anchor(0.9, file="a"), _anchor(0.95, file="b")]}
    with tempfile.TemporaryDirectory() as tmp:
        rep = _report(tmp, originals, recons, progress=progress)

    hit = [i for i in rep["issues"] if i["code"] == "stitch_bulk_rewrite"]
    assert hit and "平均改动率" in hit[0]["message"], str(hit)
    assert rep["status"] == "warn"
    assert [w["file"] for w in rep["stitch"]["worst"]] == ["b", "a"]


def test_stitch_warn_threshold_stays_below_reject_gate():
    """提醒阈值必须低于拒绝闸门：闸门开着时已应用补丁的改动率不可能越过闸门值。

    若把提醒阈值调到 0.35 以上，这条 warn 在默认配置下永远不会触发 —— 信号被写死。
    """
    from core.stitch import STITCH_MAX_CHANGE_RATE

    assert STITCH_WARN_MEAN_RATE < STITCH_MAX_CHANGE_RATE


def test_quality_report_states_which_stitch_guardrails_were_active():
    """报告要自证当时生效的护栏：窗口封顶值 + 闸门开/关。"""
    originals = [("原文第%d章。" % i) * 150 for i in range(1, 4)]
    recons = [("重构第%d章故事继续。" % i) * 130 for i in range(1, 4)]

    with tempfile.TemporaryDirectory() as tmp:
        on = _report(tmp, originals, recons)
    with tempfile.TemporaryDirectory() as tmp:
        off = _report(tmp, originals, recons,
                      req=_req(refactor_gates={"stitch_minimal_change": False}))

    assert "1200" in on["config"]["stitch_gate"]
    assert "已开启" in on["config"]["stitch_gate"]
    assert "已关闭" in off["config"]["stitch_gate"]
    assert "0.5~3.0" in off["config"]["stitch_gate"]
