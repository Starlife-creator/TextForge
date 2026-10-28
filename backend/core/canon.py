"""#5 人物 / 伏笔档案最小闭环：项目级持久化 + 按预算注入命中条目。

现状：跨批次一致性只靠「上一章结尾」和短 memo，人物称谓、伏笔状态没有累积档案，
长书跑到几十批后称谓漂移、伏笔忘回收只能靠人工抽读。

本模块先落最小闭环（产品化深度抽取属后续范围，见模块末尾说明）：
- 人物表：姓名 / 别名 / 身份 / 状态 / 出场章；
- 伏笔表：描述 / 埋设章 / 回收章 / 状态；
- 持久化到 `02_workspace/canon/canon.json` 与 `canon.md`；
- 重构提示词按字符预算注入命中条目（关键词匹配优先，常驻条目补预算，未命中不占预算）。

**未落地范围（🟡）**：LLM 自动识别新人物 / 新伏笔、状态机从「未回收」推进到「已回收」、
人物关系网与地点/物品档案；本轮 `apply_batch` 只把已知人物的出场章追加进档案。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from utils.atomic import atomic_write_json

logger = logging.getLogger("textforge")

CANON_DIR = Path("02_workspace") / "canon"
CANON_JSON = "canon.json"
CANON_MD = "canon.md"

#: 注入提示词的档案字符预算（中文约 1 字 ≈ 1 token；占 context_window 的小份额）。
CANON_BUDGET_CHARS = 1200

PERSON_FIELDS = ("name", "aliases", "identity", "status", "chapters")
FORESHADOW_FIELDS = ("id", "description", "planted_chapter", "resolved_chapter", "status")


def empty_canon() -> dict:
    return {"version": 1, "persons": [], "foreshadows": []}


def _canon_dir(output_path: Path) -> Path:
    return Path(output_path) / CANON_DIR


def _canon_json_path(output_path: Path) -> Path:
    return _canon_dir(output_path) / CANON_JSON


def load_canon(output_path: Path) -> dict:
    """读取档案；文件缺失 / 损坏 / 结构不符时返回空骨架，不抛到流水线主循环。

    手改过的 canon.json 可能缺 chapters 键或含缺 name 的脏条目：这里就地清洗，
    让后续 `person["name"]` / `person["chapters"]` 不会 KeyError，也不把脏数据
    静默带进批次提示词。
    """
    path = _canon_json_path(output_path)
    if not path.exists():
        return empty_canon()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty_canon()
    if not isinstance(data, dict) or not isinstance(data.get("persons"), list):
        return empty_canon()
    cleaned_persons: list[dict] = []
    for p in data["persons"]:
        if not isinstance(p, dict) or not str(p.get("name") or "").strip():
            continue  # 缺 name 的脏条目直接丢弃，不进后续索引
        p.setdefault("aliases", [])
        p.setdefault("identity", "")
        p.setdefault("status", "")
        p.setdefault("chapters", [])
        cleaned_persons.append(p)
    data["persons"] = cleaned_persons
    if not isinstance(data.get("foreshadows"), list):
        data["foreshadows"] = []
    data.setdefault("version", 1)
    return data


def save_canon(output_path: Path, canon: dict) -> Path:
    """原子写 canon.json，并同步生成人读版 canon.md。"""
    path = _canon_json_path(output_path)
    atomic_write_json(path, canon)
    (path.parent / CANON_MD).write_text(render_markdown(canon), encoding="utf-8")
    return path


def _dedupe(items: list) -> list:
    out: list = []
    for item in items:
        if item and item not in out:
            out.append(item)
    return out


def upsert_person(canon: dict, *, name: str, aliases: tuple | list = (),
                  identity: str = "", status: str = "", chapter: str | None = None) -> dict:
    """登记或更新人物；按姓名/别名去重，新出场章追加进 chapters 列表。"""
    name = str(name).strip()
    if not name:
        raise ValueError("人物姓名不能为空")
    entry: dict | None = None
    for person in canon["persons"]:
        if person["name"] == name or name in person.get("aliases", []):
            entry = person
            break
    if entry is None:
        entry = {"name": name, "aliases": [], "identity": identity,
                 "status": status, "chapters": []}
        canon["persons"].append(entry)
    entry["aliases"] = _dedupe(entry.get("aliases", []) + list(aliases))
    if identity:
        entry["identity"] = identity
    if status:
        entry["status"] = status
    if chapter and str(chapter) not in entry["chapters"]:
        entry["chapters"].append(str(chapter))
    return entry


def upsert_foreshadow(canon: dict, *, description: str, chapter: str | None = None,
                      status: str = "open", planted_chapter: str | None = None,
                      resolved_chapter: str | None = None) -> dict:
    """登记伏笔；同描述未回收条目复用，否则追加新条目。"""
    description = str(description).strip()
    if not description:
        raise ValueError("伏笔描述不能为空")
    entry: dict | None = None
    for f in canon["foreshadows"]:
        if f["description"] == description and f["status"] != "resolved":
            entry = f
            break
    if entry is None:
        entry = {
            "id": f"f{len(canon['foreshadows']) + 1}",
            "description": description,
            "planted_chapter": planted_chapter or chapter,
            "resolved_chapter": None,
            "status": status,
        }
        canon["foreshadows"].append(entry)
    if status:
        entry["status"] = status
    if resolved_chapter:
        entry["resolved_chapter"] = str(resolved_chapter)
        entry["status"] = "resolved"
    elif planted_chapter:
        entry["planted_chapter"] = str(planted_chapter)
    return entry


def apply_batch(canon: dict, chapter_id: str, text: str) -> None:
    """批次落盘后的增量更新：已知人物在本章出现就补记出场章。

    本轮不做新人物/新伏笔的 LLM 抽取（见模块 docstring 未落地范围）。
    """
    text = text or ""
    for person in canon["persons"]:
        candidates = [person["name"], *person.get("aliases", [])]
        if any(name and name in text for name in candidates):
            if str(chapter_id) not in person["chapters"]:
                person["chapters"].append(str(chapter_id))


def _person_line(p: dict) -> str:
    aliases = "、".join(p.get("aliases") or [])
    alias_part = f"（别名：{aliases}）" if aliases else ""
    identity = p.get("identity") or "身份待补"
    status = p.get("status") or "状态未明"
    chapters = "、".join(str(c) for c in p.get("chapters") or []) or "—"
    return f"- **{p['name']}**{alias_part}：身份={identity}；状态={status}；出场章={chapters}"


def _foreshadow_line(f: dict) -> str:
    state = "已回收" if f.get("status") == "resolved" else "未回收"
    planted = f.get("planted_chapter") or "—"
    resolved = f.get("resolved_chapter") or "—"
    return f"- [{state}] {f['description']}（埋设章={planted}；回收章={resolved}）"


def render_markdown(canon: dict) -> str:
    lines = ["# 人物 / 伏笔档案（自动维护，供一致性参照）", ""]
    lines.append("## 人物表")
    if canon["persons"]:
        lines.extend(_person_line(p) for p in canon["persons"])
    else:
        lines.append("- （暂无人物记录）")
    lines.append("")
    lines.append("## 伏笔表")
    if canon["foreshadows"]:
        lines.extend(_foreshadow_line(f) for f in canon["foreshadows"])
    else:
        lines.append("- （暂无伏笔记录）")
    return "\n".join(lines) + "\n"


def build_canon_context(canon: dict, *, keywords: tuple[str, ...] = (),
                        budget_chars: int = CANON_BUDGET_CHARS) -> str:
    """按预算注入档案条目：关键词命中优先，其余常驻条目补预算；未命中不占预算。

    返回空字符串表示本轮无档案可注入（空档案 / 全部未命中且预算内无常驻条目）。
    """
    persons = canon.get("persons") or []
    foreshadows = canon.get("foreshadows") or []
    keywords = tuple(k for k in keywords if str(k).strip())

    def _hit(*candidates: str) -> bool:
        return any(k and c and k in c for c in candidates for k in keywords)

    p_hit = [p for p in persons if _hit(p["name"], *p.get("aliases", []))]
    p_rest = [p for p in persons if p not in p_hit]
    f_hit = [f for f in foreshadows if _hit(f["description"])]
    f_rest = [f for f in foreshadows if f not in f_hit]

    block: list[str] = []
    used = 0

    def _add(line: str) -> bool:
        nonlocal used
        if used + len(line) > budget_chars:
            return False
        block.append(line)
        used += len(line)
        return True

    if persons:
        _add("【人物档案（仅用于保持称谓与状态一致，不要复述进正文）】")
        for p in p_hit + p_rest:
            _add(_person_line(p))
    if foreshadows:
        _add("【伏笔档案（保持埋设与回收的连续性，不要剧透回收安排）】")
        for f in f_hit + f_rest:
            _add(_foreshadow_line(f))
    return "\n".join(block)
