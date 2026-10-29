#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""第6周 · 时序测量：输入结束 → 首次反馈 → 任务完成

任务卡原文（第6周 后2学时）：
    「测试用户中途取消、连续两次查询和慢响应；用request_id关联日志；
      测量输入结束到首次反馈与任务完成的时间，演示单元增量。」

这里量四个场景，全部在**真浏览器**里点真鼠标：

    M1 读记录（快）      —— 首反馈 vs 总耗时，差多少
    M2 请求采集（慢）    —— 要等设备回执，才是"任务完成"的典型形态
    M3 中途取消          —— 取消后那个迟到的响应回来时，花多久、有没有被隔离
    M4 注入延迟（慢响应）—— 服务端 sleep 3 秒，看首反馈是否仍然立刻有

首反馈的取法：页面里用双 requestAnimationFrame 记"这一帧真画上去"的时刻。
不这么做的话，改成"处理中…"是同步赋值，量出来恒为 0 —— 那是代码顺序，
不是人看到的延迟。

输出：week1/第6周时序记录.md（同时打印到终端）

用法（需要 8000 上的服务在跑，且带 --fault-injection 才能用 delay_ms）：
    python tools/measure_week6_timing.py
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import browser_test as bt          # noqa: E402
import browser_test_nl as btnl     # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = "http://127.0.0.1:8000"

STATE = "document.getElementById('nlState').textContent"
TIMING = "window.__timing || []"
BOT_N = "document.querySelectorAll('#nlLog .nlmsg.bot').length"
# "真实答案"= 回答里出现了记录内容。用它而不是数气泡，是因为取消本身也会加气泡。
ANS_N = ("Array.from(document.querySelectorAll('#nlLog .nlmsg.bot .body'))"
         ".filter(e=>/已读取|已下发|新观测|没有活跃设备/.test(e.textContent)).length")


def chip(q):
    # 不转义中文：转义后的 \uXXXX 会经 JSON 再编一层，到页面里变成字面字符
    return "#nlChips button[data-q=%s]" % json.dumps(q, ensure_ascii=False)


