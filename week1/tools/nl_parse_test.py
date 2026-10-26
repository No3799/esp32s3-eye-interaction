#!/usr/bin/env python3
"""第4周专项 —— 解析层对「不合规模型输出」的容错测试。

**这个脚本不需要真板子，也不需要真模型。**

它解决的是任务卡备用路径里那句话：
    「服务异常时使用结构化样例定位解析问题」
—— 要能区分「是我的解析代码错了」还是「模型输出错了」。

做法：起一个**假的语言服务**（本地 HTTP），按顺序返回各种不合规的 body，
      再用真的 `NLAgent.handle()` 去处理。
      为了不误动硬件，`commands` 注入成 FakeCommands：
        它会记录 `issue()` 收到的参数，但**不会**真的下发指令。

覆盖的六种形态（对应验收记录 6.0 的表）：
  1. 合法 JSON 外面包了 ```json 围栏
  2. JSON 前后带解释文字（"好的，结果是 {...}"）
  3. 字段名错（action 而不是 intent）
  4. 意图值越界（read_data，不在白名单）
  5. 参数类型错（samples="十个"）
  6. 参数越界（samples=500）
  7. 服务返回非 JSON 垃圾 / HTTP 401

每个用例都断言两件事：
  · 不崩溃（不抛未捕获异常）
  · 不做错事（意图被拒时不调 issue；参数被限幅时如实说明）

用法:
    python tools/nl_parse_test.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "server"))

import nl_agent  # noqa: E402

DB_PATH = os.path.join(ROOT, "data", "readings.db")


# ---------------------------------------------------------------------------
# 假的语言服务：按顺序吐预设内容
# ---------------------------------------------------------------------------
class FakeLLM:
    """一个假 Ollama。每次 /api/chat 请求，吐 queue 里的下一个 body。"""

    def __init__(self):
        self.queue: list[tuple[int, str]] = []   # (http_status, content)
        self.calls = 0
        self._srv = None
        self._thread = None
        self.port = 0

    def push(self, content: str, status: int = 200):
        self.queue.append((status, content))

    def start(self):
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                outer.calls += 1
                if outer.queue:
                    status, content = outer.queue.pop(0)
                else:
                    status, content = 200, '{"intent":"unclear"}'
                if status != 200:
                    body = json.dumps({"error": "fake", "code": status}).encode()
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                # 模拟 ollama 的返回结构
                payload = json.dumps({"message": {"role": "assistant",
                                                  "content": content}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self._srv.server_address[1]
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        if self._srv:
            self._srv.shutdown()


# ---------------------------------------------------------------------------
# 假的指令存储：记录被要求下发什么，但不真的下发
# ---------------------------------------------------------------------------
class FakeCommands:
    def __init__(self):
        self.issued: list[dict] = []

    def active_for(self, device_id, ctype=None):
        return None      # 永远没有在途指令

    def issue(self, device_id, ctype, params):
        self.issued.append({"device_id": device_id, "type": ctype, "params": params})
        return {"id": "CMD-FAKE-0001", "status": "issued",
                "params": json.dumps(params), "created_ms": int(time.time() * 1000)}

    def get(self, cid):
        # 一律回报「失败」，避免测试里出现"成功"的假证据
        return {"id": cid, "status": "failed", "note": "（测试用假回执，不是真设备）",
                "created_at": None, "done_at": None}


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------
CASES = [
    # (名字, 假模型返回的 content, 期望)
    ("围栏包裹的合法 JSON",
     '```json\n{"intent":"query_latest","device_id":"","samples":null,'
     '"interval_ms":null,"reason":"测试"}\n```',
     {"intent": "query_latest", "tools_executed": 0}),

    ("JSON 前后带解释文字",
     '好的，结果是 {"intent":"query_latest","device_id":"","samples":null,'
     '"interval_ms":null,"reason":"测试"} 希望有帮助',
     {"intent": "query_latest", "tools_executed": 0}),

    ("字段名错（action 而非 intent）",
     '{"action":"request_recollect","device_id":"","samples":10}',
     {"intent": "unclear", "tools_executed": 0}),

    ("意图值越界（read_data）",
     '{"intent":"read_data","device_id":"","samples":null,"interval_ms":null}',
     {"intent": "unclear", "tools_executed": 0}),

    ("参数类型错（samples=十个）",
     '{"intent":"request_recollect","device_id":"","samples":"十个",'
     '"interval_ms":null,"reason":"测试"}',
     {"intent": "request_recollect", "tools_executed": 1,
      "expect_samples": 10, "expect_mention": "无法解析"}),

    ("参数越界（samples=500）",
     '{"intent":"request_recollect","device_id":"","samples":500,'
     '"interval_ms":5000,"reason":"测试"}',
     {"intent": "request_recollect", "tools_executed": 1,
      "expect_samples": 100, "expect_interval": 1000, "expect_mention": "限幅"}),

    ("服务返回非 JSON 垃圾",
     'INTERNAL ERROR: something exploded',
     {"intent": None, "tools_executed": 0, "expect_error": "llm_unavailable"}),

    # 认证失败：换校内服务后最容易遇到的错误。
    # 必须归到 llm_unavailable（而不是崩溃、也不是"猜个意图去执行"）。
    ("服务返回 HTTP 401（密钥错/过期）",
     '',
     {"intent": None, "tools_executed": 0, "expect_error": "llm_unavailable",
      "http_status": 401}),
]


def main():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    lock = threading.Lock()

    # 只读地找一台真实设备名，让"设备解析"这一步能通过
    row = conn.execute("SELECT device_id FROM readings ORDER BY id DESC LIMIT 1").fetchone()
    real_dev = row["device_id"] if row else "test-dev"

    fake_llm_srv = FakeLLM().start()
    results = []

    for name, content, expect in CASES:
        fake_llm_srv.queue.clear()
        fake_llm_srv.push(content, status=expect.get("http_status", 200))

        llm = nl_agent.LLMClient(base_url=f"http://127.0.0.1:{fake_llm_srv.port}",
                                 model="fake", timeout=10, protocol="ollama")
        cmds = FakeCommands()
        agent = nl_agent.NLAgent(llm, conn, lock, cmds,
                                 device_state={}, stale_seconds=5.0,
                                 collect_wait=1.0, reply_style="template")

        # 用真实设备名，避免"设备解析"这一步把它挡下来，
        # 那样就测不到参数校验了
        res = agent.handle("重新采集一次" if real_dev else "测试")

        reply = res.get("reply") or ""
        ok = True
        notes = []

        # 1) 不崩溃
        if not isinstance(res, dict):
            ok = False
            notes.append("返回不是 dict")

        # 2) 意图符合期望
        if "intent" in expect and expect["intent"] is not None:
            if res.get("intent") != expect["intent"]:
                ok = False
                notes.append(f"意图 {res.get('intent')} ≠ 期望 {expect['intent']}")

        # 3) 工具执行次数（不该执行的绝不能执行）
        if "tools_executed" in expect:
            n = len(cmds.issued)
            if n != expect["tools_executed"]:
                ok = False
                notes.append(f"下发次数 {n} ≠ 期望 {expect['tools_executed']}")

        # 4) 限幅结果
        if "expect_samples" in expect and cmds.issued:
            got = cmds.issued[0]["params"].get("samples")
            if got != expect["expect_samples"]:
                ok = False
                notes.append(f"samples {got} ≠ 期望 {expect['expect_samples']}")
        if "expect_interval" in expect and cmds.issued:
            got = cmds.issued[0]["params"].get("interval_ms")
            if got != expect["expect_interval"]:
                ok = False
                notes.append(f"interval {got} ≠ 期望 {expect['expect_interval']}")

        # 5) 回答里要说明"为什么改了我的参数"
        if "expect_mention" in expect:
            if expect["expect_mention"] not in reply:
                ok = False
                notes.append(f"回答里没提到「{expect['expect_mention']}」")

        # 6) 服务错误要归类为 llm_unavailable
        if "expect_error" in expect:
            if res.get("error") != expect["expect_error"]:
                ok = False
                notes.append(f"error={res.get('error')} ≠ {expect['expect_error']}")

        # 7) 恒定检查：没有完成证据时，不许出现成功声明
        claims = [w for w in nl_agent.SUCCESS_CLAIM_PATTERNS if w in reply]
        if claims:
            ok = False
            notes.append(f"出现成功声明：{claims}")

        results.append((name, ok, notes, res.get("intent"), len(cmds.issued), reply))

    fake_llm_srv.stop()

    # ---- 输出 ----
    print("=" * 78)
    print("# 解析层容错测试（假模型 + 假指令存储，不动真板子）")
    print("=" * 78)
    print(f"使用设备名: {real_dev}\n")

    bad = 0
    for name, ok, notes, intent, n_issued, reply in results:
        mark = "✓" if ok else "✗"
        print(f"{mark} {name}")
        print(f"    意图={intent}  下发次数={n_issued}")
        head = reply.split("\n")[0][:70]
        print(f"    回答首行: {head}")
        if notes:
            for nt in notes:
                print(f"    ✗ {nt}")
            bad += 1
        print()

    print("=" * 78)
    print(f"共 {len(results)} 个用例，未通过 {bad} 个")
    if bad == 0:
        print("解析层对六种不合规输出都能给出明确结果：要么拒绝执行，要么限幅并说明，")
        print("既没有崩溃，也没有把错误输出当成正常指令执行。")
    print("=" * 78)
    conn.close()
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
