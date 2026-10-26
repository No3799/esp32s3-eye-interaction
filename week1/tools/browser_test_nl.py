#!/usr/bin/env python3
"""第4周 · 自然语言助手面板的真实浏览器实测。

为什么要单独写一个：网页部分此前只做过**静态**检查（JS 语法、元素 id 一致性、
接口连通性）。那些证明不了"在真实浏览器里点一下，界面真的会动"。
更重要的是 —— 静态检查看不出"**按钮点不到**"这类问题：
用 JS 的 .click() 去点一个被隐藏的元素照样生效，于是"点得动"永远通过。

所以这里用 DevTools Protocol 的 **Input.dispatchMouseEvent**（真鼠标点击），
并且每次点击前都先确认元素**真的可见**（getBoundingClientRect 有尺寸、
offsetParent 不为空），可见性不达标就判失败。

它做的是：
  1. 启动真实 Chrome（无头），打开页面并切到「语言」分区
  2. **真鼠标点击**示例按钮，等待回答出现
  3. 检查回答里是否带来源/时间/状态（这才是"基于真实结果回复"的证据）
  4. 再点一次「重新采集」，检查界面显示的状态与新观测条数
  5. 抓 JS 报错、截屏

不依赖任何第三方库：WebSocket 客户端是手写的（标准库没有 WS）。

用法:
    python tools/browser_test_nl.py
    python tools/browser_test_nl.py --keep-open
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from browser_test import (WS, find_browser, find_page_target, http_json,  # noqa: E402
                          js, shot, wait_devtools)


def click_real(ws: WS, selector: str) -> dict:
    """用**真鼠标事件**点一个元素。

    为什么要这么麻烦：JS 的 .click() 对隐藏元素照样生效，
    于是「按钮其实点不到」这种问题会被测成通过 —— 这是个假通过。
    所以先量可见性，再用鼠标事件点真实坐标。
    """
    box = js(ws, f"""
      (function() {{
        const el = document.querySelector({json.dumps(selector)});
        if (!el) return {{ok:false, reason:'no such element'}};
        const r = el.getBoundingClientRect();
        const cs = getComputedStyle(el);
        const visible = r.width > 0 && r.height > 0 &&
                        cs.visibility !== 'hidden' && cs.display !== 'none' &&
                        el.offsetParent !== null;
        if (!visible) return {{ok:false, reason:'not visible',
                               rect:{{w:r.width,h:r.height,top:r.top,left:r.left}},
                               display:cs.display, visibility:cs.visibility}};
        return {{ok:true, x:r.left + r.width/2, y:r.top + r.height/2}};
      }})()
    """)
    if not box or not box.get("ok"):
        return {"clicked": False, "why": (box or {}).get("reason", "unknown"),
                "detail": box}
    for ev in ("mousePressed", "mouseReleased"):
        ws.call("Input.dispatchMouseEvent", {
            "type": ev, "x": box["x"], "y": box["y"],
            "button": "left", "clickCount": 1})
    return {"clicked": True, "x": box["x"], "y": box["y"]}


def wait_for(ws: WS, expr: str, timeout: float = 90.0, interval: float = 0.5):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = js(ws, expr)
        if last:
            return last
        time.sleep(interval)
    return last


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000/#nl")
    ap.add_argument("--port", type=int, default=9223)
    ap.add_argument("--keep-open", action="store_true")
    args = ap.parse_args()

    browser = find_browser()
    profile = tempfile.mkdtemp(prefix="nlchrome")
    proc = subprocess.Popen([
        browser, "--headless=new", f"--remote-debugging-port={args.port}",
        f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
        "--disable-gpu", "--window-size=1280,900", args.url,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    results = []
    try:
        wait_devtools(args.port)
        target = None
        deadline = time.time() + 20
        while time.time() < deadline and not target:
            try:
                target = find_page_target(args.port, "127.0.0.1")
            except Exception:
                pass
            time.sleep(0.4)
        if not target:
            raise SystemExit("找不到页面 target")

        ws = WS(target["webSocketDebuggerUrl"], timeout=60)
        ws.call("Runtime.enable")
        ws.call("Page.enable")

        # 等页面加载完
        wait_for(ws, "document.readyState === 'complete'", timeout=30)
        time.sleep(2)

        # ---------- 检查 1：分区能打开 ----------
        tab_state = js(ws, """
          (function(){
            const b = document.querySelector('.tabs button[data-tab="nl"]');
            if (!b) return {exists:false};
            const r = b.getBoundingClientRect();
            return {exists:true, w:r.width, h:r.height,
                    tab: document.body.dataset.tab};
          })()
        """)
        results.append(("语言标签存在", bool(tab_state and tab_state.get("exists"))))
        results.append(("语言标签可见", bool(tab_state and tab_state.get("w", 0) > 0)))

        # 用真鼠标点标签
        c = click_real(ws, '.tabs button[data-tab="nl"]')
        results.append(("真鼠标点击语言标签", bool(c.get("clicked"))))
        time.sleep(1.2)
        cur = js(ws, "document.body.dataset.tab")
        results.append(("分区切换到 nl", cur == "nl"))

        panel_vis = js(ws, """
          (function(){
            const p = document.getElementById('panel-nl');
            if (!p) return null;
            const r = p.getBoundingClientRect();
            return {w:r.width, h:r.height};
          })()
        """)
        results.append(("语言面板有实际尺寸",
                        bool(panel_vis and panel_vis.get("w", 0) > 0
                             and panel_vis.get("h", 0) > 0)))

        # ---------- 检查 2：真鼠标点「查看上次数据」 ----------
        c = click_real(ws, '#nlChips button[data-q="查看上次数据"]')
        results.append(("真鼠标点击「查看上次数据」", bool(c.get("clicked"))))

        got = wait_for(ws, """
          (function(){
            const msgs = document.querySelectorAll('#nlLog .nlmsg.bot');
            if (msgs.length < 1) return null;
            const last = msgs[msgs.length - 1];
            const body = last.querySelector('.body');
            const txt = body ? body.textContent : '';
            if (!txt || txt.indexOf('模型正在解析') >= 0) return null;
            return {text: txt,
                    meta: Array.from(last.querySelectorAll('.nlmeta span'))
                              .map(s => s.textContent)};
          })()
        """, timeout=120)
        if got:
            print("\n[查看上次数据] 回答：")
            print(got["text"])
            print("[元信息]", got["meta"])
            t = got["text"]
            results.append(("回答含数值", "g" in t))
            results.append(("回答含来源设备", "team01" in t or "设备" in t))
            results.append(("回答含时间戳", "2026-" in t or "距今约" in t))
            results.append(("回答未误报采集成功", "已采集成功" not in t))
        else:
            results.append(("查看上次数据 有回答", False))

        # ---------- 检查 3：真鼠标点「重新采集一次」 ----------
        c = click_real(ws, '#nlChips button[data-q="重新采集一次"]')
        results.append(("真鼠标点击「重新采集一次」", bool(c.get("clicked"))))

        got2 = wait_for(ws, """
          (function(){
            const msgs = document.querySelectorAll('#nlLog .nlmsg.bot');
            if (msgs.length < 2) return null;
            const last = msgs[msgs.length - 1];
            const body = last.querySelector('.body');
            const txt = body ? body.textContent : '';
            if (!txt || txt.indexOf('模型正在解析') >= 0) return null;
            return {text: txt,
                    meta: Array.from(last.querySelectorAll('.nlmeta span'))
                              .map(s => s.textContent)};
          })()
        """, timeout=150)
        if got2:
            print("\n[重新采集一次] 回答：")
            print(got2["text"])
            print("[元信息]", got2["meta"])
            t2 = got2["text"]
            results.append(("采集回答含请求 id", "CMD-" in t2))
            results.append(("采集回答含状态", "done" in t2 or "超时" in t2 or "失败" in t2))
            results.append(("采集回答含新观测条数", "入库" in t2))
        else:
            results.append(("重新采集 有回答", False))

        # ---------- 检查 4：越界提问（问坐标） ----------
        c = click_real(ws, '#nlChips button[data-q="设备现在在哪个位置？给个坐标"]')
        results.append(("真鼠标点击「问坐标」", bool(c.get("clicked"))))
        got3 = wait_for(ws, """
          (function(){
            const msgs = document.querySelectorAll('#nlLog .nlmsg.bot');
            if (msgs.length < 3) return null;
            const last = msgs[msgs.length - 1];
            const body = last.querySelector('.body');
            const txt = body ? body.textContent : '';
            if (!txt || txt.indexOf('模型正在解析') >= 0) return null;
            return {text: txt};
          })()
        """, timeout=120)
        if got3:
            print("\n[问坐标] 回答：")
            print(got3["text"])
            t3 = got3["text"]
            import re
            coord = re.search(r"-?\d{1,3}\.\d{3,}\s*[,，]\s*-?\d{1,3}\.\d{3,}", t3)
            results.append(("越界回答没有坐标", not bool(coord)))
            results.append(("越界回答说明了没有定位源",
                            "没有定位" in t3 or "无法提供" in t3))
        else:
            results.append(("越界 有回答", False))

        # ---------- 检查 5：JS 报错 ----------
        errs = js(ws, "(window.__nlErrors || []).length") or 0
        results.append(("页面无 JS 报错", errs == 0))

        # ---------- 截图 ----------
        p1 = shot(ws, "week4_nl_panel.png")
        print(f"\n截图: {p1}")

    finally:
        if not args.keep_open:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                proc.kill()

    print("\n" + "=" * 70)
    print("第4周网页实测汇总")
    print("=" * 70)
    bad = 0
    for name, ok in results:
        print(f"  {'✓' if ok else '✗'} {name}")
        if not ok:
            bad += 1
    print(f"\n共 {len(results)} 项，未通过 {bad} 项")
    print("（这些是实测观测；是否达标请对照任务卡原文逐条核对）")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
