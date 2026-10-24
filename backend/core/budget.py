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

# 「窗口 token 数 → 中文字数」的换算。下面这些窗口派生上限全部从它推导，
# 所以这个 1.5 只允许出现在这一处：splitter / refactor / blueprint / control /
# estimate 曾经各写一份，改一处漏四处。
WINDOW_CHAR_RATIO = 1.5


def window_char_capacity(context_window: int) -> int:
    """把模型上下文窗口（token）折算成「大约能放多少中文字」。

    这是历史口径，只用于**按字切文本**的场景（装批前的窗口字数、缝合交界窗口、
    蓝图总预算）。正文 token 预算一律走 `body_token_budget`，不要拿这个数当依据。
    """
    return int(context_window * WINDOW_CHAR_RATIO)


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

    批大小在 phase1 就要定下来（那时蓝图还没生成），所以按生成上限预留。
    中文≈1 token/字，直接把「字」当「token」用即为偏保守的估计。
    """
    return blueprint_chars(context_window)


# 蓝图字数上限的算式：phase2 生成、重构预留、（间接）缝合注入三处都从这里取。
# 这三个数一旦各写一份，就会出现「生成 wrote 5000 字而预留按另一套算」的静默错配。
BLUEPRINT_MIN_CHARS = 2000
BLUEPRINT_MAX_CHARS = 5000
BLUEPRINT_RATIO = 0.05


def blueprint_chars(context_window: int) -> int:
    """phase2 蓝图的字数上限。"""
    chars = window_char_capacity(context_window)
    return int(min(BLUEPRINT_MAX_CHARS, max(BLUEPRINT_MIN_CHARS, chars * BLUEPRINT_RATIO)))


# 确认接口能接受的蓝图长度，相对窗口字数的比例（沿用 control.py 的原值）。
BLUEPRINT_ACCEPT_RATIO = 0.20


def blueprint_accept_max_chars(context_window: int) -> int:
    """`/api/blueprint/confirm` 能接受的蓝图最大长度。

    必须 **≥ `blueprint_chars()`**：否则 phase2 自己按上限生成的蓝图，用户在闸门里
    原样点「确认」会被这个接口 422 拒掉（窗口很小时真的会撞上）。
    """
    return max(
        blueprint_chars(context_window),
        int(window_char_capacity(context_window) * BLUEPRINT_ACCEPT_RATIO),
    )


# 单批最多几章：phase1 装批、拆批降级、开跑前预估共用同一个上限。
MAX_CHAPTERS_PER_BATCH = 8

# 诊断阶段「上一批尾巴」重叠上下文占单批正文预算的比例
# （沿用原 0.15/0.40 的相对比例，只是口径从字换成 token）。
OVERLAP_RATIO = 0.375

# 缝合的交界窗口：按窗口字数线性取，但**必须封顶** —— 不封顶时窗口随用户填的
# 上下文无限放大，128k 下算出 14745 字，比一整章还长，「修补交界」就变成「替换整章」。
STITCH_WINDOW_RATIO = 0.075
STITCH_MAX_WINDOW_CHARS = 1200


def stitch_window_chars(window_chars: int) -> int:
    """交界窗口字数（缝合时前文结尾 / 后文开头各取这么长）。"""
    return int(min(int(window_chars * STITCH_WINDOW_RATIO), STITCH_MAX_WINDOW_CHARS))


# 缝合请求里随行的全局设定（蓝图切片）比例。它实际被蓝图自身上限饱和，
# 所以这里不另设封顶：5000 字蓝图就是天花板。
BLUEPRINT_FOR_STITCH_RATIO = 0.10


def blueprint_for_stitch_chars(window_chars: int) -> int:
    """喂给缝合器的蓝图切片长度。"""
    return int(window_chars * BLUEPRINT_FOR_STITCH_RATIO)


# 最终蓝图上下文的总预算（blueprint.build_blueprint_context 与开跑前预估共用）。
BLUEPRINT_CONTEXT_RATIO = 0.60


def blueprint_context_chars(window_chars: int) -> int:
    """蓝图阶段能用的上下文字数上限。"""
    return int(window_chars * BLUEPRINT_CONTEXT_RATIO)


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


# 诊断输出：每章摘要要求 ≤150 字，另加人物状态与断层诊断；勾选结构化诊断时
# 末尾还要再附一个 JSON 代码块。
DIAG_TOKENS_PER_CHAPTER = 260
DIAG_TOKENS_FLOOR = 600
DIAG_JSON_TOKENS = 900
DIAG_TOKENS_CAP = 4000


def diagnose_max_tokens(n_chapters: int, with_json: bool = False) -> int:
    """诊断阶段的输出预留，按**本批章数**缩放。

    旧实现固定 2000：8 章的书光摘要就要 1200 字，加上人物状态、断层诊断，再挂上
    排在**最末尾**的 ```json``` 块，必然被截尾。而 `_parse_diag_json` 解析不到就只写
    一行日志继续跑 —— 表现为「明明勾了修断层，产出的清单却是空的」，用户毫无线索。
    上限 4000：单批章数由 pack_groups 限制在 8 以内，超过 4000 说明提示词要求被放宽了，
    那时该改的是提示词而不是继续加 max_tokens。
    """
    base = DIAG_TOKENS_FLOOR + max(1, int(n_chapters)) * DIAG_TOKENS_PER_CHAPTER
    if with_json:
        base += DIAG_JSON_TOKENS
    return min(DIAG_TOKENS_CAP, base)