"""#15 预估结果内存 LRU 缓存：同输入命中、配置/文件变化失效、返回深拷贝。"""

from pathlib import Path
from types import SimpleNamespace

from core import estimate

_SENT = "他推开门，屋里静得能听见自己的心跳声。"


def _req(path: Path, **kw):
    base = dict(
        input_path=str(path), input_mode="single_file", input_format="txt",
        output_path=None, context_window=8000, author_style="出版级文学重构",
        refactor_gates={},
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _book(tmp: Path) -> Path:
    p = tmp / "book.txt"
    p.write_text("第1章 起事\n" + _SENT * 40, encoding="utf-8")
    return p


def test_estimate_cache_hits_on_same_input(tmp_path, monkeypatch):
    book = _book(tmp_path)
    calls = {"n": 0}
    orig = estimate.chapter_token_sizes

    def spy(req):
        calls["n"] += 1
        return orig(req)

    monkeypatch.setattr(estimate, "chapter_token_sizes", spy)
    estimate._ESTIMATE_CACHE.clear()

    first = estimate.estimate_run(_req(book))
    second = estimate.estimate_run(_req(book))

    assert calls["n"] == 1
    assert first == second


def test_estimate_cache_keyed_by_context_window(tmp_path, monkeypatch):
    book = _book(tmp_path)
    calls = {"n": 0}
    orig = estimate.chapter_token_sizes

    def spy(req):
        calls["n"] += 1
        return orig(req)

    monkeypatch.setattr(estimate, "chapter_token_sizes", spy)
    estimate._ESTIMATE_CACHE.clear()

    small = estimate.estimate_run(_req(book, context_window=8000))
    big = estimate.estimate_run(_req(book, context_window=32768))

    assert calls["n"] == 2
    assert small != big
    assert len(estimate._ESTIMATE_CACHE) == 2


def test_estimate_cache_busts_when_file_changes(tmp_path, monkeypatch):
    book = _book(tmp_path)
    calls = {"n": 0}
    orig = estimate.chapter_token_sizes

    def spy(req):
        calls["n"] += 1
        return orig(req)

    monkeypatch.setattr(estimate, "chapter_token_sizes", spy)
    estimate._ESTIMATE_CACHE.clear()

    estimate.estimate_run(_req(book))
    # 改写正文并刷新 mtime/size 指纹
    book.write_text("第1章 起事\n" + _SENT * 80, encoding="utf-8")
    estimate.estimate_run(_req(book))

    assert calls["n"] == 2


def test_estimate_returns_deep_copy_not_cached_reference(tmp_path, monkeypatch):
    book = _book(tmp_path)
    estimate._ESTIMATE_CACHE.clear()
    first = estimate.estimate_run(_req(book))
    first["notes"].append("外部篡改")
    second = estimate.estimate_run(_req(book))
    assert "外部篡改" not in second["notes"]
