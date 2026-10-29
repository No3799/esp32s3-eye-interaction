#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""第6周 · 有限多轮上下文验收。

任务卡：「讲解有限多轮任务上下文……实现澄清、重新输入、停止播报和回到空闲状态。」

要证明的是：**上一轮问过什么，能被下一句短回答接住；没问过就不能接。**

这里特意不用「查看」做用例 —— 实测模型自己就能把「查看」判成 query_latest
（见下面的 PROBE 说明）。用它做「没有上下文就该不清楚」的反例是不成立的。
真正需要上下文的，是那些**单看必被判为不清楚**的词：「看」「重新」「记录」。

用例：
  C1 没有上文时说「看」        → unclear（对照）
  C2 追问之后说「看」          → query_latest，且 trace 标 via=context
  C3 没有上文时说「重新」      → unclear（对照）
  C4 追问之后说「重新」        → request_recollect，via=context
  C5 追问之后问一个明确的新问题 → 不被上下文污染（应为 unsupported）
  C6 上下文有 TTL 与容量上限   → 直接对 TurnContext 做单元验证

用法（8000 上的服务需在跑）：
    python tools/test_week6_context.py
"""

import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))
from turn_context import TurnContext  # noqa: E402

BASE = "http://127.0.0.1:8000/api/ask"


def ask(text, sid, timeout=120):
    body = json.dumps({"text": text, "session_id": sid}).encode()
    req = urllib.request.Request(BASE, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def via_context(d):
    for t in (d.get("trace") or []):
        if t.get("step") == "intent_parse" and t.get("via") == "context":
            return True
    return False


def check(name, cond, detail=""):
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + str(detail)) if detail else ""))
    return bool(cond)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ok = True
    tag = str(int(time.time()))[-6:]

    print("=== C1/C2 「看」：没有上文 vs 有上文 ===")
    d = ask("看", "ctx-a-" + tag)
    ok &= check("C1 没上文：判为 unclear", d.get("intent") == "unclear", d.get("intent"))
    ok &= check("C1 追问里带选项",
                len((d.get("clarify") or {}).get("options") or []) == 2)

    d2 = ask("看", "ctx-a-" + tag)           # 同一会话，紧跟上一句
    ok &= check("C2 有上文：接成 query_latest", d2.get("intent") == "query_latest", d2.get("intent"))
    ok &= check("C2 trace 标明来自上下文", via_context(d2),
                json.dumps([t.get("step") for t in (d2.get("trace") or [])]))

    print()
    print("=== C3/C4 「重新」：没有上文 vs 有上文 ===")
    d = ask("重新", "ctx-b-" + tag)
    ok &= check("C3 没上文：判为 unclear", d.get("intent") == "unclear", d.get("intent"))
    d2 = ask("重新", "ctx-b-" + tag)
    ok &= check("C4 有上文：接成 request_recollect",
                d2.get("intent") == "request_recollect", d2.get("intent"))
    ok &= check("C4 trace 标明来自上下文", via_context(d2))

    print()
    print("=== C5 有上文时问一个明确的新问题，不该被污染 ===")
    d = ask("看看数据吧", "ctx-c-" + tag)     # 制造追问
    d = ask("现在设备在哪个位置？给个坐标", "ctx-c-" + tag)
    ok &= check("C5 仍是 unsupported（没被接成读记录）",
                d.get("intent") == "unsupported", d.get("intent"))

    print()
    print("=== C6 TurnContext 的 TTL 与容量上限 ===")
    c = TurnContext(ttl=0.4, max_sessions=3)
    c.set_pending("s1", {"options": [{"intent": "query_latest"}]})
    ok &= check("C6a 刚写入能读到", bool(c.get_pending("s1")))
    time.sleep(0.6)
    ok &= check("C6b 超时后读不到（过期的上下文不能接话）", c.get_pending("s1") is None)

    for i in range(6):
        c.set_pending("s%d" % i, {"options": [{"intent": "query_latest"}]})
    ok &= check("C6c 会话数不超过上限", c.stats()["sessions"] <= 3, c.stats())

    print()
    print("结果：" + ("全部通过" if ok else "有失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
