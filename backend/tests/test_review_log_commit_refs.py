"""批次行里「✅ 短号」的取证有效性门禁（`docs/REVIEW-LOG.md`）。

REVIEW-LOG 的每个批次行都手抄了一个 commit 短号（`B1.1 ✅ de3d07e`、`B2.1 ✅ 07f5aa7`…），
它们是「这条改动在哪个提交里」的唯一取证。但没有任何检查断言过这些短号真能解析——
写错一位、抄错一位、或者历史被重写，日志照样全绿，读者拿到的是一个指向空处的地址。
这和「验证范围」是同一种病：文档里手抄的字不指向可验证事实就会漂。

判据只有一条：**`✅` 后紧跟的 7 位十六进制必须能在本仓库解析成一个 commit**。

刻意窄的三点（防止误咬正文）：
1. 只认 `✅` 紧跟这一个形状；正文里自然出现的十六进制串（配置值、哈希示例）不参与；
2. 恰好 7 位，`✅ abcdefg`（非十六进制）/ `✅ 46b9a14a425b`（超长）都不算命中；
3. 只验证存在性，不验证「这条改动是否还在」——后者要靠 `git log -S`，判据太脆，
   而且历史批的部分回退是合法的。存在性已经能挡住「抄错 / 历史重写」。

浅克隆下按环境分流：`git cat-file -e` 在 `--depth 1` 的仓库里对旧提交返回 128
（已实测），那不是证据失效，是环境没带那段历史——**本地 skip 是对的**。
但 CI 里出现浅克隆只能是我们自己的 checkout 配置掉了 `fetch-depth: 0`，
那是配置回归，必须红（见 `test_ci_checkout_must_be_full`）：否则整条门禁会
「每轮都绿、一次都没真跑」，比没有门禁更糟。
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LOG = ROOT / "docs" / "REVIEW-LOG.md"

# 只认「✅ 后紧跟 7 位小写十六进制」，前后都要有边界。
_SHA = re.compile(r"✅\s*([0-9a-f]{7})(?![0-9a-f])")


def shas_in(text: str) -> list[tuple[int, str]]:
    """按出现顺序返回 `(行号, 短号)`，行号从 1 起。重复出现照留（便于报出位置）。"""
    out: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for m in _SHA.finditer(line):
            out.append((lineno, m.group(1)))
    return out


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )


def git_available() -> bool:
    """仓库在、且能跑 git。打包成 tar 分发或没装 git 时整条门禁无意义。"""
    try:
        return _git("rev-parse", "--git-dir").returncode == 0
    except OSError:
        return False


def is_shallow() -> bool:
    """浅克隆（`--depth N`）里旧提交不可达，此时无法取证。"""
    r = _git("rev-parse", "--is-shallow-repository")
    return r.returncode == 0 and r.stdout.strip() == "true"


def in_ci() -> bool:
    """是否跑在 CI 里（GitHub Actions 会注入 `GITHUB_ACTIONS=true`）。

    这里**只用来加严**、不用来放宽：CI 里浅克隆必须红，本地浅克隆才允许 skip。
    """
    return os.environ.get("GITHUB_ACTIONS", "").strip().lower() == "true"


def coverage_gap() -> str | None:
    """返回「本应取证却取不了」的原因；None 表示取证能力完好。

    这是本文件存在意义的看门人：`test_review_log_shas_all_resolve` 在浅克隆里
    会 skip，而 skip 是绿的。如果 CI 的 checkout 掉了 `fetch-depth: 0`，整条门禁
    会变成「每次都绿、一次都没真跑」——那比没有门禁更糟，因为它给了假的安心。
    所以：**CI 里浅克隆 = 红**（配置坏了，要人修）；本地浅克隆 = skip（环境限制）。
    """
    if not git_available():
        return "不是 git 工作区"
    if is_shallow():
        return "浅克隆（--depth N），无完整提交历史"
    return None


def resolves(sha: str) -> bool:
    """短号能否解析成一个 commit。"""
    return _git("cat-file", "-e", f"{sha}^{{commit}}").returncode == 0


def validate(text: str, resolver=resolves) -> list[str]:
    """返回问题列表；空列表 = 通过。"""
    problems: list[str] = []
    for lineno, sha in shas_in(text):
        if not resolver(sha):
            problems.append(f"REVIEW-LOG.md:{lineno} `✅ {sha}` 解析不到对应 commit")
    return problems


@pytest.fixture(scope="module")
def log_text() -> str:
    return LOG.read_text(encoding="utf-8")


def require_evidence() -> None:
    """取证能力不足时的统一出口：CI 里红、本地 skip。

    为什么分两种反应：浅克隆让 `git cat-file` 对旧提交必然失败（实测 exit 128），
    证据本身没失效，是环境没带那段历史 —— 本地允许 skip。但 CI 里出现浅克隆，
    只能是我们自己的 checkout 配置掉了 `fetch-depth: 0`（build-check.yml:117 /
    release.yml:62 都已显式设 0），那是**配置回归**，必须红。
    """
    gap = coverage_gap()
    if gap is None:
        return
    if in_ci():
        pytest.fail(
            f"CI 里取不了证（{gap}）：本门禁在 CI 中会 silent-skip，等于从未执行。"
            "检查 .github/workflows 里跑 pytest 的 job 是否带 with: fetch-depth: 0"
            "（build-check.yml backend-check / release.yml backend-test）。"
        )
    pytest.skip(f"环境无法取证（{gap}），本地浅克隆属正常情况")


def test_ci_checkout_must_be_full() -> None:
    """CI 环境必须完整检出——本文件是「门禁会不会被静默跳过」的看门人。

    浅克隆下上面几条取证测试只能 skip，而 **skip 是绿的**。若 checkout 退回
    `fetch-depth: 1`，整条 commit-ref 门禁就变成「每轮都绿、一次都没真跑」，
    比没有门禁更糟：它给假的安心。所以这条断言在 CI 里恒开、且不 skip。
    """
    if not git_available():
        pytest.skip("不是 git 工作区，无法判断检出方式")
    if not in_ci():
        pytest.skip("非 CI 环境，检出方式由本地决定")
    assert not is_shallow(), (
        "CI 里检出是浅克隆（--depth N），commit-ref 门禁将 silent-skip。"
        "给该 job 的 actions/checkout 加 with: fetch-depth: 0。"
    )


def test_repo_is_usable_or_skip() -> None:
    if not git_available():
        pytest.skip("不是 git 工作区，短号取证无意义")
    if is_shallow() and not in_ci():
        pytest.skip("浅克隆（无完整历史），`git cat-file` 对旧 commit 必然失败，属环境限制")
    if is_shallow() and in_ci():
        pytest.fail("CI 里检出不完整，见 test_ci_checkout_must_be_full 的说明")


def test_review_log_shas_all_resolve(log_text: str) -> None:
    """真实的 REVIEW-LOG：每一个 `✅ 短号` 都必须能解析。"""
    require_evidence()
    hits = shas_in(log_text)
    assert hits, "一个短号都没抽到——正文里明明有 `B1.1 ✅ de3d07e`，说明抽取正则失效了"
    assert validate(log_text) == []


def test_known_shas_are_extracted(log_text: str) -> None:
    """抽取本身要有断言：门禁最怕的是「抽到 0 个 ⇒ 永远绿」。"""
    got = {sha for _, sha in shas_in(log_text)}
    # 这几个是 REVIEW-LOG §2 批次行里确实存在的短号。
    for sha in ("de3d07e", "07f5aa7", "478cff3", "0bde150"):
        assert sha in got, f"没从 REVIEW-LOG 抽到 {sha}，抽取正则可能已失效"


def test_body_hex_strings_are_not_matched() -> None:
    """不误咬正文：没有 `✅` 前缀的十六进制串一律不当短号。"""
    text = (
        "| B1.1 ✅ de3d07e | 说明 |\n"
        "超时 3f2a9c1 毫秒，见 app.py\n"
        "`#0bde150` 只是行内引用的另一种写法\n"
        "✅ abcdefg 不是十六进制\n"
        "✅ 46b9a14a425b2dc1 太长了不是短号\n"
    )
    assert shas_in(text) == [(1, "de3d07e")]


def test_fake_sha_is_red(log_text: str) -> None:
    """反向断言：塞一个不存在的短号必须红。

    用**真的** `resolves`（`deadbee` 在本仓库确实不存在），而不是桩：
    桩只能证明 `validate` 会转发 resolver 的结果，证明不了门禁有牙齿。
    """
    if not git_available() or is_shallow():
        pytest.skip("环境无法取证")
    assert resolves("deadbee") is False, "deadbee 竟然解析成功，换一个假短号"

    tampered = log_text.replace("✅ de3d07e", "✅ deadbee", 1)
    assert tampered != log_text, "替换没生效，反向断言无效"
    problems = validate(tampered)
    assert len(problems) == 1
    assert "deadbee" in problems[0]


def test_restoring_the_sha_turns_it_green_again(log_text: str) -> None:
    """改回真短号必须重新绿——证明红的是那一位，不是别的东西。"""
    if not git_available() or is_shallow():
        pytest.skip("环境无法取证")
    tampered = log_text.replace("✅ de3d07e", "✅ deadbee", 1)
    assert validate(tampered) != []
    restored = tampered.replace("✅ deadbee", "✅ de3d07e", 1)
    assert validate(restored) == []


def test_resolver_actually_rejects_garbage() -> None:
    """resolver 自己也要有牙齿，否则整条门禁是自证。"""
    if not git_available() or is_shallow():
        pytest.skip("环境无法取证")
    assert resolves("de3d07e") is True
    assert resolves("deadbee") is False
    assert resolves("0000000") is False
