"""批收口块的「验证范围」字段门禁（`docs/REVIEW-LOG.md`）。

只加列不加检查，两批之后就会有人漏填——列还在、内容空了。所以这里把收口块
变成可执行判据：

1. 每个 `### 批 N` 段都必须有、且**只有一条** `> 验证范围（批 N）：…`；
2. 取值只能落在枚举形态里（两种 `全量已跑`、一种「项数未记」的历史形态、
   一种 `全量未跑（纯文档批次）`），自由文本一律红；
3. `全量未跑（纯文档批次）` 只在**该批正文里没有出现代码文件**时才成立，
   否则「全量未跑」四个字就能绕过全文——豁免条件等于没有。

第 3 条是这条门禁的牙齿：它把「豁免」从一句自我声明变成可核对的事实。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOG = ROOT / "docs" / "REVIEW-LOG.md"

_BATCH_HEAD = re.compile(r"^### 批 (\d+)")
_ANY_HEAD = re.compile(r"^#{1,3} ")
_SCOPE = re.compile(r"^>\s*验证范围（批 (\d+)）：(.+)$")

# 允许的取值形态。宁可是"几种固定写法"，也不要"看起来合理就过"——
# 后者等于把判据交回给写日志的人。
_ALLOWED = (
    re.compile(r"^全量已跑（\d+ 项 / 耗时\s*[^）]+）$"),
    re.compile(r"^全量已跑（\d+ 项 / 耗时未记）；文件级范围未记（历史补记）$"),
    # 允许原样引用当时的记录（如「252 passed + 1 skipped」），但必须仍带
    # 「/ 耗时未记）；文件级范围未记（历史补记）」，判据依然是结构化的。
    re.compile(r"^全量已跑（[^）]{1,60} / 耗时未记）；文件级范围未记（历史补记）$"),
    # 历史批当时就没记全量：明写"没记"，而不是补一个编出来的数。
    re.compile(r"^全量未记（历史批；[^）]{1,80}）$"),
    re.compile(r"^全量未跑（纯文档批次）$"),
)

# 一批正文里出现这些后缀即为"动过代码"，此时不允许声明纯文档批次。
_CODE_FILE = re.compile(
    r"(?<![\w-])[\w./\\-]+\.(?:py|pyi|ts|tsx|vue|rs|sql|bat|ps1|mjs|cjs|js|toml|ini|yml|yaml)\b"
)


def batch_regions(text: str) -> dict[int, str]:
    """`### 批 N` 到下一个一/二/三级标题之间的正文。"""
    lines = text.splitlines()
    heads: list[tuple[int, int]] = []
    for i, line in enumerate(lines):
        m = _BATCH_HEAD.match(line)
        if m:
            heads.append((i, int(m.group(1))))
    out: dict[int, str] = {}
    for start, bid in heads:
        end = len(lines)
        for j in range(start + 1, len(lines)):
            if _ANY_HEAD.match(lines[j]):
                end = j
                break
        out[bid] = "\n".join(lines[start:end])
    return out


def scope_lines(text: str) -> dict[int, list[str]]:
    """收口块里的 `> 验证范围（批 N）：取值`，按批次归集（保留重复以便判错）。"""
    got: dict[int, list[str]] = {}
    for line in text.splitlines():
        m = _SCOPE.match(line)
        if m:
            got.setdefault(int(m.group(1)), []).append(m.group(2).strip())
    return got


def validate(text: str) -> list[str]:
    """返回错误列表；空列表 = 通过。"""
    errors: list[str] = []
    regions = batch_regions(text)
    if not regions:
        return ["没有解析到任何 `### 批 N` 段（判据失去锚点）"]

    scopes = scope_lines(text)
    for bid in sorted(regions):
        values = scopes.get(bid, [])
        if not values:
            errors.append(f"批 {bid} 缺少 `> 验证范围（批 {bid}）：` 行")
            continue
        if len(values) > 1:
            errors.append(f"批 {bid} 有 {len(values)} 条验证范围，要求恰好一条")
            continue
        value = values[0]
        if not any(p.match(value) for p in _ALLOWED):
            errors.append(f"批 {bid} 的验证范围取值不在枚举内：{value}")
            continue
        if value.startswith("全量未跑"):
            hit = _CODE_FILE.search(regions[bid])
            if hit:
                errors.append(
                    f"批 {bid} 声明「纯文档批次」，但正文出现代码文件 {hit.group(0)}"
                )

    for bid in sorted(set(scopes) - set(regions)):
        errors.append(f"验证范围指向不存在的批次：批 {bid}")
    return errors


# ---------------------------------------------------------------- 夹具

_OK_DOC = """## 2. 方案

