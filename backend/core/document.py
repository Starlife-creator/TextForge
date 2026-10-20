from docx import Document
from pathlib import Path
import tempfile, os, logging

logger = logging.getLogger("textforge")

def write_text_docx(text: str, output_path: str):
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    doc = Document()
    written = 0
    for para in text.split("\n\n"):
        para = para.strip('\n')
        # 跳过全空段："\n\n\n\n".split("\n\n") 会产出 ['', '']，照写就是空段落
        if not para.strip():
            continue
        p = doc.add_paragraph()
        lines = para.split("\n")
        for i, line in enumerate(lines):
            # 原实现每行末尾都 add_break()，导致每个段落结尾多出一个空行；
            # 改成「行与行之间」才换行，段落尾部不再拖尾
            if i:
                p.add_run().add_break()
            if line:
                p.add_run(line)
        written += 1
    if written == 0:
        # 跳过空段之后，全空白输入会产出一个零段落的空 docx，必须留痕
        logger.warning(f"[docx] 输入全为空白，生成的是空文档: {output_path}")
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=out.parent, prefix=out.stem + "_", suffix=".docx")
    try:
        # 原实现先 os.close(fd) 再 doc.save(tmp)：fdopen 失败时 fd 泄漏，
        # 且 Windows 上对已关闭句柄 unlink 会失败，留下孤儿临时文件
        with os.fdopen(fd, 'wb') as f:
            doc.save(f)
        os.replace(tmp, out)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass
        raise