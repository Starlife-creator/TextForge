"""最小 OpenAI 兼容 mock LLM（流式），用于端到端跑通流水线而不依赖 LM Studio。

- /v1/models        返回模型列表
- /v1/chat/completions  返回 SSE 分帧的 data: {...} + [DONE]
- 按提示词里的 ===CHAPTER_BREAK=== 契约产出 N 段，否则产出普通文本
- 蓝图/诊断/缝合分别产出符合各自契约的可解析内容

用法：python mock_llm.py [port]
"""
import json, re, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8299
BREAK = "===CHAPTER_BREAK==="

# 统计各阶段调用次数，便于测试断言
STATS = {"diagnose": 0, "blueprint": 0, "blueprint_map": 0,
         "refactor": 0, "stitch": 0, "total": 0}
# 故障注入
FAIL = {"phase": None, "times": 0, "status": 500, "body": "injected",
        "extra_breaks": 0, "skip": 0, "arm_after": 0}
# 上下文上限模拟：max_chapters>0 时，请求里章数超过它即返回 400（LM Studio 文案）。
CTX_LIMIT = {"max_chapters": 0}
# 内容级质检故障注入：下 N 次重构输出插入 "TODO" 占位，触发自动重写。
BAD_CONTENT = {"times": 0}
# 缝合内容注入：下 N 次缝合返回「等长但整段换写法」的补丁，
# 用来验证最小改动闸门确实会拒绝长度合规的整段重写（而不是只挡住截断）。
STITCH_BULK = {"times": 0}
# 服务端上报的真实上下文窗口（供 /props、/api/v0/models、/models、/api/show 探测）。
CONTEXT_REPORT = {"n_ctx": 4096}
LOG = []
# 纠错提示的稳定标记：真实模型与 mock 都以此判断「模型已知晓自己上次错在哪」
CORRECTED_MARK = "你上一次的输出"


def classify(user: str) -> str:
    """判断这次请求属于哪个阶段。

    判据用各阶段**唯一的机器标记**，并从最具体排到最泛：
    缝合提示词里嵌着蓝图（含「优化建议书」），重构提示词里也嵌着蓝图 ——
    若按「先蓝图后缝合」的老顺序，缝合/ 重构调用会被误判成蓝图，
    返回的文本缺少该阶段的标记，下游直接报「缝合输出缺少标记」。
    这类 bug 只在真正走到缝合（多批次）时才暴露，单批次测试永远看不到。
    """
    if "===PREV_END===" in user or "===NEXT_START===" in user:
        return "stitch"                      # 缝合提示词独有标记
    if "【蓝图分卷摘要任务】" in user:
        return "blueprint_map"               # phase2 分层压缩独有标记
    if "拆书助手" in user or "候选行：" in user:
        return "split"                       # AI 拆章提示词独有标记
    if "===CHAPTER_BREAK===" in user or "章节之间用" in user:
        return "refactor"                    # 重构提示词独有标记
    if "架构师" in user or "优化建议书" in user:
        return "blueprint"
    return "diagnose"


# AI 拆章里「算章节起点」的标题样式（模拟一个判断力正常的模型）
_SPLIT_HEAD = re.compile(
    r'^(?:第[零一二三四五六七八九十百千万0-9两]+[章回节卷篇集折]'
    r'|楔子|序章|序言|自序|前言|引子|引言|序幕|尾声|终章|尾章|终篇|番外|外传|间章|后记|跋'
    r'|Chapter|Part|Book)')


def make_split_result(user: str) -> str:
    """解析候选行（`行号|内容`），回传「像章节起点」的行号 —— 模拟一个有判断力的模型。

    模拟的判断力：
      - 命中编号/特殊名（第X章、楔子、尾声…）→ 是起点
      - 短标题行（≤12 字、不以句末标点结尾）→ 是起点
      - 紧跟在另一起点下一行的候选 → 是它的副标题，**不算**新起点
    这条链路只在**候选行**里选，真实模型再差也不会碰到正文。
    """
    pairs = [(int(m.group(1)), m.group(2).strip())
             for m in re.finditer(r'^(\d+)\|(.*)$', user, re.M)]
    accepted = []
    for ln, s in pairs:
        is_head = bool(_SPLIT_HEAD.match(s)) or (
            1 <= len(s) <= 12 and not s.endswith(("。", "！", "？", "，", "的")))
        if is_head and (not accepted or ln > accepted[-1] + 1):
            accepted.append(ln)
    return json.dumps({"starts": accepted}, ensure_ascii=False)


