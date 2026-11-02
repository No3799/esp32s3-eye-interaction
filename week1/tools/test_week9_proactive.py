#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第 9 周验收：视觉事件触发的主动询问

任务卡要点：「事件触发、反馈优先级与主动询问条件」「补上确认、忽略与冷却时间」；
当堂验证「误判能够纠正，**用户拒绝后不反复打扰**」。

这个脚本就测"会不会烦人"这件事，四条：

1. 画面能用时**不问** —— 问了就是打扰
2. 画面不能用时**问一次** —— 问了才叫"主动"
3. 说"这次算了"之后，**同一张不再问**
4. 说"别再问"之后，**冷却期内全部静音**，并且冷却状态是落盘的（刷新不忘）

用法：
    python tools/test_week9_proactive.py
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call(path, body=None, timeout=180):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data,
                                 method="POST" if data else "GET")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with OP.open(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def find_snap(items, status=None, exclude=()):
    for it in items:
        if status and it.get("status") != status:
            continue
        if it.get("id") in exclude:
            continue
        return it
    return None


def main():
    ok = True
    print("=" * 70)
    print("第 9 周验收 · 视觉事件触发的主动询问")
    print("=" * 70)

    code, lst = call("/api/snapshot/list?limit=30")
    snaps = lst.get("snapshots") or []
    print(f"库内快照 {len(snaps)} 张")

    # 挑两张不同的：一张清晰（应该不问），一张看不清（应该问）
    clear = find_snap(snaps, status="ok")          # 优先用清晰合成图
    code, vlist = call("/api/vision/list?limit=30")
    judged = {i["snapshot_id"]: i for i in (vlist.get("items") or [])}
    usable_sid = next((sid for sid, it in judged.items()
                       if (it.get("result") or {}).get("label") == "usable"), None)
    bad_sid = next((sid for sid, it in judged.items()
                    if (it.get("result") or {}).get("label") in ("blocked", "unclear")), None)

    print("\n[1] 画面能用时，不该问")
    if usable_sid:
        code, d = call("/api/vision", {"snapshot_id": usable_sid})
        p = d.get("proactive") or {}
        print(f"    {usable_sid} 标签={(d.get('result') or {}).get('label')} "
              f"-> ask={p.get('ask')}  理由：{p.get('reason')}")
        if p.get("ask"):
            print("    ✗ 明明能用还问，这就是打扰")
            ok = False
        else:
            print("    ✓ 没有打扰")
    else:
        print("    （库里还没有 usable 的推理记录，跳过）")

    print("\n[2] 画面看不清时，问一次")
    if bad_sid:
        code, d = call("/api/vision", {"snapshot_id": bad_sid})
        p = d.get("proactive") or {}
        print(f"    {bad_sid} 标签={(d.get('result') or {}).get('label')} "
              f"-> ask={p.get('ask')}  理由：{p.get('reason')}")
        if not p.get("ask"):
            print("    ✗ 该问却没问")
            ok = False
    else:
        print("    （库里还没有 blocked/unclear 的推理记录，跳过）")

    print("\n[3] 说「这次算了」之后，同一张不再问")
    if bad_sid:
        call("/api/proactive/respond", {"action": "ignore", "snapshot_id": bad_sid,
                                        "reason": "验收：忽略一次"})
        code, d = call("/api/vision", {"snapshot_id": bad_sid})
        p = d.get("proactive") or {}
        print(f"    再次推理同一张 -> ask={p.get('ask')}  理由：{p.get('reason')}")
        if p.get("ask"):
            print("    ✗ 忽略过还在问")
            ok = False
        else:
            print("    ✓ 记住了你的选择")

    print("\n[4] 说「别再问」之后，冷却期内全部静音")
    code, d = call("/api/proactive/respond", {"action": "mute",
                                              "reason": "验收：别再问"})
    st = d.get("state") or {}
    print(f"    状态: muted={st.get('muted')}  剩余 {st.get('mute_remain_s')} 秒"
          f"（冷却 {st.get('cooldown_s')} 秒）")
    if not st.get("muted"):
        print("    ✗ 没有进入冷却")
        ok = False
    # 冷却期内换一张新的不可用图，也不该问
    other = next((s["id"] for s in snaps
                  if s.get("id") not in (bad_sid, usable_sid) and s.get("width")), None)
    if other:
        code, d2 = call("/api/vision", {"snapshot_id": other})
        p2 = d2.get("proactive") or {}
        print(f"    冷却期内推理另一张 {other} -> ask={p2.get('ask')}  理由：{p2.get('reason')}")
        if p2.get("ask"):
            print("    ✗ 冷却期还在问")
            ok = False
        else:
            print("    ✓ 静音生效")

    # 落盘检查：状态来自文件，不是内存
    code, st2 = call("/api/proactive/state")
    print(f"    再次读取状态（落盘）: muted={st2.get('muted')} "
          f"剩余{st2.get('mute_remain_s')}秒  最近事件 {len(st2.get('recent') or [])} 条")

    print("\n[5] 反例：非法动作要拒绝")
    code, d = call("/api/proactive/respond", {"action": "whatever"})
    print(f"    action=whatever -> HTTP {code} {'✓ 被拒' if code == 400 else '✗ 竟然通过'}")
    if code != 400:
        ok = False

    print("\n" + "=" * 70)
    print(">>> " + ("全部通过" if ok else "有未通过项"))
    print("=" * 70)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
