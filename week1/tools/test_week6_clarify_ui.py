#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""第6周 · 澄清选项的界面验收：按钮真的能点，点完真的接上上下文。

为什么单独测界面：接口层已经证明"有上文时「看」能接成 query_latest"，
但用户在页面上看到的是**按钮**。如果按钮没渲染、或者点了没反应，
接口再对也没用 —— 这正是"后端全绿、前端不能用"的典型。

检查项：
  U1 说一句含糊的 → 出现两个选项按钮，且文案是人话
  U2 用真鼠标点第一个 → 对话里出现「查看上次数据」这条用户消息
  U3 回答来自上下文（不是又被问一次）→ 出现"已读取"或"未通过"这类真实结果，
     并且**没有**再次出现"没能判断"

用法（8000 上的服务需在跑）：
    python tools/test_week6_clarify_ui.py
"""

import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import browser_test as bt  # noqa: E402
import browser_test_nl as btnl  # noqa: E402


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    browser = bt.find_browser()
    profile = tempfile.mkdtemp(prefix="w6clar_")
    port = 9495
    args = [browser, "--headless=new", f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}", "--no-first-run", "--disable-gpu",
            "--hide-scrollbars", "--window-size=1280,1000",
            "--autoplay-policy=no-user-gesture-required",
            "http://127.0.0.1:8000/#nl"]
    p = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + str(detail)) if detail else ""))
        ok = ok and bool(cond)

    def js(ws, expr, timeout=20.0):
        return bt.js(ws, expr, timeout=timeout)

    STATE = "document.getElementById('nlState').innerText"
    BTN_N = "document.querySelectorAll('#nlLog .nlclarify button').length"
    LAST_BOT = ("(()=>{const n=document.querySelectorAll('#nlLog .nlmsg.bot .body');"
                "return n.length? n[n.length-1].innerText : '';})()")

    def wait_done(ws, timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            if (js(ws, STATE) or "").strip() in ("完成", "已拒绝"):
                return True
            time.sleep(0.3)
        return False

    try:
        bt.wait_devtools(port)
        t = None
        for _ in range(60):
            t = bt.find_page_target(port, "127.0.0.1")
            if t:
                break
            time.sleep(0.3)
        if not t:
            print("  [FAIL] 页面没打开")
            return 1
        ws = bt.WS(t["webSocketDebuggerUrl"])
        ws.call("Page.enable")
        ws.call("Runtime.enable")
        time.sleep(4)

        # U1 说一句含糊的
        btnl.click_real(ws, "#nlChips button[data-q='看看数据吧']")
        wait_done(ws)
        n_btn = js(ws, BTN_N)
        check("U1 出现追问选项按钮", n_btn == 2, "按钮数=%s" % n_btn)
        labels = js(ws, "Array.from(document.querySelectorAll('#nlLog .nlclarify button'))"
                        ".map(b=>b.innerText).join(' / ')")
        check("U1 按钮文案是人话", "查看" in (labels or "") and "采集" in (labels or ""), labels)

        # U2 真鼠标点第一个选项
        r = btnl.click_real(ws, "#nlLog .nlclarify button")
        check("U2 选项按钮点得到", r.get("clicked"), str(r))
        time.sleep(0.8)
        asked = js(ws, "Array.from(document.querySelectorAll('#nlLog .nlmsg.me .body'))"
                       ".map(e=>e.innerText).join(' | ')")
        check("U2 点完自动发出了那句回答", "查看上次数据" in (asked or ""), asked)

        # U3 结果来自上下文，而不是又被问一次
        wait_done(ws, 60)
        last = js(ws, LAST_BOT) or ""
        check("U3 没有再重复追问", "没能判断" not in last, last[:50])
        check("U3 拿到了真实结果（读到记录 或 设备不在线）",
              ("已读取" in last) or ("范围校验" in last) or ("没有" in last), last[:50])
        check("U1 的选项按钮点完已移除", js(ws, BTN_N) == 0, js(ws, BTN_N))

        check("页面无 JS 报错", js(ws, "window.__errs ? window.__errs.length : 0") in (0, None))

        print()
        print("结果：" + ("全部通过" if ok else "有失败项"))
        return 0 if ok else 1
    finally:
        p.terminate()


if __name__ == "__main__":
    sys.exit(main())
