"""离线 token 估算（CJK 启发式）。

为什么不用 tiktoken：它首次运行要下载 BPE 词表文件。本项目的主要部署形态是
**本地离线模型**（LM Studio / Ollama），在断网或内网机器上 tiktoken 会直接不可用，
而这恰恰是最需要精确控制上下文的那批用户。

为什么必须有它：旧代码用 `chars = context_window * 1.5` 估算，隐含「1 token ≈ 1.5 字」。
这个比例对英文勉强成立，对中文系统性高估约 50%（真实约 1 字 ≈ 1 token），
叠加「批大小只算正文、漏算蓝图等脚手架」，最终请求 token 远超模型窗口，
在本地部署上直接 400 报错，整批返工。

精度说明：本估算**故意偏保守**（宁可高估 token）。高估的代价是批次略小、
请求略多；低估的代价是 400 整批报废 —— 两者不对称，必须往保守一侧偏。
"""

# 聊天模板开销：每条 message 的 role/分隔符等固定 token（OpenAI 格式约 7~10）
MESSAGE_OVERHEAD = 8

# 服务端 chat template / BOS 等一次性开销
REPLY_OVERHEAD = 4


def _is_cjk(ch: str) -> bool:
    """CJK 表意文字 + 中日韩标点/全角符号。

    把中日韩标点也算进来：它们的 BPE 长度同样接近 1 个汉字，
    而中文正文里逗号句号书名号的出现频率和实义字是同一量级。
    """
    o = ord(ch)
    return (
        0x4E00 <= o <= 0x9FFF      # CJK 统一表意文字
        or 0x3400 <= o <= 0x4DBF   # 扩展 A
        or 0x3000 <= o <= 0x303F   # 中日韩符号和标点
        or 0xFF00 <= o <= 0xFFEF   # 全角字符
    )


def estimate_tokens(text: str) -> int:
    """估算一段文本的 token 数。

    规则（保守）：
      - CJK 字符（含全角标点）：1 字 ≈ 1 token
      - ASCII 字母数字：4 字符 ≈ 1 token
      - 其余（空格、换行、西文标点）：2 字符 ≈ 1 token

    罕见汉字、生僻字实际可能是 2~3 token，这里按 1 算 —— 这是有意的：
    高估/低估的不对称性见模块文档。
    """
    if not text:
        return 0
    cjk = alnum = other = 0
    for ch in text:
        if _is_cjk(ch):
            cjk += 1
        elif ch.isalnum() and ord(ch) < 128:
            alnum += 1
        else:
            other += 1
    return cjk + (alnum + 3) // 4 + (other + 1) // 2


def estimate_messages_tokens(messages) -> int:
    """估算整个 messages 数组的 token 数（含每条的模板开销）。"""
    if not messages:
        return 0
    total = REPLY_OVERHEAD
    for m in messages:
        content = m.get("content") or ""
        if not isinstance(content, str):
            # 多模态/结构化 content 走不到这里，保守按 repr 估
            content = str(content)
        total += estimate_tokens(content) + MESSAGE_OVERHEAD
    return total


def estimate_payload_tokens(payload: dict) -> int:
    """估算一次请求 payload 的**输入侧** token 数（不含 max_tokens 预留）。"""
    return estimate_messages_tokens(payload.get("messages"))