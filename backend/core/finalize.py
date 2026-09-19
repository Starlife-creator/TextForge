from pathlib import Path
from utils.atomic import atomic_write_text, atomic_write_json
from utils.sort import natural_sort_key
from core.document import write_text_docx
import shutil, datetime, json, logging

logger = logging.getLogger("textforge")

# v8.8：_backup 最多保留最近 N 个备份
BACKUP_KEEP = 20

def find_reconstructed_chapter(output_path: Path, chapter_id: str, progress: dict) -> Path:
    recon_dir = output_path / "02_workspace/reconstructed"
    ch_file = recon_dir / f"chapter_{chapter_id}.txt"
    if ch_file.exists(): return ch_file
    virtuals = [v for v in progress.get('virtual_chapter_map', []) if v['original_id'] == chapter_id]
    if virtuals:
        raise FileNotFoundError(
            f"第 {chapter_id} 章被切分为 {len(virtuals)} 个虚拟章但未合并，"
            f"请检查缝合阶段日志（merge_virtual_chapters 是否执行、stitch_done 事件是否发出）"
        )
    raise FileNotFoundError(f"找不到章节 {chapter_id} 的重构稿：{ch_file}")

def write_stitch_report(output_path: Path, progress: dict):
    final_dir = output_path / "03_final"
    final_dir.mkdir(parents=True, exist_ok=True)
    anchors = progress.get("stitch_anchors", [])
    txt_lines = ["缝合补丁报告", "=" * 60, ""]
    for a in anchors:
        txt_lines.append(f"[{a.get('source', 'unknown')}] {a['file']}")
        txt_lines.append(f"  偏移: {a['start']} - {a['end']}")
        txt_lines.append(f"  原文: {a['original'][:100]}...")
        txt_lines.append(f"  替换: {a['replacement'][:100]}...")
        txt_lines.append(f"  已应用: {a.get('applied', False)}")
        txt_lines.append("")
    atomic_write_text(final_dir / "缝合补丁报告.txt", "\n".join(txt_lines))
    atomic_write_json(final_dir / "缝合补丁报告.json", {"anchors": anchors})

def _safe_filename(name: str) -> str:
    for ch in '<>:"/\\|?*': name = name.replace(ch, '_')
    if name.upper().split('.')[0] in {"CON","PRN","AUX","NUL","COM1","COM2","COM3","COM4","COM5","COM6","COM7","COM8","COM9","LPT1","LPT2","LPT3","LPT4","LPT5","LPT6","LPT7","LPT8","LPT9"}:
        name = f"_{name}"
    return name.strip() or "重构终稿"

def _cleanup_backups(backup_dir: Path, keep: int = BACKUP_KEEP):
    """v8.8 新增：_backup 只保留最近 keep 个，避免无限增长。"""
    if not backup_dir.exists():
        return
    backups = sorted(backup_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in backups[keep:]:
        try:
            old.unlink(missing_ok=True)
        except Exception:
            pass

def phase4_finalize(output_path: Path, progress: dict, req):
    final_dir = output_path / "03_final"
    final_dir.mkdir(parents=True, exist_ok=True)
    backup_dir = output_path / "_backup"
    output_format = req.input_format
    
    original_chapters = [c for c in progress['chapters'] if not c.get('is_virtual')]
    original_chapters.sort(key=lambda c: natural_sort_key(c['filename']))
    
    merged_texts = []
    for ch in original_chapters:
        try:
            ch_file = find_reconstructed_chapter(output_path, ch['id'], progress)
            merged_texts.append(ch_file.read_text(encoding='utf-8', newline=''))
        except FileNotFoundError:
            if ch.get('is_empty'): merged_texts.append("")
            else: raise
    
    novel_name = _safe_filename(progress['novel_name'])
    
    if req.input_mode == "single_file":
        output_file = final_dir / f"{novel_name}_重构终稿.{output_format}"
        _write_with_backup(output_file, merged_texts, output_format, backup_dir)
    else:
        for ch, text in zip(original_chapters, merged_texts):
            out_name = f"重构_{Path(ch['filename']).stem}.{output_format}"
            output_file = final_dir / out_name
            _write_with_backup(output_file, [text], output_format, backup_dir)
        output_file = final_dir / f"{novel_name}_重构终稿.{output_format}"
        _write_with_backup(output_file, merged_texts, output_format, backup_dir)
    
    _cleanup_backups(backup_dir)
    write_stitch_report(output_path, progress)
    logger.info(f"[finalize] 输出完成 -> {final_dir}")

def _write_with_backup(output_file: Path, texts: list, ext: str, backup_dir: Path):
    if output_file.exists():
        backup_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        i = 1
        while True:
            backup_file = backup_dir / f"{output_file.stem}_备份_{ts}_{i}{output_file.suffix}"
            if not backup_file.exists(): break
            i += 1
        shutil.copy2(output_file, backup_file)
    if ext == "docx":
        write_text_docx("\n\n---\n\n".join(texts), str(output_file))
    else:
        atomic_write_text(output_file, "\n\n---\n\n".join(texts))