### 批 1 —— 示例批次

| # | 改动 | DoD |
|---|---|---|
| B1.1 | 改 `backend/core/versions.py` 的判据 | 内容等于新内容 |

> **批 1 收口（2026-10-29）**：
> 验证范围（批 1）：全量已跑（12 项 / 耗时未记）
"""

_DOC_ONLY_DOC = """## 2. 方案

### 批 1 —— 只改文档

| # | 改动 | DoD |
|---|---|---|
| B1.1 | 补 `docs/REVIEW-LOG.md` 的措辞 | 读得通 |

> **批 1 收口（2026-10-29）**：
> 验证范围（批 1）：全量未跑（纯文档批次）
"""


def test_review_log_passes_own_gate():
    """真实文档必须通过；顺便确认判据真的扫到了 4 个批次。"""
    text = LOG.read_text(encoding="utf-8")
    assert len(batch_regions(text)) >= 4
    assert validate(text) == []


def test_missing_scope_line_is_red():
    """删掉收口块里的验证范围 ⇒ 必红（这就是"只加列不加检查"的失效形态）。"""
    broken = _OK_DOC.replace("> 验证范围（批 1）：全量已跑（12 项 / 耗时未记）\n", "")
    assert validate(broken) == ["批 1 缺少 `> 验证范围（批 1）：` 行"]


def test_doc_only_exemption_needs_no_code_in_batch():
    """「全量未跑（纯文档批次）」只在正文没有代码文件时成立。

    把同一批的正文换成带代码文件、却仍写「全量未跑」⇒ 必红。
    没有这条，任何人写四个字就能跳过全量跑。
    """
    assert validate(_DOC_ONLY_DOC) == []
    sneaky = _DOC_ONLY_DOC.replace(
        "| B1.1 | 补 `docs/REVIEW-LOG.md` 的措辞 | 读得通 |",
        "| B1.1 | 改 `backend/core/refactor.py` 的合并写 | 并发两章都在 |",
    )
    errors = validate(sneaky)
    assert len(errors) == 1
    assert "声明「纯文档批次」" in errors[0]
    assert "refactor.py" in errors[0]


def test_free_text_value_is_red():
    """自由文本一律红：随手写「这次先不跑」不能算豁免。"""
    broken = _OK_DOC.replace(
        "全量已跑（12 项 / 耗时未记）", "这次先不跑，下次补"
    )
    errors = validate(broken)
    assert len(errors) == 1
    assert "不在枚举内" in errors[0]


def test_duplicate_and_orphan_scope_lines_are_red():
    """同一批两条 ⇒ 红；指向不存在的批次 ⇒ 红。"""
    dup = _OK_DOC + "> 验证范围（批 1）：全量已跑（12 项 / 耗时未记）\n"
    assert "要求恰好一条" in validate(dup)[0]

    orphan = _OK_DOC + "> 验证范围（批 9）：全量已跑（12 项 / 耗时未记）\n"
    assert validate(orphan) == ["验证范围指向不存在的批次：批 9"]


def test_restoring_the_line_turns_it_green_again():
    """反向断言必须双向：改回原样 ⇒ 复绿。只绿不红等于没装护栏。"""
    ok = _OK_DOC
    broken = ok.replace("> 验证范围（批 1）：全量已跑（12 项 / 耗时未记）\n", "")
    assert validate(broken) != []
    assert validate(ok) == []
