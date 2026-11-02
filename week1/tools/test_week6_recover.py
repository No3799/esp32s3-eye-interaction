#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第 6 周补验：反悔后的安全恢复（真浏览器）

任务卡「提交与迁移」点名要迁移一条「用户反悔后安全恢复」的处理规则。
代码里三条都写了，但一直只做过 JS 语法检查，**没在真浏览器点过** ——
当时下午暂停了浏览器自动化授权，所以只能记"未验证"。

这条规则要用浏览器验，因为它的三条全在**界面行为**上：
  ① 不自动重发 —— 取消不能变成"稍后自动执行"
  ② 不留空手   —— 那话要填回输入框，省去重打
  ③ 说清边界   —— 明确讲设备指令不会被撤回

验证步骤（design/15 §10 写的判据）：
  发出请求 → 按停止 → 确认输入框被填回、且没有第二个请求发出

判据全在页面上取，不靠"我觉得没问题"：
  · /api/ask 的调用次数（在页面里挂钩子数，不信网络面板）
  · 输入框的 value
  · 状态标签与消息文本
  · 取消后干等 8 秒，看有没有第二个回答冒出来

用法：
    python tools/test_week6_recover.py
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

URL = "http://127.0.0.1:8000/#nl"
PORT = 9341
QUESTION = "查看上次数据"

# 在页面脚本之前挂钩子：数 /api/ask 被调了几次
INJECT = r"""
window.__ask = 0;
window.__askUrls = [];
(function(){
  var of = window.fetch;
  window.fetch = function(u, o){
    var url = (typeof u === 'string') ? u : ((u && u.url) || '');
    if (url.indexOf('/api/ask') >= 0) {
      window.__ask++;
      window.__askUrls.push(url);
    }
    return of.apply(this, arguments);
  };
})();
"""


def main():
    browser = find_browser()
    profile = tempfile.mkdtemp(prefix="w6rec_")
    proc = subprocess.Popen([
        browser, "--headless=new", "--no-proxy-server",
        f"--remote-debugging-port={PORT}", f"--user-data-dir={profile}",
        "--no-first-run", "--no-default-browser-check", "--disable-gpu",
        "--window-size=1400,1000", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    ok_all = True
    try:
        wait_devtools(PORT)
        tgt = None
        for _ in range(40):
            tgt = find_page_target(PORT, "about:blank")
            if tgt:
                break
            time.sleep(0.3)
        if not tgt:
            raise SystemExit("没找到标签页")

        ws = WS(tgt["webSocketDebuggerUrl"])
        ws.call("Page.enable")
        ws.call("Runtime.enable")
        ws.call("Page.addScriptToEvaluateOnNewDocument", {"source": INJECT})
        ws.call("Page.navigate", {"url": URL})
        js(ws, "new Promise(r=>setTimeout(()=>r(1), 3500))", timeout=30)

        # 确保在语言分区（hash 直达）
        js(ws, "document.body.setAttribute('data-tab','nl'); location.hash='#nl'; 1")
        js(ws, "new Promise(r=>setTimeout(()=>r(1), 400))", timeout=10)

        print("=" * 66)
        print("第 6 周补验 · 反悔后的安全恢复（真 Chrome）")
        print("=" * 66)
        print(f"页面: {URL}")
        print(f"问题: {QUESTION}\n")

        # ---------- 1. 发出请求 ----------
        js(ws, f"""(()=>{{
          const i = document.getElementById('nlInput');
          i.value = {json.dumps(QUESTION)};
          document.getElementById('nlSend').click();
          return 1;
        }})()""")
        print("[1] 已点「发送」")

        # 请求在途（真机读记录约 3 秒），等 350ms 后按停止
        js(ws, "new Promise(r=>setTimeout(()=>r(1), 350))", timeout=10)
        inflight = js(ws, "window.__ask")
        stopped_at = js(ws, """(()=>{
          const s = document.getElementById('nlState');
          return s ? s.textContent.trim() : null;
        })()""")
        print(f"    点停止时：/api/ask 已调用 {inflight} 次，状态=「{stopped_at}」")

        js(ws, "document.getElementById('nlStop').click(); 1")
        print("[2] 已点「停止」")

        js(ws, "new Promise(r=>setTimeout(()=>r(1), 600))", timeout=10)

        # ---------- 2. 读三条规则的结果 ----------
        snap = js(ws, """(()=>{
          const i = document.getElementById('nlInput');
          const s = document.getElementById('nlState');
          const log = document.getElementById('nlLog');
          const blocks = log ? Array.from(log.children) : [];
          const last = blocks.length ? blocks[blocks.length-1].innerText.trim() : '';
          return {
            input: i ? i.value : null,
            selected: i ? (i.selectionStart === 0 && i.selectionEnd === (i.value||'').length) : false,
            state: s ? s.textContent.trim() : null,
            blocks: blocks.length,
            last: last,
            allText: log ? log.innerText : ''
          };
        })()""")

        print("\n[3] 三条规则逐条对：")
        a = snap["input"] == QUESTION
        print(f"     ① 输入框被填回原文  : {'✓' if a else '✗'}  value = {snap['input']!r}")
        ok_all &= a

        b = "已取消" in (snap["state"] or "")
        print(f"     ② 状态变成「已取消」: {'✓' if b else '✗'}  实际 = {snap['state']!r}")
        ok_all &= b

        c = ("撤回" in (snap["allText"] or "")) and ("设备" in (snap["allText"] or ""))
        print(f"     ③ 说明「不撤回设备指令」: {'✓' if c else '✗'}")
        if c:
            line = [l for l in snap["last"].splitlines() if "撤回" in l]
            print(f"        页面原话：{line[0].strip() if line else snap['last'][:80]}")
        ok_all &= c

        print(f"\n     顺带：填回后原文是否被选中 = {snap['selected']}（方便直接改）")

        # ---------- 3. 干等 8 秒，看有没有自动重发 ----------
        print("\n[4] 等 8 秒，确认没有第二个请求、也没有迟到结果冒出来 ...")
        js(ws, "new Promise(r=>setTimeout(()=>r(1), 8000))", timeout=20)
        after = js(ws, """(()=>{
          const log = document.getElementById('nlLog');
          return {
            ask: window.__ask,
            urls: window.__askUrls,
            blocks: log ? log.children.length : 0,
            state: (document.getElementById('nlState')||{}).textContent,
            text: log ? log.innerText : ''
          };
        })()""")

        d = after["ask"] == inflight
        print(f"     /api/ask 调用次数: {inflight} -> {after['ask']}  "
              f"{'✓ 没有自动重发' if d else '✗ 又多发了请求'}")
        ok_all &= d

        e = after["blocks"] == snap["blocks"]
        print(f"     消息条数: {snap['blocks']} -> {after['blocks']}  "
              f"{'✓ 没有迟到回答插进来' if e else '✗ 取消后又冒出新回答'}")
        ok_all &= e

        f = "已取消" in (after["state"] or "")
        print(f"     状态仍是「{after['state']}」 {'✓' if f else '✗ 被改回去了'}")
        ok_all &= f

        print("\n" + "=" * 66)
        print(">>> " + ("全部通过：这条规则可以记「已验证」" if ok_all
                        else "有未通过项，不能记已验证"))
        print("=" * 66)
        ws.close()
        return 0 if ok_all else 1
    finally:
        proc.terminate()


if __name__ == "__main__":
    sys.exit(main())
