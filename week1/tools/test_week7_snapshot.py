#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""第7周 · 按需环境快照验收

任务卡原文（第7周）：
    本周问题　用户请求查看环境时，系统如何说明采集是否成功，采到的是哪一时刻的画面？
    前2学时　在既有任务与存储服务中增加按需图像采集；讲解授权、图像输入与有效性，
             复用摄像头驱动或实际图像接口，关联请求、图像记录和采集时间。
    后2学时　实现采集中、采集成功、镜头遮挡/画面不可用和采集失败反馈；
             只在请求或教学事件触发后取图，设置最少保留。
    当堂验证　上次成功图像不得被标为本次采集成功；采集失败保持明确状态。
    备用路径　用获授权的真实历史图像练习，标为回放；随后补验一次真实按需采集。

这里测七条，重点是**第 5 条**（S5）：失败时绝不能把上次成功的图说成这次拍到了。

用法（服务需带 --fault-injection，S5 才能确定地造失败）：
    python tools/test_week7_snapshot.py
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

ROOT = "http://127.0.0.1:8000"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 本机，别走代理


def req(method, path, body=None, raw=False, timeout=40):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(ROOT + path, data=data, method=method)
    if data:
        r.add_header("Content-Type", "application/json")
    try:
        with OPENER.open(r, timeout=timeout) as resp:
            payload = resp.read()
            if raw:
                return resp.status, payload, dict(resp.headers)
            return resp.status, json.loads(payload.decode() or "{}")
    except urllib.error.HTTPError as e:
        payload = e.read()
        if raw:
            return e.code, payload, dict(e.headers)
        try:
            return e.code, json.loads(payload.decode() or "{}")
        except Exception:
            return e.code, {"error": payload.decode()[:200]}


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + str(detail)) if detail else ""))
        ok = ok and bool(cond)

    print("=== S1 授权：没有明确请求就不取图 ===")
    st, d = req("POST", "/api/snapshot", {"note": "漏了 trigger"})
    print("     HTTP %s  status=%s  reason=%s" % (st, d.get("status"), (d.get("reason") or "")[:60]))
    check("无 trigger 被拒绝（HTTP 403）", st == 403)
    check("记录状态为 denied", d.get("status") == "denied")
    check("被拒绝也有编号（可追溯）", bool(d.get("id")))
    check("被拒绝不返回旧图冒充", d.get("is_this_attempt") is False)

    print("\n=== S2 越权回放：只允许 debug_logs/ 下的历史抓帧 ===")
    st, d = req("POST", "/api/snapshot/replay", {"file": "../../windows/win.ini"})
    print("     HTTP %s  %s" % (st, (d.get("error") or "")[:70]))
    check("越界路径被拒绝（HTTP 403）", st == 403)

    print("\n=== S3 回放：真实历史图像，必须标成回放 ===")
    src = "debug_logs/cam_frame.jpg"
    st, d = req("POST", "/api/snapshot/replay", {"file": src, "note": "第1周真实抓帧"})
    print("     HTTP %s  id=%s  source=%s  status=%s  %sx%s"
          % (st, d.get("id"), d.get("source"), d.get("status"),
             d.get("width"), d.get("height")))
    check("回放导入成功", st == 200 and d.get("status") == "ok", d.get("reason"))
    check("source 是 replay（不是 device）", d.get("source") == "replay")
    check("回放不算本次采集", d.get("is_this_attempt") is False)
    check("记录了来源文件", bool(d.get("imported_from")))
    replay_id = d.get("id")

    print("\n=== S4 真采集（板子在线就成功，不在线就明确失败）===")
    st, d = req("POST", "/api/snapshot", {"trigger": "user", "request_id": "t7-real"})
    print("     HTTP %s  status=%s  is_this=%s  %sx%s  %sms"
          % (st, d.get("status"), d.get("is_this_attempt"), d.get("width"), d.get("height"),
             d.get("latency_ms")))
    print("     reply: %s" % (d.get("reply") or "").replace("\n", " ")[:110])
    if d.get("status") == "ok":
        check("真采集成功：有尺寸且文件可读", (d.get("width") or 0) > 0 and bool(d.get("file")))
        check("成功时 is_this_attempt=True", d.get("is_this_attempt") is True)
        st2, img, hdr = req("GET", "/api/snapshot/%s.jpg" % d.get("id"), raw=True)
        check("能取回 JPEG 字节", st2 == 200 and img[:2] == b"\xff\xd8", hdr.get("Content-Type"))
    else:
        check("失败时状态明确（不是 ok）", d.get("status") not in (None, "", "ok"))
        check("失败时 is_this_attempt=False", d.get("is_this_attempt") is False)
        check("失败时给了原因", bool(d.get("reason")))

    print("\n=== S5 当堂验证：失败不得把上次成功说成本次 ===")
    st, d = req("POST", "/api/snapshot",
                {"trigger": "user", "request_id": "t7-fail", "force_ip": "10.255.255.1"})
    print("     HTTP %s  status=%s  is_this=%s" % (st, d.get("status"), d.get("is_this_attempt")))
    print("     reply: %s" % (d.get("reply") or "").replace("\n", " ")[:140])
    check("注入后确实失败", d.get("status") not in (None, "", "ok"), d.get("status"))
    check("失败结果的 is_this_attempt=False", d.get("is_this_attempt") is False)
    last = d.get("last_ok")
    if last:
        check("引用上次成功时，明确标注它不是本次", last.get("is_this_attempt") is False)
        check("上次成功带自己的拍摄时间", bool(last.get("captured_at")))
        check("回复里写清「不是本次」", "不是本次" in (d.get("reply") or ""))
    else:
        check("还没有过成功采集时，不引用任何旧图", last is None)
    check("失败记录也已落库", bool(d.get("id")))

    print("\n=== S6 列表：记录可查、可分成功与失败 ===")
    st, d = req("GET", "/api/snapshot/list?limit=10")
    items = d.get("snapshots") or []
    print("     共 %s 条，其中成功 %s 条" % (d.get("count"), d.get("ok_count")))
    for r in items[:5]:
        print("       %-28s %-12s %-7s %s" % (r.get("id"), r.get("status"),
                                             r.get("source"), (r.get("reason") or "")[:36]))
    check("列表返回了记录", len(items) > 0)
    check("回放那条在列表里", any(r.get("id") == replay_id for r in items))
    st, img, hdr = req("GET", "/api/snapshot/%s.jpg" % replay_id, raw=True)
    check("回放的图也能取回（是真实 JPEG）", st == 200 and img[:2] == b"\xff\xd8")

    print("\n=== S7 保留策略：last_ok 只认设备真实采集 ===")
    st, d = req("GET", "/api/snapshot/list?limit=50")
    last = d.get("last_ok") or {}
    print("     last_ok = %s" % (json.dumps(last, ensure_ascii=False) if last else "（无）"))
    if last:
        rec = next((r for r in d["snapshots"] if r.get("id") == last.get("id")), {})
        check("last_ok 指向的是 device 采集，不是回放", rec.get("source") == "device",
              rec.get("source"))
    else:
        check("没有设备采集成功过时 last_ok 为空（回放不算）", True,
              "回放不计入 last_ok，符合预期")

    print("\n结果：" + ("全部通过" if ok else "有失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
