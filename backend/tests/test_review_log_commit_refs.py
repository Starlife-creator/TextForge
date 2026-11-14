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

浅克隆下必须**跳过而不是红**：`git cat-file -e` 在 `--depth 1` 的仓库里对旧提交
返回 128（已实测），那不是证据失效，是这个环境根本没带那段历史。
"""
from __future__ import annotations

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


def test_repo_is_usable_or_skip() -> None:
    if not git_available():
        pytest.skip("不是 git 工作区，短号取证无意义")
    if is_shallow():
        pytest.skip("浅克隆（无完整历史），`git cat-file` 对旧 commit 必然失败，属环境限制")


def test_review_log_shas_all_resolve(log_text: str) -> None:
    """真实的 REVIEW-LOG：每一个 `✅ 短号` 都必须能解析。"""
    if not git_available() or is_shallow():
        pytest.skip("环境无法取证")
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
