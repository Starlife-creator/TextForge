"""#18 诊断包：把后端日志、系统信息、progress 脱敏摘要、质量报告打包成 zip。

## 安全口径（必须守住）
- **不打包 progress.json 全文**：其中 `api_config` / `models` 可能留存明文密钥，
  只抽取脱敏后的摘要字段；
- 所有进入压缩包的字典先过 `redact`；日志文本由 SensitiveFilter 在写入期已脱敏，
  这里不再做字符串替换，避免破坏日志行结构；
- 成员清单写进 `manifest.json`，收包方可核对完整性；任何敏感字段都不允许新增进
  成员表而不走 `redact`。

## 成员表
- `manifest.json`：生成时间、版本、成员清单与各成员字节数；
- `system_info.json`：运行环境快照（版本/平台/Python/路径，不含密钥）；
- `backend.log`：后端日志尾部（默认最后 2000 行）；
- `progress_summary.json`：当前任务脱敏摘要（阶段/章节/用量/最近错误/质量状态）；
- `quality_report.json`：最近一次成品质量报告（脱敏后原样附入，无则缺省）；
- `README.md`：包内容与脱敏说明。
"""

import io
import json
import os
import platform
import sys
import time
import zipfile
from pathlib import Path

from utils.appdir import data_dir
from utils.progress_io import ProgressUnreadable, read_progress
from utils.redact import _mask_text, redact
from utils.version import VERSION

# 附进诊断包的日志行数上限；超过只取尾部，避免一个包塞几十 MB。
MAX_LOG_LINES = 2000
# 附进摘要的错误日志条数上限（progress 里可能累积数百条）。
MAX_ERROR_LOGS = 100


def _system_snapshot() -> dict:
    """运行环境快照；刻意不复制环境变量表（可能夹带密钥），只取白名单字段。"""
    return {
        "version": VERSION,
        "python": sys.version.split()[0],
        "platform": f"{platform.system()} {platform.release()}",
        "machine": platform.machine(),
        "executable": sys.executable,
        "cwd": os.getcwd(),
        "app_dir": str(data_dir()),
        "debug": os.environ.get("TEXTFORGE_DEBUG", "0") == "1",
    }


def _read_log_tail(log_path: Path, max_lines: int) -> str:
    """读取日志尾部 max_lines 行；文件缺失/读失败时写占位说明，不抛异常。"""
    if not log_path.is_file():
        return f"（日志文件不存在：{log_path}）\n"
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"（日志读取失败：{e}）\n"
    lines = text.splitlines()
    if len(lines) > max_lines:
        return (f"# 仅附最后 {max_lines} 行（共 {len(lines)} 行）\n"
                + "\n".join(lines[-max_lines:]) + "\n")
    return text if text.endswith("\n") else text + "\n"


def _progress_summary(output_dir: Path) -> dict | None:
    """从 progress.json 抽取脱敏摘要；文件缺失返回 None，损坏返回错误占位。"""
    p = output_dir / "progress.json"
    if not p.is_file():
        return None
    try:
        prog = read_progress(p)
    except ProgressUnreadable:
        return {"_read_error": "progress.json 无法解析，未纳入诊断包"}
    batches = prog.get("batches") or []
    quality = prog.get("quality_report")
    summary = {
        "novel_name": prog.get("novel_name"),
        "current_phase": prog.get("current_phase"),
        "input_path": prog.get("input_path"),
        "input_mode": prog.get("input_mode"),
        "total_chapters": len(prog.get("chapters") or []),
        "batches_total": len(batches),
        "batches_phase3_done": sum(1 for b in batches if b.get("status") == "phase3_done"),
        "usage": prog.get("usage") or {},
        "error_logs": (prog.get("error_logs") or [])[-MAX_ERROR_LOGS:],
        "regen_chapters": list((prog.get("regen_count") or {}).keys()),
        "quality_report_status": quality.get("status") if isinstance(quality, dict) else None,
        "config_drift": redact(prog.get("config_drift") or []),
    }
    # 二次脱敏兜底：summary 里若残留嵌套敏感键（如 models 子项），在此统一遮罩。
    return redact(summary)


def _quality_report(output_dir: Path) -> dict | None:
    q = output_dir / "_diagnostics" / "quality_report.json"
    if not q.is_file():
        return None
    try:
        data = json.loads(q.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return redact(data)


def _readme(manifest: dict) -> str:
    return f"""# TextForge 诊断包

生成时间：{manifest['generated_at']}
后端版本：{manifest['version']}

## 成员清单
{chr(10).join(f"- `{m}`" for m in manifest['members'])}

## 脱敏说明
- 本包**不包含** progress.json 全文；模型 API URL / 密钥 / 令牌均已脱敏为 `***`。
- `backend.log` 为后端日志尾部（默认最后 {MAX_LOG_LINES} 行）：写入期由 SensitiveFilter 脱敏，入包前再对字符串模式扫一遍；但修复前落盘的历史日志不保证完全干净，上报前请人工扫一眼。
- 错误日志附最近 {MAX_ERROR_LOGS} 条。
- 上报前请再次人工扫一遍 `system_info.json` 与 `progress_summary.json`，确认无个人敏感路径需要打码。

## 问题上报
请附上本压缩包，并描述：操作步骤、期望行为、实际现象、发生时间。
"""


def build_diagnostic_bundle(output_dir: str | os.PathLike | None = None,
                            *, log_path: str | os.PathLike | None = None,
                            max_log_lines: int = MAX_LOG_LINES) -> bytes:
    """生成诊断包 zip 字节流。纯同步函数，路由层经 asyncio.to_thread 调用。

    - `output_dir`：当前任务输出目录；None 时跳过 progress / 质量报告成员。
    - `log_path`：后端日志路径；缺省取 `data_dir()/logs/backend.log`。
    """
    out_dir = Path(output_dir) if output_dir else None
    log_file = Path(log_path) if log_path else data_dir() / "logs" / "backend.log"

    system_info = _system_snapshot()
    progress_summary = _progress_summary(out_dir) if out_dir else None
    quality = _quality_report(out_dir) if out_dir else None

    members: dict[str, bytes] = {}
    members["system_info.json"] = json.dumps(
        system_info, ensure_ascii=False, indent=2).encode("utf-8")
    # 日志成员过 _mask_text 兜底：写入期的 SensitiveFilter 只覆盖修复之后的日志，
    # 修复前落盘的历史日志可能含明文密钥，这里再扫一遍字符串模式。
    raw_log = _read_log_tail(log_file, max_log_lines)
    members["backend.log"] = _mask_text(raw_log).encode("utf-8")
    if progress_summary is not None:
        members["progress_summary.json"] = json.dumps(
            progress_summary, ensure_ascii=False, indent=2).encode("utf-8")
    if quality is not None:
        members["quality_report.json"] = json.dumps(
            quality, ensure_ascii=False, indent=2).encode("utf-8")

    manifest = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "version": VERSION,
        "source_output_dir": str(out_dir) if out_dir else None,
        "members": sorted(set(members) | {"manifest.json", "README.md"}),
        "member_bytes": {name: len(blob) for name, blob in members.items()},
        "notes": [
            "本包不含 progress.json 全文；模型配置/密钥已脱敏为 ***。",
            f"backend.log 为最后 {max_log_lines} 行；错误日志附最近 {MAX_ERROR_LOGS} 条。",
        ],
    }
    members["manifest.json"] = json.dumps(
        manifest, ensure_ascii=False, indent=2).encode("utf-8")
    members["README.md"] = _readme(manifest).encode("utf-8")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(members):
            zf.writestr(name, members[name])
    return buf.getvalue()
