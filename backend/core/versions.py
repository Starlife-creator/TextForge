"""成品版本管理：每次重跑前把上一版成品快照到 `_versions/`。

为什么不用现有的逐文件 `_backup/`：那是「每个被覆盖的文件各存一份」，散、不可浏览、
无法作为「回到上一版」的入口。这里改为**整版快照**——重跑前先把当前 `03_final/`
整个复制成 `_versions/<时间戳>_<书名>/`，用户就能浏览并一键回滚到任意一版。

安全约束（与 output.clear 一致）：
- 只在 finalize **开始时**、真正要覆盖前才快照（避免每次空跑都堆版本）；
- 保留最近 N 版（默认 10），多的自动清理；
- 恢复时先给当前版本也做一次快照（恢复动作本身可再被回退）。
"""

import logging
import re
import shutil
import time
from pathlib import Path

logger = logging.getLogger("textforge")

VERSIONS_DIR = "_versions"
DEFAULT_KEEP = 10


def _versions_root(output_path: Path) -> Path:
    return output_path / VERSIONS_DIR


def snapshot(output_path: Path, novel_name: str = "", keep: int = DEFAULT_KEEP) -> str | None:
    """把当前 03_final 快照成一个新版本，返回版本目录名；无可快照内容返回 None。"""
    final_dir = output_path / "03_final"
    if not final_dir.is_dir():
        return None
    try:
        has_content = any(final_dir.iterdir())
    except OSError:
        return None
    if not has_content:
        return None
    root = _versions_root(output_path)
    root.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[<>:"/\\|?*]', "_", novel_name or "未命名")[:40]
    ts = time.strftime("%Y%m%d_%H%M%S")
    name = f"{ts}_{safe}"
    dest = root / name
    i = 1
    while dest.exists():
        dest = root / f"{name}_{i}"
        i += 1
    try:
        shutil.copytree(final_dir, dest)
    except Exception as e:
        logger.warning(f"[versions] 快照失败 {dest}: {e}")
        return None
    _prune(root, keep)
    logger.info(f"[versions] 已快照上一版成品 -> {dest.name}")
    return dest.name


def _prune(root: Path, keep: int) -> None:
    versions = []
    for p in root.iterdir():
        try:
            if p.is_dir():
                versions.append((p.stat().st_mtime, p.name, p))
        except OSError:
            continue
    # 同秒内创建的两个版本 mtime 相同，必须用 id 兜底排序，否则「保留最近 N 个」
    # 会随机留下旧的那个。id 以时间戳开头 + `_序号` 后缀，字典序即创建顺序。
    versions.sort(key=lambda x: (x[0], x[1]), reverse=True)
    for _, _, old in versions[keep:]:
        try:
            shutil.rmtree(old)
        except OSError as e:
            # 静默会让磁盘在「版本数看着正常、实际没删掉」的情况下悄悄膨胀。
            logger.warning(f"[versions] 删除旧版本 {old.name} 失败（可能被占用）：{e}")


def list_versions(output_path: Path) -> list[dict]:
    """列出所有成品版本（新的在前）。"""
    root = _versions_root(output_path)
    if not root.is_dir():
        return []
    out = []
    for p in root.iterdir():
        try:
            if not p.is_dir():
                continue
            files = [f for f in p.iterdir() if f.is_file()]
            total = sum(f.stat().st_size for f in files)
            out.append({
                "id": p.name,
                "mtime": int(p.stat().st_mtime),
                "files": len(files),
                "size": total,
                "names": sorted(f.name for f in files)[:12],
            })
        except OSError:
            continue
    out.sort(key=lambda x: (x["mtime"], x["id"]), reverse=True)
    return out


def restore(output_path: Path, version_id: str) -> dict:
    """把指定版本还原回 03_final/。还原前先给当前版本做一次快照（可再回退）。"""
    root = _versions_root(output_path)
    src = root / version_id
    # 目录穿越防护：version_id 只允许根目录下的直接子目录名
    if not version_id or "/" in version_id or "\\" in version_id or ".." in version_id:
        raise ValueError("非法的版本号")
    if not src.is_dir():
        raise FileNotFoundError(f"版本不存在：{version_id}")
    # 先快照当前成品，保证「恢复」这个动作本身可被回退
    cur_name = snapshot(output_path, novel_name="恢复前")
    final_dir = output_path / "03_final"
    tmp = output_path / ("._restore_tmp")
    # backup_dir 必须在 try 之前绑定：否则 copytree 一旦抛错，finally 里引用
    # 未绑定的名字会再抛 NameError，把真实异常盖掉、还残留临时目录。
    backup_dir = output_path / "._restore_old"
    try:
        if tmp.exists():
            shutil.rmtree(tmp)
        shutil.copytree(src, tmp)
        # 原子替换 03_final
        if backup_dir.exists():
            shutil.rmtree(backup_dir)
        if final_dir.exists():
            shutil.move(str(final_dir), str(backup_dir))
        shutil.move(str(tmp), str(final_dir))
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        if backup_dir.exists():
            shutil.rmtree(backup_dir, ignore_errors=True)
    logger.info(f"[versions] 已恢复成品版本 {version_id}")
    return {"restored": version_id, "snapshot_before_restore": cur_name}