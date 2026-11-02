#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证「双击 html 打开」时按钮还能用

页面加载正常不代表按钮能用：POST 走的是另一条路（预检 + 请求体）。
所以在 file:// 下真的点一次拍照，看它有没有真的拿到图。

用法：
    python tools/test_file_mode.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from browser_test import WS, find_browser, find_page_target, js, wait_devtools  # noqa: E402

URL = "file:///D:/aijiaohu/week1/web/index.html"
PORT = 9338


def main():
    browser = find_browser()
    profile = tempfile.mkdtemp(prefix="filemode_")
    proc = subprocess.Popen([
        browser, "--headless=new", "--no-proxy-server",
        f"--remote-debugging-port={PORT}", f"--user-data-dir={profile}",
        "--no-first-run", "--no-default-browser-check", "--disable-gpu",
        "--window-size=1280,1000", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_devtools(PORT)
        tgt = None
        for _ in range(40):
            tgt = find_page_target(PORT, "about:blank")
            if tgt:
                break
            time.sleep(0.3)
        ws = WS(tgt["webSocketDebuggerUrl"])
        ws.call("Page.enable")
        ws.call("Runtime.enable")
        ws.call("Page.navigate", {"url": URL})
        js(ws, "new Promise(r=>setTimeout(()=>r(1), 4000))", timeout=30)

        print("=" * 62)
        print("file:// 方式打开 · 按钮实测")
        print("=" * 62)

        before = js(ws, "(()=>{const e=document.getElementById('snapState');return e?e.textContent.trim():null})()")
        print(f"点击前状态: {before}")

        # 真的点那个按钮（不是直接调函数，走一遍点击链路）
        js(ws, "(()=>{const b=document.getElementById('snapBtn'); if(!b) return 'no-btn'; b.click(); return 'clicked';})()")
        print("已点击「拍一张」")

        ok = None
        for _ in range(40):
            js(ws, "new Promise(r=>setTimeout(()=>r(1), 500))", timeout=10)
            st = js(ws, """(()=>{
              const s=document.getElementById('snapState'), m=document.getElementById('snapMeta');
              const img=document.getElementById('snapImg');
              return {state: s?s.textContent.trim():null, meta: m?m.textContent.trim():null,
                      w: img?img.naturalWidth:0, h: img?img.naturalHeight:0,
                      src: img?(img.getAttribute('src')||'').slice(0,80):''};
            })()""")
            if st and st["state"] and ("成功" in st["state"] or "失败" in st["state"] or "不可用" in st["state"]):
                ok = st
                break

        print(f"结果状态: {ok['state'] if ok else '(超时未结束)'}")
        print(f"图上尺寸: {ok['w']}x{ok['h']}   (0x0 = 图没加载出来)")
        print(f"图片地址: {ok['src']}")
        print(f"说明文字: {(ok['meta'] or '')[:160]}")

        # 顺带确认语音面板的接口地址是否走 API_BASE
        probe = js(ws, """(()=>{
          const d=window.__diag;
          return d ? {ok:d.ok, fail:d.reqs.length} : null;
        })()""")
        print(f"\n请求统计: 成功 {probe['ok'] if probe else '?'} 条, 失败 {probe['fail'] if probe else '?'} 条")

        verdict = bool(ok and "成功" in (ok["state"] or "") and ok["w"] > 0)
        print("\n>>> " + ("通过：双击打开时拍照可用" if verdict else "未通过：需要继续查"))
        ws.close()
        return 0 if verdict else 1
    finally:
        proc.terminate()


if __name__ == "__main__":
    sys.exit(main())
