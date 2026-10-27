from pathlib import Path
from utils.atomic import atomic_write_text, atomic_write_json
from utils.sort import natural_sort_key
from core.document import write_chapters_docx
import shutil, datetime, logging

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
    """缝合补丁报告。

    写到 `_diagnostics/` 而不是 `03_final/`：03_final 是交付目录，
    混进 .json 报告会让「打开成品」把报告当章节列出来（/api/final_files 只能
    靠扩展名过滤，那对 .txt 报告无效）。诊断产物应该和成品分开。

    优先从 stitch_anchors.jsonl 读取完整锚点（含原文/替换文全文）；
    若不存在则回退到 progress.json 的轻量引用（不含原文/替换文）。
    """
    import json as _json  # 本地导入确保可用
    diag_dir = output_path / "_diagnostics"
    diag_dir.mkdir(parents=True, exist_ok=True)

    # 1) 尝试从 JSONL 读取完整锚点（用于生成完整报告）
    anchors_path = diag_dir / "stitch_anchors.jsonl"
    full_anchors = []
    if anchors_path.exists():
        try:
            with open(anchors_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        full_anchors.append(_json.loads(line))
        except (OSError, _json.JSONDecodeError):
            pass

    # 2) 回退：用 progress 中的轻量引用（无原文/替换文全文）
    if not full_anchors:
        full_anchors = progress.get("stitch_anchors", [])

    txt_lines = ["缝合补丁报告", "=" * 60, ""]
    for a in full_anchors:
        txt_lines.append(f"[{a.get('source', 'unknown')}] {a['file']}")
        txt_lines.append(f"  偏移: {a.get('start')} - {a.get('end')}")
        txt_lines.append(f"  原文: {(a.get('original') or '')[:100]}...")
        txt_lines.append(f"  替换: {(a.get('replacement') or '')[:100]}...")
        txt_lines.append(f"  已应用: {a.get('applied', False)}")
        txt_lines.append("")
    atomic_write_text(diag_dir / "缝合补丁报告.txt", "\n".join(txt_lines))
    atomic_write_json(diag_dir / "缝合补丁报告.json", {"anchors": full_anchors})

def _safe_filename(name: str) -> str:
    for ch in '<>:"/\\|?*': name = name.replace(ch, '_')
    if name.upper().split('.')[0] in {"CON","PRN","AUX","NUL","COM1","COM2","COM3","COM4","COM5","COM6","COM7","COM8","COM9","LPT1","LPT2","LPT3","LPT4","LPT5","LPT6","LPT7","LPT8","LPT9"}:
        name = f"_{name}"
    return name.strip() or "重构终稿"

def _cleanup_backups(backup_dir: Path, keep: int = BACKUP_KEEP):
    """v8.8 新增：_backup 只保留最近 keep 个，避免无限增长。

    注意 p.stat() 必须包在 try 内：原实现放在 try 外面，一个并发消失或无权限的条目
    就会抛 OSError 穿透 phase4_finalize —— 此时成品其实已经写完了，却报 code="exception"
    且不发 done 事件，用户会以为失败而重跑。
    """
    if not backup_dir.exists():
        return
    stamped = []
    for p in backup_dir.iterdir():
        try:
            if p.is_file():
                stamped.append((p.stat().st_mtime, p))
        except OSError:
            continue
    stamped.sort(key=lambda x: x[0], reverse=True)
    for _, old in stamped[keep:]:
        try:
            old.unlink(missing_ok=True)
        except OSError:
            pass

def phase4_finalize(output_path: Path, progress: dict, req):
    final_dir = output_path / "03_final"
    final_dir.mkdir(parents=True, exist_ok=True)
    backup_dir = output_path / "_backup"
    output_format = req.input_format
    novel_name_raw = progress.get('novel_name') or '重构终稿'

    # 重跑覆盖前先把**上一版成品**整版快照到 _versions/，供用户一键回滚
    # （逐文件 _backup 仍在，但它们是给「单文件意外覆盖」兜底的，不作为版本入口）
    try:
        from core.versions import snapshot as _snapshot
        _snapshot(output_path, novel_name=novel_name_raw)
    except Exception as e:
        logger.warning(f"[finalize] 成品版本快照失败（继续）: {e}")
    
    original_chapters = [c for c in progress['chapters'] if not c.get('is_virtual')]
    original_chapters.sort(key=lambda c: natural_sort_key(c['filename']))
    
    merged_texts = []
    for ch in original_chapters:
        try:
            ch_file = find_reconstructed_chapter(output_path, ch['id'], progress)
            merged_texts.append(ch_file.read_text(encoding='utf-8'))
        except FileNotFoundError:
            if ch.get('is_empty'): merged_texts.append("")
            else: raise
    
    novel_name = _safe_filename(progress.get('novel_name') or '重构终稿')
    rich = bool(getattr(req, "rich_text", False))

    if req.input_mode == "single_file":
        output_file = final_dir / f"{novel_name}_重构终稿.{output_format}"
        _write_with_backup(output_file, merged_texts, output_format, backup_dir, rich=rich)
    else:
        # 逐章文件名必须去非法字符 + 去重：多文件模式允许 txt/md/docx 混放，
        # 「001.txt」与「001.md」会生成同一个 重构_001.{fmt}，后者静默覆盖前者。
        used = set()
        for ch, text in zip(original_chapters, merged_texts, strict=False):
            stem = _safe_filename(Path(ch['filename']).stem)
            out_name = f"重构_{stem}.{output_format}"
            if out_name in used:
                n = 2
                while f"重构_{stem}({n}).{output_format}" in used:
                    n += 1
                out_name = f"重构_{stem}({n}).{output_format}"
            used.add(out_name)
            output_file = final_dir / out_name
            _write_with_backup(output_file, [text], output_format, backup_dir, rich=rich)
        output_file = final_dir / f"{novel_name}_重构终稿.{output_format}"
        _write_with_backup(output_file, merged_texts, output_format, backup_dir, rich=rich)

    # #20：勾选 export_epub 时额外产出 EPUB（导航目录 + 粗斜体映射）；失败不阻断其他格式交付。
    if getattr(req, "export_epub", False):
        from core.epub import write_epub
        epub_chapters = [
            {"id": ch["id"], "title": ch.get("title") or ch.get("filename"), "text": text}
            for ch, text in zip(original_chapters, merged_texts, strict=False)
        ]
        try:
            write_epub(final_dir / f"{novel_name}_重构终稿.epub", epub_chapters,
                       title=novel_name_raw)
            progress["epub_export"] = {"status": "ok",
                                       "generated_at": datetime.datetime.now().isoformat(timespec="seconds")}
        except Exception as e:
            # 与质量报告同一口径：失败不拖垮其他格式，但必须留痕，否则用户勾了
            # export_epub 却没拿到产物时界面仍显示完成、历史里无任何线索。
            progress["epub_export"] = {
                "status": "failed",
                "error": f"{type(e).__name__}: {e}"[:500],
                "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
            }
            logger.warning(f"[finalize] EPUB 导出失败（继续交付其他格式）: {e}", exc_info=True)

    _cleanup_backups(backup_dir)
    write_stitch_report(output_path, progress)
    write_quality_report_safely(
        output_path, progress,
        chapters=original_chapters, texts=merged_texts, req=req,
    )
    logger.info(f"[finalize] 输出完成 -> {final_dir}")


def write_quality_report_safely(output_path: Path, progress: dict, *,
                                chapters: list, texts: list, req) -> None:
    """生成质量报告。失败**不拖垮**已写完的成品，但原因必须落进 progress。

    原先这里只有一行 `logger.warning`：打包版用户看不到日志，界面只会显示
    「尚无报告」；CI 里也取不到值 —— v0.13.0 首次发布时就丢过一次报告，
    端到端只拿到一个 404，没有任何线索可查。
    """
    try:
        from core.quality import build_quality_report, write_quality_report
        report = build_quality_report(
            chapters=chapters, texts=texts,
            progress=progress, req=req, output_path=output_path,
        )
        report_path = write_quality_report(output_path, report)
        progress["quality_report"] = {
            "status": report["status"],
            "path": str(report_path),
            "generated_at": report["generated_at"],
            "issues_by_severity": report["summary"]["issues_by_severity"],
        }
        logger.info(f"[finalize] 质量报告 -> {report_path} status={report['status']}")
    except Exception as e:
        progress["quality_report"] = {
            "status": "failed",
            "error": f"{type(e).__name__}: {e}"[:500],
            "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        }
        logger.warning(f"[finalize] 质量报告生成失败（成品照常交付）: {e}", exc_info=True)

def _write_with_backup(output_file: Path, texts: list, ext: str, backup_dir: Path,
                       *, rich: bool = False):
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
        write_chapters_docx(texts, str(output_file), rich=rich, title=output_file.stem)
    else:
        atomic_write_text(output_file, "\n\n---\n\n".join(texts))