def chapter_count(user: str) -> int:
    """从提示词尾部推断本批应产出几段。

    按**去重后的章名顺序**计数：测试语料的每段正文都以「第X章 第Y段」开头，
    直接数行会把每章的每个段落都算进去（6 章 × 3 段 = 18）。
    """
    body = user.split("【待处理正文】")[-1] if "【待处理正文】" in user else user
    names, seen = [], set()
    for m in re.finditer(r"^\s*第\s*([0-9零一二三四五六七八九十百千万]+)\s*章", body, re.M):
        n = m.group(1)
        if n not in seen:
            seen.add(n)
            names.append(n)
    return max(1, len(names))


_BULK_FILLER = "（模拟整段重写）月色下的窗棂被重新描了一遍。"


def _bulk_rewrite(seg: str) -> str:
    """等长但每句都换掉的文本：长度比 0.5~3.0 放行，只有改动率闸门能抓住。

    真实模型的「顺手重写整段」就是这个形状 —— 字数差不多，句子全换了。
    """
    if not seg:
        return seg
    n = max(1, len(seg) // len(_BULK_FILLER) + 1)
    return (_BULK_FILLER * n)[:len(seg)]


def make_text(user: str, phase: str) -> str:
    if phase == "blueprint_map":
        # 必须短于输入，否则压缩不收敛、phase2 会退化成抽样。
        return "分卷脉络：本段主线推进、人物状态变化与未回收伏笔已压缩（mock）。\n"
    if phase == "blueprint":
        return ("《全流程优化建议书》\n"
                "1. 全书主线脉络：主角从迷茫走向担当，最终完成救赎。\n"
                "2. 主角人物弧光：起点怯懦，中段决断，终点从容。\n"
                "3. 核心逻辑漏洞与缝合方案：第三幕时间线断层需补过渡；配角动机不足需铺垫。\n"
                "4. 全书基调与重构方向：冷峻克制，减少解释性叙述，多用动作推进。\n"
                "（本段为蓝图，供后续重构作为系统提示词使用。）\n" * 2)
    if phase == "diagnose":
        return ("诊断结论：节奏稳健，人物动机基本成立。\n"
                "问题 1：场景转场生硬，缺乏过渡。\n"
                "问题 2：配角出场缺少动机铺垫。\n"
                "改进建议：补充心理描写与转折句。\n")
    if phase == "split":
        return make_split_result(user)
    if phase == "stitch":
        # 真实契约（core/stitch.py::parse_stitch_result）：必须用
        # ===PREV_END=== / ===NEXT_START=== 两个标记包裹改写后的两段。
        # 旧 mock 返回的是自称的 {"prev_tail":...} 格式，与真实契约不符 ——
        # 之所以一直没暴露，是因为老的分批逻辑在测试小说上只切出 1 批，
        # 批次间没有边界，缝合路径**从未被执行过**。token 预算让批次变多后才暴露。
        # 这里回显原文并各加一句过渡，既保持正文完整，又能验证补丁确实被应用。
        m = re.search(r'【前文结尾】\n(.*?)\n\n【后文开头】\n(.*)\Z', user, re.S)
        prev_seg = m.group(1) if m else ""
        next_seg = m.group(2) if m else ""
        if STITCH_BULK.get("times", 0) > 0:
            STITCH_BULK["times"] -= 1
            prev_seg = _bulk_rewrite(prev_seg)
            next_seg = _bulk_rewrite(next_seg)
        return (f"===PREV_END===\n{prev_seg}\n（缝合过渡：夜色里传来一阵脚步。）\n"
                f"===NEXT_START===\n（缝合过渡：他深吸一口气，推门而入。）\n{next_seg}\n")
    if phase == "refactor":
        n = chapter_count(user)
        segs = []
        for i in range(n):
            # 每章产出多段（真实小说如此），以覆盖 document.py 的段落/换行处理
            paras = [
                f"【重构第{i+1}段·其一】夜色像墨一样沉下来，他把手按在门框上，指节发白。",
                f"【重构第{i+1}段·其二】远处传来一声闷响，像是谁把一扇门关在了风里。\n他没有回头。",
                f"【重构第{i+1}段·其三】脚下的木地板凉得刺骨，他慢慢地数着自己的呼吸。",
            ]
            segs.append("\n\n".join(paras))
        out = f"\n\n{BREAK}\n\n".join(segs)
        # 注入只针对**多章批次**：单章重生成（/api/regen_chapter）的提示词只含一章，
        # 若也在这里注入，测试就无法区分「批次里的占位」与「重写后的干净正文」，
        # 场景十三的断言会变成随机结果。
        if BAD_CONTENT.get("times", 0) > 0 and "结构性质检" not in user and n > 1:
            BAD_CONTENT["times"] -= 1
            STATS["bad_content_injected"] = STATS.get("bad_content_injected", 0) + 1
            out = out.replace("【重构第1段·其一】", "TODO：此处待补。【重构第1段·其一】", 1)
        # 故障注入：模拟真实模型在**中间**多分了一段。
        # 用户实际遇到的报错是「需要 3 个，实际 5 个（切出 6 段）」——
        # 多余分隔符出现在批次中间（模型把原文的「第N章」标题也当成分隔点），
        # 不是末尾；末尾空段早已被容忍逻辑吃掉，放末尾测不到东西。
        if FAIL.get("extra_breaks", 0) > 0 and CORRECTED_MARK not in user:
            parts = out.split(f"\n\n{BREAK}\n\n")
            if len(parts) > 3:
                parts.insert(2, "第一章\n（模型误插入的一段）\n")
                out = f"\n\n{BREAK}\n\n".join(parts)
        return out
    return "重构完成。"


def frames(content: str):
    """OpenAI 流式分帧。按行分片，顺带验证前端能正确处理跨块的多字节字符。"""
    out = []
    step = 24
    for i in range(0, len(content), step):
        delta = content[i:i + step]
        obj = {"id": "mock", "object": "chat.completion.chunk", "model": "mock-model",
               "choices": [{"index": 0, "delta": {"content": delta}, "finish_reason": None}]}
        out.append(obj)
    out.append({"id": "mock", "object": "chat.completion.chunk", "model": "mock-model",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": len(content), "completion_tokens": len(content),
                          "total_tokens": len(content) * 2}})
    return out


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # 各厂商「真实上下文窗口」探测端点（第 3 层）。全部返回同一个上报值，
        # 便于测试断言「探测到的就是服务端真实值」。
        _ctx = CONTEXT_REPORT.get("n_ctx", 4096)
        if "/api/v0/models" in self.path:            # LM Studio 原生
            self._json(200, {"data": [{"id": "mock-model", "object": "model",
                                       "max_context_length": _ctx}]})
        elif self.path.rstrip("/").endswith("/props"):   # llama.cpp server
            self._json(200, {"default_generation_settings": {"n_ctx": _ctx}})
        elif "/models" in self.path:                 # OpenAI 兼容
            self._json(200, {"object": "list",
                             "data": [{"id": "mock-model", "object": "model",
                                       "owned_by": "mock", "max_model_len": _ctx}]})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"error": "bad json"})
        # Ollama /api/show：用 POST + {"name": ...} 返回 model_info
        if self.path.rstrip("/").endswith("/api/show"):
            _ctx = CONTEXT_REPORT.get("n_ctx", 4096)
            return self._json(200, {"model_info": {"mock.context_length": _ctx}})
        user = "".join(m.get("content", "") for m in req.get("messages", []))
        phase = classify(user)
        STATS[phase] = STATS.get(phase, 0) + 1
        STATS["total"] += 1
        if "【上一批结尾（仅用于衔接）】" in user:
            STATS["continuity_injected"] = STATS.get("continuity_injected", 0) + 1
        if "【前情摘要（仅用于保持事实一致）】" in user:
            STATS["story_memo_injected"] = STATS.get("story_memo_injected", 0) + 1
        # 缝合契约纠正提示（core/stitch.py::_stitch_retry_hook）：只有上一次缝合
        # 被拒后才会出现，用它证明「拒绝 → 带错因重试」这条链路真的走通了。
        if "你上一次的缝合输出不合格" in user:
            STATS["stitch_corrected"] = STATS.get("stitch_corrected", 0) + 1
        LOG.append({"phase": phase, "n_msgs": len(req.get("messages", [])),
                    "extra": FAIL.get("extra_breaks", 0),
                    "has_fix": CORRECTED_MARK in user})
        # 上下文超限模拟：章节数超过 max_chapters 即返回 400（LM Studio 文案）。
        # 用「章节数」而非 token 数做判据是为了确定性 —— 分数由提示词里的章名个数
        # 决定，测试不必去凑精确的 token 阈值；而报错文案仍是真实的 LM Studio 格式，
        # 所以仍然在验证「从报错里解析上限并拆批」这条链路。
        _cap = CTX_LIMIT.get("max_chapters") or 0
        if _cap and phase not in ("split", "stitch"):
            _nchap = chapter_count(user)
            if _nchap > _cap:
                _req, _avail = _nchap * 2000, _cap * 2000
                msg = (f"request ({_req} tokens) exceeds the available context size "
                       f"({_avail} tokens), try increasing it")
                STATS["ctx_overflow"] = STATS.get("ctx_overflow", 0) + 1
                return self._json(400, {"error": {"message": msg}})
        if FAIL["phase"] == phase and FAIL["times"] > 0:
            # 触发点用「第 N 次调用」在 mock 内部计数，而不是由测试在外面布防。
            # 外部布防（armed）依赖测试轮询 progress.json，而 mock 跑得极快：
            # 12 个批次约 2 秒跑完，而轮询间隔 0.5 秒 —— 等测试发现「第一批已完成」
            # 时，早就全部跑完了，失败永远不会触发（表现为随机 FAIL）。
            # 放在这里由调用序号决定，与测试的轮询节奏彻底解耦。
            # 前提：测试必须保证这一阶段**至少有 N+1 次调用**（即书足够大、
            # 会被拆成多个批次），否则同样不会触发。
            _after = FAIL.get("arm_after", 0) or 0
            _ordinal = STATS.get(phase, 0)      # 本次调用是该阶段的第几次
            if not _after or _ordinal > _after:
                if FAIL.get("skip", 0) > 0:
                    FAIL["skip"] -= 1
                else:
                    FAIL["times"] -= 1
                    return self._json(FAIL["status"], {"error": {"message": FAIL["body"]}})

        content = make_text(user, phase)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            for obj in frames(content):
                chunk = ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode()
                self.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
                self.wfile.flush()
                time.sleep(0.002)
            done = b"data: [DONE]\n\n"
            self.wfile.write(f"{len(done):X}\r\n".encode() + done + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_PUT(self):
        """控制面：查统计 / 注入故障"""
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if body.get("log_clear"):
            LOG.clear()
        if body.get("reset"):
            for k in STATS:
                STATS[k] = 0
        if "fail" in body:
            FAIL.update(body["fail"])
        if "ctx_limit" in body:
            CTX_LIMIT.update(body["ctx_limit"])
        if "bad_content" in body:
            BAD_CONTENT.update(body["bad_content"])
        if "stitch_bulk" in body:
            STITCH_BULK.update(body["stitch_bulk"])
        if "context_report" in body:
            CONTEXT_REPORT.update(body["context_report"])
        self._json(200, {"stats": STATS, "fail": FAIL, "ctx_limit": CTX_LIMIT,
                         "bad_content": BAD_CONTENT, "stitch_bulk": STITCH_BULK,
                         "context_report": CONTEXT_REPORT, "log": LOG[-12:]})


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()