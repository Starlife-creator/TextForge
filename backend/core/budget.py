"""单批正文的 token 预算。

旧模型是 `chars = context_window * 1.5; batch_limit = chars * 0.40`，两个问题：
  1. 只给**正文**定额，把每次请求都随行的脚手架（蓝图≤5000字 + 作者风格 +
     保护门 + 断层清单 + 规则骨架）当成不存在 —— 这部分实测 5000~7000 token。
  2. 「1 token ≈ 1.5 字」对中文系统性高估，真实约 1 字 ≈ 1 token。

两者叠加的结果：用户把上下文设成 16384，程序按自己的算式认为放得下，
实际发出去的却是 16038 token，而本地 LM Studio 只加载了 8448 —— 直接 400，
整批返工。这里改成按 token 算，并且**先扣脚手架再定正文上限**。
"""

from utils.tokens import estimate_tokens

# 上下文安全余量：服务端 chat template / BOS / 特殊 token 等不可见开销，
# 以及本项目无法预知的各厂商差异。宁可少放一点，也不要压线触发 400。
CONTEXT_SAFETY = 0.90

# 输出预留系数：重构后正文长度与原批大致相当（中文约 1:1），留 15% 裕量。
OUTPUT_RATIO = 1.15

# 提示词固定骨架（规则条款文本）的粗估 token，用于尚未构建完整提示词时的兜底。
PROMPT_FIXED_TOKENS = 600

# 单批正文 token 下限：再小就退化成逐段调用，反而放大了脚手架开销。
MIN_BODY_TOKENS = 200

# 跨批连续性上下文（上一批结尾）的 token 预留。批大小在 phase1 就定下来，
# 那时重构稿还不存在，因此按固定上限预留，避免重构时把请求顶出窗口。
CONTINUITY_MAX_TOKENS = 600


def continuity_token_allowance(context_window: int) -> int:
    """跨批连续性上下文在单批请求里的 token 预留（保守上限）。"""
    return int(min(CONTINUITY_MAX_TOKENS, max(200, context_window * 0.04)))


# 前情摘要（上一批诊断结果）的 token 预留。它补的是「事实一致」，
# 与上一批结尾片段（补「文风衔接」）是两件事，必须分开预留。
STORY_MEMO_MAX_TOKENS = 500


def story_memo_token_allowance(context_window: int) -> int:
    """前情摘要在单批请求里的 token 预留；也用作摘要裁剪的字符上限。"""
    return int(min(STORY_MEMO_MAX_TOKENS, max(180, context_window * 0.035)))


def blueprint_token_allowance(context_window: int) -> int:
    """蓝图在重构提示词里占用的 token 预留。

    phase2 以 `min(5000, max(2000, context_window*1.5*0.05))` 字生成蓝图，
    批大小在 phase1 就要定下来（那时蓝图还没生成），所以这里按同一算式取上限预留。
    中文≈1 token/字，直接把「字」当「token」用即为偏保守的估计。
    """
    chars = context_window * 1.5
    return int(min(5000, max(2000, chars * 0.05)))


def body_token_budget(context_window: int, *, blueprint_text: str = "",
                      author_style: str = "", extra_text: str = "",
                      reserve_tokens: int = 0,
                      fixed_tokens: int = PROMPT_FIXED_TOKENS,
                      safety: float = CONTEXT_SAFETY,
                      output_ratio: float = OUTPUT_RATIO) -> int:
    """算出单批**正文**可用的 token 上限。

    blueprint_text 已知时按实测长度扣；未知（phase1）时按 blueprint_token_allowance 预留。
    reserve_tokens 用于尚未生成、但重构时一定会带上的内容（如跨批连续性上下文）。
    """
    total = context_window * safety
    scaffold = float(fixed_tokens)
    scaffold += (estimate_tokens(blueprint_text) if blueprint_text
                 else blueprint_token_allowance(context_window))
    scaffold += estimate_tokens(author_style or "")
    scaffold += estimate_tokens(extra_text or "")
    scaffold += max(0, int(reserve_tokens))
    usable = total - scaffold
    body = usable / (1 + output_ratio)
    return max(MIN_BODY_TOKENS, int(body))


def output_token_reserve(body_tokens: int, output_ratio: float = OUTPUT_RATIO) -> int:
    """与正文预算配套的 max_tokens，保证输入+输出不越过上下文。"""
    return max(64, int(body_tokens * output_ratio))