#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第10—11周网页验收（真 Chrome）

为什么要单独跑一遍浏览器
------------------------
接口测试能证明"服务端算得对"，证明不了"页面上看到的是对的"。
这套界面以前踩过三次假通过：审计读的是从没写入过的列表、量的是隐藏元素得 0、
JS 的 .click() 对隐藏元素照样生效。所以这里只做两件事：

1. **先切到「多源」分区再读元素** —— 分区没切过去，表格是 display:none，
   读出来的文字全是空串，断言会"通过"得莫名其妙；
2. **判据全取页面上的可见文字**，不回头问接口。

验证步骤：
  A. 表格真的渲染出 6 个来源，且状态词齐全（成功/已过期/无此来源）
  B. 点「补传 30 条」→ 那一行变成「已过期」，年龄约 10 分钟
  C. 输入「设备现在在哪？」→ 回复里明确「不能给出坐标」，且有依据行、有三层表
  D. 截图留证

用法：
    python tools/test_week10_web.py
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from browser_test import WS, find_browser, find_page_target, js, wait_devtools  # noqa: E402

URL = "http://127.0.0.1:8000/#multi"
PORT = 9347
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "debug_logs", "_w10_multi_live.png")

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> bool:
    _results.append((bool(ok), name, detail))
    print(("  ✓ " if ok else "  ✗ ") + name + (("　—　" + detail) if detail else ""))
    return bool(ok)


def table_rows(ws) -> list[list[str]]:
    raw = js(ws, """(()=>{
      const tb = document.getElementById('srcBody');
      if (!tb) return '[]';
      return JSON.stringify(Array.from(tb.querySelectorAll('tr')).map(
        tr => Array.from(tr.querySelectorAll('td')).map(td => td.textContent.trim())));
    })()""")
    try:
        return json.loads(raw)
    except ValueError:
        return []


def main() -> int:
    browser = find_browser()
    profile = tempfile.mkdtemp(prefix="w10web_")
    proc = subprocess.Popen([
        browser, "--headless=new", "--no-proxy-server",
        f"--remote-debugging-port={PORT}", f"--user-data-dir={profile}",
        "--no-first-run", "--no-default-browser-check", "--disable-gpu",
        "--window-size=1400,1200", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

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
        ws.call("Page.navigate", {"url": URL})
        js(ws, "new Promise(r=>setTimeout(()=>r(1), 4000))", timeout=40)

        print("=" * 70)
        print("第10—11周网页验收 · 多源状态与可解释上下文（真 Chrome）")
        print("=" * 70)
        print("页面：%s\n" % URL)

        # ---------- 先确认真的在这个分区 ----------
        tab = js(ws, "document.body.dataset.tab")
        check(tab == "multi", "页面停在「多源」分区", "data-tab=%s" % tab)
        visible = js(ws, """(()=>{
          const p = document.getElementById('panel-multi');
          if (!p) return -1;
          const r = p.getBoundingClientRect();
          return Math.round(r.height);
        })()""")
        check(isinstance(visible, (int, float)) and visible > 300,
              "分区可见（高度 > 300px，不是隐藏状态）", "height=%s" % visible)

        # ---------- A. 表格 ----------
        rows = table_rows(ws)
        print("\n[A] 来源表（页面实际文字）")
        for r in rows:
            print("    " + " | ".join(r[:4]))
        check(len(rows) == 6, "表格渲染出 6 个来源", "实际 %d 行" % len(rows))
        joined = " ".join(" ".join(r) for r in rows)
        check("成功" in joined, "有「成功」状态")
        check("已过期" in joined, "有「已过期」状态")
        check("无此来源" in joined, "有「无此来源」状态（本机无 GNSS）")
        check("回放" in joined, "回放来源被标了「回放」")

        # ---------- B. 补传按钮 ----------
        print("\n[B] 点「补传 30 条」")
        js(ws, "document.getElementById('btnBackfill').click(); 1")
        js(ws, "new Promise(r=>setTimeout(()=>r(1), 1200))", timeout=20)
        rows2 = table_rows(ws)
        replay_row = next((r for r in rows2 if "补传回放" in r[0]), None)
        if replay_row:
            print("    " + " | ".join(replay_row[:5]))
            age_text = replay_row[3]
            check(replay_row[2] == "已过期", "补传那行变成「已过期」",
                  replay_row[2])
            check("分" in age_text, "年龄按分钟显示（不是 0 秒）", age_text)
            note = replay_row[-1]
            check("不能当作当前状态" in note, "说明里写明不能当当前状态", note[:40])
        else:
            check(False, "找到「IMU · 补传回放样例」这一行")

        # ---------- C. 问一句 ----------
        print("\n[C] 问「设备现在在哪？」")
        js(ws, """(()=>{
          const i = document.getElementById('ctxQ');
          i.value = '设备现在在哪？';
          document.getElementById('ctxAsk').click();
          return 1;
        })()""")
        js(ws, "new Promise(r=>setTimeout(()=>r(1), 1500))", timeout=20)
        text = js(ws, "document.getElementById('ctxText').textContent.trim()")
        basis = js(ws, "document.getElementById('ctxBasis').textContent.trim()")
        layers = js(ws, """(()=>{
          const t = document.querySelector('#ctxLayers table');
          return t ? t.querySelectorAll('tbody tr').length : 0;
        })()""")
        print("    答：%s" % text[:80])
        print("    依据：%s" % basis[:80])
        check("不能给出坐标" in text, "回复明确说不能给出坐标")
        check(not any(ch.isdigit() for ch in text), "回复里没有坐标数字")
        check("依据" in basis and "规则版本" in basis, "带依据行与规则版本")
        check(layers >= 8, "三层表渲染出多行（观测+派生+决策）", "%d 行" % layers)

        # ---------- D. 截图 ----------
        print("\n[D] 截图")
        r = ws.call("Page.captureScreenshot",
                    {"format": "png", "captureBeyondViewport": True}, timeout=60.0)
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        with open(OUT, "wb") as f:
            f.write(base64.b64decode(r["data"]))
        print("    ✓ %s（%d KB）" % (OUT, os.path.getsize(OUT) // 1024))

        ok = sum(1 for x in _results if x[0])
        total = len(_results)
        print("\n" + "=" * 70)
        print("结果：%d/%d 通过" % (ok, total))
        if ok < total:
            for good, name, detail in _results:
                if not good:
                    print("  ✗ %s　%s" % (name, detail))
        print("=" * 70)
        return 0 if ok == total else 1
    finally:
        proc.terminate()


if __name__ == "__main__":
    sys.exit(main())