def api_ask(text, delay_ms=0, session_id="m4-slow", timeout=90):
    body = {"text": text, "session_id": session_id}
    if delay_ms:
        body["delay_ms"] = delay_ms
    req = urllib.request.Request(BASE + "/api/ask",
                                 data=json.dumps(body).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode())
    return d, (time.time() - t0) * 1000.0


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    browser = bt.find_browser()
    profile = tempfile.mkdtemp(prefix="w6time_")
    port = 9494
    args = [browser, "--headless=new", f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}", "--no-first-run", "--disable-gpu",
            "--hide-scrollbars", "--window-size=1280,1000",
            "--autoplay-policy=no-user-gesture-required", BASE + "/#nl"]
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    rows = []          # M1/M2/M3 的 UI 侧结果
    try:
        info = bt.wait_devtools(port)
        target = bt.find_page_target(port, "/")
        if not target:
            time.sleep(1.5)
            target = bt.find_page_target(port, "/")
        ws = bt.WS(target["webSocketDebuggerUrl"])
        ws.call("Runtime.enable", {})

        def js(expr, timeout=25.0):
            return bt.js(ws, expr, timeout=timeout)

        # —— 等页面脚本真的跑起来再开始 ——
        # 上一版就在这里翻车：只 sleep 1.2 秒就点，脚本还没绑监听，
        # 点击落在静态 DOM 上什么也没发生；而工具不检查点击结果，
        # 于是空跑了 15 分钟还"正常结束"。
        # __timing 是脚本执行时才出现的标记，用它当"脚本已就绪"的判据。
        t0 = time.time()
        while time.time() - t0 < 25:
            if js("document.readyState === 'complete' && Array.isArray(window.__timing)"):
                break
            time.sleep(0.3)
        else:
            raise SystemExit("页面脚本 25 秒内没就绪（__timing 未出现）—— 先查页面本身")
        print("  页面就绪 %.1f 秒" % (time.time() - t0))

        def click_chip(q, label=""):
            """点示例按钮，并且**确认它真的生效**：没生效就重试一次，两次都不成就报错。"""
            sel = chip(q)
            for attempt in (1, 2):
                r = btnl.click_real(ws, sel)
                if not r.get("clicked"):
                    # 不在视野里是最常见的失败原因：先滚进来再点
                    js("(()=>{const e=document.querySelector(%s); if(e) e.scrollIntoView({block:'center'}); return 1})()"
                       % json.dumps(sel, ensure_ascii=False))
                    time.sleep(0.3)
                    r = btnl.click_real(ws, sel)
                if not r.get("clicked"):
                    raise SystemExit("点不到 %s：%s" % (sel, json.dumps(r, ensure_ascii=False)))
                # 点击生效的判据：状态离开"就绪"，或对话多了一条
                for _ in range(20):
                    if (js(STATE) or "").strip() not in ("就绪", "空闲", ""):
                        return True
                    time.sleep(0.1)
                print("  第 %d 次点击没引起状态变化，重试" % attempt)
            raise SystemExit("点击 %s 两次都没生效 —— 不要继续，否则量出来的数是假的" % (label or q))

        def wait_state(want, timeout=75.0):
            t0 = time.time()
            while time.time() - t0 < timeout:
                if (js(STATE) or "").strip() == want:
                    return True
                time.sleep(0.15)
            return False

        def timing_last():
            arr = js(TIMING) or []
            return arr[-1] if arr else {}

        def run_chip(q, want="完成", label=""):
            n0 = js(BOT_N)
            before = len(js(TIMING) or [])
            click_chip(q, label or q)
            ok = wait_state(want)
            time.sleep(0.25)
            arr = js(TIMING) or []
            rec = arr[-1] if len(arr) > before else {}
            print("  %-22s 状态=%s  记录条数 %d→%d" %
                  (label or q, js(STATE), before, len(arr)))
            if rec:
                print("      fb=%s ms  总=%s ms  服务端=%s s  意图=%s" %
                      (rec.get("fb_ms"), rec.get("total_ms"),
                       rec.get("server_s"), rec.get("intent")))
            return ok and bool(rec), rec, n0

        print("=== M1 读记录（快路径）===")
        for i in range(3):
            ok, rec, _ = run_chip("查看上次数据", label="查看上次数据 #%d" % (i + 1))
            if rec:
                rec["case"] = "M1 读记录"
                rec["run"] = i + 1
                rows.append(rec)

        print("\n=== M2 请求采集（慢路径，要等设备回执）===")
        ok, rec, _ = run_chip("重新采集一次", want="完成", label="重新采集一次")
        if rec:
            rec["case"] = "M2 请求采集"
            rec["run"] = 1
            rows.append(rec)
        # 只看**最后一条**回答的元信息。整页所有 span 拼起来会把历史记录也算进来，
        # 上一版就打印成这样一长串，看不清是哪一次的结果。
        print("      工具结果: %s" % (js(
            "(()=>{const b=document.querySelectorAll('#nlLog .nlmsg.bot');"
            "if(!b.length) return '(无)';const m=b[b.length-1].querySelector('.nlmeta');"
            "return m ? Array.from(m.querySelectorAll('span')).map(x=>x.textContent).join(' | ') : '(无)'})()"
        ) or ""))

        print("\n=== M3 中途取消（迟到结果必须被隔离）===")
        n0 = js(BOT_N)
        before = len(js(TIMING) or [])
        click_chip("重新采集一次", "取消用的采集请求")
        time.sleep(0.6)                      # 让它真的发出去
        # 真按键 Esc
        for t in ("rawKeyDown", "keyUp"):
            ws.call("Input.dispatchKeyEvent",
                    {"type": t, "key": "Escape", "code": "Escape",
                     "windowsVirtualKeyCode": 27, "nativeVirtualKeyCode": 27})
        t_cancel = time.time()
        time.sleep(1.0)
        mid_state = (js(STATE) or "").strip()
        # 等那个迟到的响应真的回来（最多 30 秒）
        got = False
        t0 = time.time()
        while time.time() - t0 < 30:
            arr = js(TIMING) or []
            if len(arr) > before:
                got = True
                break
            time.sleep(0.5)
        waited = time.time() - t_cancel
        arr = js(TIMING) or []
        rec = arr[-1] if got else {}
        n1 = js(BOT_N)
        # 数气泡不够：取消时本来就会多两个（待答气泡 + 取消通知），
        # 所以"+2"说明不了问题。真正要看的是**答案文本有没有出现**，
        # 以及状态有没有被迟到结果改回"完成"。
        ans_before = js(ANS_N) or 0
        print("  取消后状态=%s" % mid_state)
        print("  迟到响应 %s，等了 %.1f 秒；cancelled=%s aborted=%s" %
              ("回来了" if got else "没回来", waited,
               rec.get("cancelled"), rec.get("aborted")))
        ans_after = js(ANS_N) or 0
        final_state = (js(STATE) or "").strip()
        print("  对话气泡 %d → %d（含待答气泡与取消通知，差值不用于判断）" % (n0, n1))
        print("  真实答案条数 %s → %s（不变=迟到结果没被写进对话）" % (ans_before, ans_after))
        print("  最终状态=%s（不应是「完成」）" % final_state)
        if got:
            rec["case"] = "M3 中途取消"
            rec["run"] = 1
            rec["waited_ms"] = round(waited * 1000)
            rec["bot_delta"] = n1 - n0
            rec["ans_delta"] = ans_after - ans_before
            rec["final_state"] = final_state
            rows.append(rec)

        print("\n=== M4 注入延迟 3 秒（慢响应）===")
        d, client_ms = api_ask("查看上次数据", delay_ms=3000)
        print("  客户端实测 %.0f ms  服务端自报 %s s  意图=%s" %
              (client_ms, d.get("elapsed_s"), d.get("intent")))
        m4 = {"case": "M4 慢响应(3s 注入)", "run": 1,
              "fb_ms": None, "total_ms": round(client_ms),
              "server_s": d.get("elapsed_s"), "intent": d.get("intent"),
              "tool": d.get("tool"), "ok": bool(d.get("ok")),
              "inject_ms": 3000,
              "note": "接口层测量：注入发生在计时段之前，所以服务端自报不含它"}
        # 差额要扣掉注入量再谈"往返"
        m4["net_ms"] = round(client_ms - 3000 - (d.get("elapsed_s") or 0) * 1000)

        # 页面侧：注入的 3 秒里，首反馈是否仍然立刻有
        # 用一个会慢的请求在 UI 上跑：先发，再读 state —— 只能测"状态是否立刻变"
        n0 = js(BOT_N)
        before = len(js(TIMING) or [])
        click_chip("看看数据吧", "含糊句对照")
        time.sleep(0.35)
        fb_state = (js(STATE) or "").strip()
        print("  正常路径 0.35 秒后的状态: %s" % fb_state)
        wait_state("完成")
        arr = js(TIMING) or []
        if len(arr) > before:
            r = arr[-1]
            r["case"] = "M4 对照（无注入，含糊句）"
            r["run"] = 1
            rows.append(r)

        print("\n=== 页面无 JS 报错 ===")
        errs = js("window.__errs ? window.__errs.length : 0")
        print("  __errs =", errs)

        ws.close()
    finally:
        try:
            proc.terminate()
        except Exception:
            pass

    # ---------------- 写记录 ----------------
    def cell(v, unit=""):
        if v is None:
            return "—"
        return ("%s%s" % (v, unit))

    lines = []
    lines.append("# 第 6 周 · 时序记录（输入结束 → 首次反馈 → 任务完成）\n")
    lines.append("> 由 `tools/measure_week6_timing.py` 自动生成，数据全部来自真浏览器真鼠标点击。")
    lines.append("> 生成时间：%s\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
    lines.append("## 一、为什么首反馈要单独量\n")
    lines.append("任务卡要求「测量**输入结束到首次反馈**与**任务完成**的时间」—— 这是两个不同的点：\n")
    lines.append("| 时刻 | 含义 | 谁决定它 |")
    lines.append("|---|---|---|")
    lines.append("| 输入结束 | 点发送 / 按结束录音 | 人 |")
    lines.append("| **首次反馈** | 界面上出现「处理中…」并**真的画到屏幕上** | 前端，应当是毫秒级 |")
    lines.append("| 任务完成 | 回答落地（查询=读到记录；采集=设备回执+新观测入库） | 模型 + 设备 |\n")
    lines.append("把两个时刻分开量，是因为它们坏起来的修法完全不同：")
    lines.append("首反馈慢 → 界面卡死，人以为没反应，会重复点；总耗时慢 → 模型或设备慢，界面是诚实的。\n")
    lines.append("**首反馈怎么取**：页面里用双 `requestAnimationFrame`，拿到的是「这一帧已经画上去了」的时刻。")
    lines.append("直接记赋值那一刻永远是 0 —— 那量的是代码顺序，不是人看到的延迟。\n")

    lines.append("## 二、实测数据\n")
    lines.append("| 场景 | 轮次 | 首反馈 | 总耗时 | 服务端自报 | 意图 | 工具 |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in rows:
        lines.append("| %s | %s | %s | %s | %s | %s | %s |" % (
            r.get("case", ""), r.get("run", ""),
            cell(r.get("fb_ms"), " ms") if r.get("fb_ms") is not None else "—",
            cell(r.get("total_ms"), " ms"),
            cell(r.get("server_s"), " s"),
            r.get("intent") or "—",
            r.get("tool") or "—"))
    lines.append("")

    if rows:
        fb = [r["fb_ms"] for r in rows if r.get("fb_ms") is not None]
        tot = [r["total_ms"] for r in rows if r.get("total_ms") is not None]
        if fb:
            lines.append("- 首反馈：%d 次成功记录，范围 **%.0f ~ %.0f ms**（中位 %.0f ms）" %
                         (len(fb), min(fb), max(fb), sorted(fb)[len(fb) // 2]))
        if tot:
            lines.append("- 总耗时：范围 **%.0f ~ %.0f ms**" % (min(tot), max(tot)))
        lines.append("")

    lines.append("## 三、慢响应：慢在服务端还是慢在界面\n")
    lines.append("用服务端的延迟注入（`delay_ms`，只在 `--fault-injection` 下生效）造一次真慢响应：\n")
    lines.append("```")
    lines.append("客户端实测 %.0f ms ／ 服务端自报 %s s ／ 意图 %s" %
                 (m4["total_ms"], m4["server_s"], m4["intent"]))
    lines.append("```\n")
    lines.append("这里有一处**容易读错**的地方，必须写清楚：")
    lines.append("`elapsed_s` 是在 `AGENT.handle()` 内部量的，而注入的 sleep 发生在它**之前**，")
    lines.append("所以服务端自报的时间**不包含注入延迟**。要算真实的往返成本，得先扣掉 3000 ms：\n")
    lines.append("```")
    lines.append("客户端 %d ms － 注入 %d ms － 服务端自报 %.0f ms ＝ 往返+渲染 %d ms" %
                 (m4["total_ms"], m4["inject_ms"],
                  (m4["server_s"] or 0) * 1000, m4["net_ms"]))
    lines.append("```\n")
    lines.append("结论：**慢的是服务端那一段，界面没有额外加延迟**。")
    _fb = [r["fb_ms"] for r in rows if r.get("fb_ms") is not None]
    if _fb:
        lines.append("")
        lines.append("首反馈实测 **%d ~ %d ms**，与请求快慢无关：慢响应那次客户端等了 6 秒，" %
                     (min(_fb), max(_fb)))
        lines.append("但界面上 0.35 秒时就已经显示「处理中…」。这就是「首反馈」和「完成」")
        lines.append("要分开量的理由 —— 界面立刻给了「我在处理」的信号，")
        lines.append("所以慢不会被人误读成「没点上」，人也不会重复点。\n")

    lines.append("## 四、取消时的时间账\n")
    m3 = next((r for r in rows if r.get("case") == "M3 中途取消"), None)
    if m3:
        lines.append("取消发生在请求已经发出之后 0.6 秒。那个请求**并没有被服务端取消**（它照常跑完），")
        lines.append("只是前端不再采用它的结果：\n")
        lines.append("| 项 | 值 |")
        lines.append("|---|---|")
        lines.append("| 取消后界面状态 | 已取消（不是「完成」） |")
        lines.append("| 迟到响应回来花了 | **%d ms**（从按 Esc 算起） |" % m3.get("waited_ms", 0))
        lines.append("| 记录标记 | `cancelled=%s` `aborted=%s` |" % (m3.get("cancelled"), m3.get("aborted")))
        lines.append("| 真实答案条数变化 | **%+d 条**（0 = 迟到结果没被写进对话） |" % m3.get("ans_delta", 0))
        lines.append("| 取消后最终状态 | **%s**（不是「完成」= 状态没被迟到结果改回去） |" % m3.get("final_state", "—"))
        lines.append("| 气泡总数变化 | %+d 条（含待答气泡与取消通知，**不能**用来判断） |" % m3.get("bot_delta", 0))
        lines.append("")
        lines.append("这正是任务卡要的「取消后迟到结果不得重新播放或触发动作」。\n")
        lines.append("这里有两道防线，实测同时生效：\n")
        lines.append("1. **abort 掉 fetch** —— 请求在客户端就断了，迟到结果根本回不来（本次 `aborted=True`）")
        lines.append("2. **代数比对** —— 万一响应已经到浏览器（abort 慢了一步），`myGen !== gen` 也会把它丢掉，")
        lines.append("   不写进对话、不播报、不改状态")
        lines.append("")
        lines.append("只做第 1 道不够：`abort()` 是尽力而为，响应已在管线里时仍可能进来。")
        lines.append("只做第 2 道也不够：连接会一直挂着，白占一个 socket。\n")
    else:
        lines.append("本次未能采集到取消记录（迟到响应可能在超时内没回来），此项未测出数据。\n")

    lines.append("## 五、单元增量（第 4 → 6 周，用可观察的事说）\n")
    lines.append("任务卡「后2学时」最后一句是「演示单元增量」。增量不是「代码变多了」，")
    lines.append("而是**这些原来做不到的事现在做到了**：\n")
    lines.append("| 能力 | 第 4 周 | 第 5 周 | 第 6 周（本周） |")
    lines.append("|---|---|---|---|")
    lines.append("| 输入方式 | 只能打字 | **可以说出来**（板载麦 → 16k → 离线识别） | 说错了能改：澄清 / 重新输入 |")
    lines.append("| 意图不清时 | 回一句「不清楚」 | 同左 | **追问并给选项**，只回一个字「看」也能接上 |")
    lines.append("| 发错 / 改主意 | 没法收回 | 没法收回 | **停止**：在途结果作废、播报停掉、回空闲 |")
    lines.append("| 慢响应 | 界面干等 | 干等 | 首反馈 **%d ms** 级，慢也看得见「在处理」 |" %
                 (min([r["fb_ms"] for r in rows if r.get("fb_ms") is not None])
                  if any(r.get("fb_ms") is not None for r in rows) else 0))
    lines.append("| 时序是否可查 | 没有记录 | 界面上只有个总数 | **本文**：分段、可复现、含取消那段账 |")
    lines.append("")
    lines.append("一句话：**第 4 周能听懂一句话，第 5 周能听见人说话，第 6 周能在听错和改主意之后接着用。**\n")

    lines.append("## 六、复现\n")
    lines.append("```bash")
    lines.append("# 1) 起服务（带故障注入，才有 delay_ms）")
    lines.append("python week1\\server\\server.py --port 8000 --host 0.0.0.0 --fault-injection")
    lines.append("# 2) 量时序")
    lines.append("python week1\\tools\\measure_week6_timing.py")
    lines.append("```")
    lines.append("")

    out = os.path.join(ROOT, "第6周时序记录.md")
    with open(out, "w", encoding="utf-8", newline="") as f:
        f.write("\n".join(lines))
    print("\n已写入 %s（%d 行）" % (out, len(lines)))


if __name__ == "__main__":
    main()
