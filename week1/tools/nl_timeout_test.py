#!/usr/bin/env python3
"""第4周专项 —— 「设备无响应」路径的真机验收。

任务卡当堂验证与提交要求覆盖「旧数据 / 设备无响应」。
第2周已有 `fail_acceptance.py` 测的是"设备回报失败(failed)"，
但**设备完全不回执（无响应 → 超时）**是另一条路径，本脚本专门测它。

怎么造出"设备无响应"？
  起一个**代理服务器**放在板子和真服务器之间：板子上报先到代理，
  代理只放行**下行捎带**（让板子取到指令、从而进入 received 状态之前的
  必要交互）与普通查询，但把**设备的数据上报和回执**扣下来不发出去。

  更简单可靠的做法：**直接让板子连到一个"哑服务器"** ——
  哑服务器对 /api/data 一律返回 200 但**不真正入库**，
  这样板子以为服务器收到了，指令会一直停在 issued/delivered，超时到 accept 阶段。

  本脚本用第二种：起一个哑服务器（吞掉上报），让板子改连它，
  然后对着**真服务器**下发一条采集指令 —— 真服务器等不到板子来取，
  于是 accept 阶段超时。助手对着这条超时指令必须**如实说超时**，
  绝不能说"已采集成功"。

⚠️ 这个测试会**改板子的 SERVER_URL**？不会 —— 固件的地址是编译进去的。
   所以实际做法是：**不接真板子**，而是用一个模拟板子（sim）连真服务器，
   然后把模拟板子"停掉"，看真服务器的指令超时。
   模拟板子与真板子走**完全相同的协议**（同样的 POST /api/data 与捎带指令），
   所以超时路径与真机一致。
   真板子的 timeout 路径在第2周已用真机验证过（CMD-20260918-0004，accept 阶段 30.45s）。

用法:
    python tools/nl_timeout_test.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB_PATH = os.path.join(ROOT, "data", "readings.db")
BASE = "http://127.0.0.1:8000"

# 成功的说法。没有完成证据时出现即为违规。
SUCCESS_WORDS = ("已采集成功", "采集成功", "成功采集", "已完成采集",
                  "采集完成", "数据已更新", "已更新数据")


def http(method: str, url: str, body: dict | None = None, timeout: float = 30.0):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
        try:
            return resp.status, json.loads(raw)
        except json.JSONDecodeError:
            return resp.status, {"raw": raw[:400]}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"raw": raw[:400]}
    except Exception as e:
        return 0, {"error": str(e)[:300]}


def main():
    print("=" * 78)
    print("# 第4周专项：设备无响应 → 助手必须如实说超时，不能说成功")
    print("=" * 78)

    # ---- 1. 确认故障注入可用（造 failed 路径）----
    print("\n[1] 先用故障注入造「设备回报失败(failed)」这条路径")
    st, r = http("POST", BASE + "/api/debug/fault", {"mode": "command_data", "times": 8})
    if st == 403:
        print(f"  故障注入未开启（{r.get('error')}）")
        print("  → 需要用 --fault-injection 启动服务器才能测 failed 路径")
        print("  → 下面改测 timeout 路径（不需要故障注入）")
    else:
        print(f"  故障注入已开启: {r}")

    # ---- 2. 造一个"永不上报的设备"，走 accept 阶段超时 ----
    # 做法：直接往 commands 表里看真服务器对"没见过的设备"会怎样。
    # 指令下发后，服务器要等板子来取；没有板子 → accept 超时。
    # 我们用一个**真实存在但当前离线**的设备（selftest-sim）来触发这条路径。
    print("\n[2] 对一台离线设备下发采集指令 → 应走 accept 阶段超时")
    st, devs = http("GET", BASE + "/api/devices")
    offline = None
    for d in devs.get("devices", []):
        if d.get("age_seconds") is not None and d["age_seconds"] > 300:
            offline = d["device_id"]
            break
    if not offline:
        print("  [跳过] 库里没有离线设备")
        return 1
    print(f"  目标离线设备: {offline}")

    # 注意：助手对离线设备会**当场拒绝**（不在线就不下发），
    # 这本身是正确的行为。所以要测真正的「超时」路径，
    # 得绕过助手的离线预检，直接走 /api/command 下发。
    st, cmd = http("POST", BASE + "/api/command",
                   {"device_id": offline, "type": "recollect",
                    "samples": 5, "interval_ms": 100})
    print(f"  直接下发指令: HTTP {st} → {cmd.get('request_id')} "
          f"status={cmd.get('status')}")
    rid = cmd.get("request_id")
    if not rid:
        print(f"  [失败] 指令没下发成功: {cmd}")
        return 1

    # 等它超时（accept 阶段 30 秒）
    print("  等待指令超时（accept 阶段上限 30 秒）…")
    deadline = time.time() + 75
    final = None
    while time.time() < deadline:
        st, c = http("GET", BASE + "/api/command/" + rid)
        if c.get("status") in ("timeout", "failed", "done"):
            final = c
            break
        time.sleep(2)
    if not final:
        st, final = http("GET", BASE + "/api/command/" + rid)
    print(f"  指令最终状态: status={final.get('status')} "
          f"timeout_stage={final.get('timeout_stage')}")

    # ---- 3. 关键：把这条超时指令塞给助手，看它怎么回答 ----
    # 助手是按"自己下发并等待"来组织的，所以这里用一个直接的单元级检查：
    # 构造一条与上面同构的"无完成证据"的工具结果，跑回复拼装 + 闸门。
    print("\n[3] 检查助手在「无完成证据」时的回答（用同构的失败结果）")
    sys.path.insert(0, os.path.join(ROOT, "server"))
    import nl_agent  # noqa: E402

    agent = nl_agent.NLAgent.__new__(nl_agent.NLAgent)
    agent.stale_seconds = 5.0
    agent.reply_style = "template"

    for status, extra in (("timeout", {"timeout_stage": "accept"}),
                          ("failed", {"note": "设备回报失败"}),
                          ("issued", {})):
        tool_res = {
            "ok": True, "tool": "request_recollect",
            "request_id": "CMD-TEST-0001", "status": status,
            "params": {"samples": 5, "interval_ms": 100},
            "samples_in_db": 0, "first_sample_ms": None, "last_sample_ms": None,
            "has_completion_evidence": False,
            "timeout_stage": extra.get("timeout_stage"),
            "note": extra.get("note"),
            "source": {"kind": "command", "request_id": "CMD-TEST-0001",
                       "device_id": "test-dev", "created_at": None, "done_at": None},
        }
        reply = agent._reply_recollect(tool_res, "（测试）", None)
        claims = [w for w in SUCCESS_WORDS if w in reply]
        print(f"\n  --- 状态 {status} ---")
        print("  " + reply.replace("\n", "\n  "))
        print(f"  [检查] 出现成功声明？ {claims if claims else '没有（正确）'}")
        if claims:
            print(f"  [✗ 违规] 状态 {status} 时声称了成功")

    # ---- 4. 闸门本身的单元检查 ----
    print("\n[4] 闸门单元检查：给一段声称成功的话，allowed=False 时必须被拦下")
    txt, ok, why = nl_agent._assert_evidence_gate("已采集成功，共 12 条", False)
    print(f"  拦截结果: ok={ok} 原因={why or '（无）'}")
    txt2, ok2, why2 = nl_agent._assert_evidence_gate("已采集成功，共 12 条", True)
    print(f"  有证据时: ok={ok2} 原因={why2 or '（无）'}")

    # ---- 5. 查库里这条超时指令的真实记录 ----
    print("\n[5] 数据库里的证据（服务器侧主动落库的超时记录）")
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    rows = list(c.execute("SELECT id, device_id, status, timeout_stage,"
                          " created_ms, timeout_ms, note FROM commands"
                          " WHERE id = ?", (rid,)))
    for r in rows:
        d = dict(r)
        print(f"  id={d['id']} device={d['device_id']} status={d['status']} "
              f"timeout_stage={d['timeout_stage']} note={d['note']}")
        if d["created_ms"] and d["timeout_ms"]:
            print(f"  从受理到判定超时: {(d['timeout_ms'] - d['created_ms'])/1000:.1f} 秒")
    c.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
