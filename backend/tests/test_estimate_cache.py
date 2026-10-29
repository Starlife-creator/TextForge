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


def test_cache_hit_deepcopy_happens_inside_the_lock(tmp_path, monkeypatch):
    """结构性判据（不是概率）：命中路径上那次 deepcopy 必须在锁内。

    没有锁时的真实失败模式：A 线程 get() 到值 → B 线程写满 16 条把它淘汰 →
    A 的 move_to_end(key) 抛 KeyError。把 `with _ESTIMATE_CACHE_LOCK` 摘掉
    ⇒ 本用例红（acquire 非阻塞成功 = 当时没人持锁）。
    """
    import copy

    book = _book(tmp_path)
    estimate._ESTIMATE_CACHE.clear()
    estimate.estimate_run(_req(book))          # 先填缓存，下一次走命中路径

    real_deepcopy = copy.deepcopy
    checked = {"n": 0}

    def spy(obj):
        checked["n"] += 1
        held = estimate._ESTIMATE_CACHE_LOCK.acquire(blocking=False)
        if held:
            estimate._ESTIMATE_CACHE_LOCK.release()
        assert not held, "读缓存不在锁内：并发淘汰会让 move_to_end 抛 KeyError"
        return real_deepcopy(obj)

    monkeypatch.setattr(copy, "deepcopy", spy)
    estimate.estimate_run(_req(book))
    assert checked["n"] == 1, f"命中路径应有且只有 1 次 deepcopy，实际 {checked['n']}"


def test_concurrent_estimates_survive_eviction(tmp_path):
    """并发压测：40 个不同输入（>缓存上限 16）多线程跑，任何线程都不许抛。

    如实标注：这条**不是**「回退即红」的守卫——GIL 下缺锁只是概率性失败。
    确定性判据是上面那条锁内 deepcopy。
    """
    import threading

    estimate._ESTIMATE_CACHE.clear()
    books = []
    for i in range(40):
        p = tmp_path / f"book_{i}.txt"
        p.write_text(f"第{i}章 起事\n" + _SENT * 5, encoding="utf-8")
        books.append(p)

    errors: list[BaseException] = []

    def worker(order: list[Path]) -> None:
        try:
            for b in order:
                estimate.estimate_run(_req(b))
        except BaseException as e:      # noqa: BLE001
            errors.append(e)

    half = books[:20]
    other = books[20:] + books[:20]
    ts = [threading.Thread(target=worker, args=(seq,)) for seq in (half, other, half, other)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=60)

    assert not errors, f"并发预估抛错：{errors[:3]!r}"
    assert len(estimate._ESTIMATE_CACHE) <= estimate._ESTIMATE_CACHE_MAX
