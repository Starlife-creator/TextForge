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
STATS = {"diagnose": 0, "blueprint": 0, "refactor": 0, "stitch": 0, "total": 0}
# 故障注入
FAIL = {"phase": None, "times": 0, "status": 500, "body": "injected"}


def classify(user: str) -> str:
    if "章节之间用" in user or "===CHAPTER_BREAK===" in user:
        return "refactor"
    if "优化建议书" in user or "全流程优化建议" in user:
        return "blueprint"
    if "缝合" in user or "过渡" in user:
        return "stitch"
    if "诊断" in user or "问题" in user:
        return "diagnose"
    return "refactor"


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


def make_text(user: str, phase: str) -> str:
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
    if phase == "stitch":
        return '===STITCH===\n{"prev_tail":"","next_head":"","patch":""}\n'
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
        return f"\n\n{BREAK}\n\n".join(segs)
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
        if "/models" in self.path:
            self._json(200, {"object": "list",
                             "data": [{"id": "mock-model", "object": "model",
                                       "owned_by": "mock"}]})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"error": "bad json"})
        user = "".join(m.get("content", "") for m in req.get("messages", []))
        phase = classify(user)
        STATS[phase] = STATS.get(phase, 0) + 1
        STATS["total"] += 1
        if FAIL["phase"] == phase and FAIL["times"] > 0:
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
        if body.get("reset"):
            for k in STATS:
                STATS[k] = 0
        if "fail" in body:
            FAIL.update(body["fail"])
        self._json(200, {"stats": STATS, "fail": FAIL})


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()