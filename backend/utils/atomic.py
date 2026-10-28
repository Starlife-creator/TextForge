import tempfile, os, json, time
from pathlib import Path


def replace_with_retry(tmp: str, path: Path) -> None:
    """os.replace 的 Windows 容错版本。

    Windows 上杀软/资源管理器/另一个线程正打开目标文件时，os.replace 会抛
    PermissionError [WinError 5]，而不是 POSIX 上的原子替换。e2e 实测：控制端点把
    Story_Bible.md 的写丢进 worker 线程，同时 status 线程正在读它，无互斥即撞。

    只对 PermissionError 退避重试（20/40/80/160/320ms，总上界 ~620ms，远小于
    任何闸门超时）；FileNotFoundError/其他 OSError 直接上抛，不吞真问题。
    """
    delays = (0.02, 0.04, 0.08, 0.16, 0.32)
    last = None
    for i in range(len(delays) + 1):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as e:
            last = e
            if i < len(delays):
                time.sleep(delays[i])
            else:
                raise
    if last is not None:
        raise last


# 内部别名（旧调用点保留 _replace_with_retry 名称）
_replace_with_retry = replace_with_retry


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
        _replace_with_retry(tmp, path)
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
        _replace_with_retry(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
