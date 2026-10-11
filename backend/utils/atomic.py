import tempfile, os, json
from pathlib import Path

def atomic_write_json(path: Path, data: dict, *, pretty: bool = True):
    """原子写 JSON（临时文件 + os.replace）。

    pretty=False 时不缩进、不 fsync —— 供高频写入使用（如心跳）。
    progress.json 可能因缝合锚点内嵌全文而达到数十 MB，此时 indent=2 + fsync
    会在事件循环里造成数十毫秒的停顿，10s 一次足以让 SSE 心跳抖动、
    pause/stop 等控制端点排队。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.stem + "_", suffix=".tmp")
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            # 注意：indent=None 只是不缩进，json 仍用默认分隔符 ', ' / ': '，
            # 只省 5% 体积；要真正紧凑必须显式传 separators。
            if pretty:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            else:
                json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
                f.flush()
        os.replace(tmp, path)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass
        raise

def atomic_write_text(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.stem + "_", suffix=".tmp")
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
