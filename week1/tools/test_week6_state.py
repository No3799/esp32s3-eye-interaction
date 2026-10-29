#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""第6周 · 状态管理验收：取消、忙时提示、连续两次查询。

任务卡原文（第6周）：
    当堂验证　「取消后迟到结果不得重新播放或触发动作；新任务不被旧任务结果覆盖。」
    后2学时　「测试用户中途取消、连续两次查询和慢响应。」

这里测的三件事，都必须在**真浏览器**里做：
    T1 中途取消     —— 取消后，那个已经发出去的请求回来了，绝不能写进对话、更不能播报
    T2 忙时再发     —— 不能"没反应"（人会以为消息丢了），也不能静默排队把新任务顶掉
    T3 连续两次查询 —— 先后两次都要各自拿到结果，不互相覆盖

为什么不能只测接口：T1 的关键是**响应回来那一刻**要不要丢弃，
那是前端的一个判定，接口层根本看不到。

用法（需要 8000 上的服务在跑）：
    python tools/test_week6_state.py
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

    base = "http://127.0.0.1:8000/#nl"
    browser = bt.find_browser()
    profile = tempfile.mkdtemp(prefix="w6state_")
    port = 9493
    args = [browser, "--headless=new", f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}", "--no-first-run", "--disable-gpu",
            "--hide-scrollbars", "--window-size=1280,1000",
            "--autoplay-policy=no-user-gesture-required", base]
    p = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + str(detail)) if detail else ""))
        ok = ok and bool(cond)

    def js(ws, expr, timeout=20.0):
        return bt.js(ws, expr, timeout=timeout)

    LAST_BOT = ("(()=>{const n=document.querySelectorAll('#nlLog .nlmsg.bot .body');"
                "return n.length? n[n.length-1].innerText : '';})()")
    BOT_N = "document.querySelectorAll('#nlLog .nlmsg.bot').length"
    STATE = "document.getElementById('nlState').innerText"

    def wait_state(ws, want, timeout=40):
        end = time.time() + timeout
        while time.time() < end:
            if (js(ws, STATE) or "").strip() == want:
                return True
            time.sleep(0.3)
        return False

    def press_esc(ws):
        for t in ("keyDown", "keyUp"):
            ws.call("Input.dispatchKeyEvent", {
                "type": t, "key": "Escape", "code": "Escape",
                "windowsVirtualKeyCode": 27, "nativeVirtualKeyCode": 27})

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

        check("语言分区已切换", js(ws, "document.body.getAttribute('data-tab')") == "nl")
        check("停止按钮存在且初始为禁用",
              js(ws, "(()=>{const b=document.getElementById('nlStop');"
                     "return !!b && b.disabled===true;})()") is True)

        # ---------------- T1 中途取消 ----------------
        print("\nT1 中途取消（取消后迟到结果不得出现）")
        click = btnl.click_real(ws, "#nlChips button[data-q]")
        check("点了「查看上次数据」", click.get("clicked"), str(click))
        time.sleep(0.8)
        check("已进入处理中", (js(ws, STATE) or "").strip() == "处理中…", js(ws, STATE))
        check("停止按钮此时可用",
              js(ws, "!document.getElementById('nlStop').disabled") is True)

        press_esc(ws)
        time.sleep(0.4)
        check("Esc 之后状态变成已取消",
              (js(ws, STATE) or "").strip() == "已取消", js(ws, STATE))

        n_at_cancel = js(ws, BOT_N)
        print("    等 12 秒，看那个迟到的响应会不会自己冒出来…")
        time.sleep(12)
        last = js(ws, LAST_BOT) or ""
        check("迟到结果没有写进对话", "已读取" not in last, last[:60])
        check("那条气泡仍写着已取消", "已取消" in last, last[:60])
        check("对话条数没有因为迟到结果增加", js(ws, BOT_N) == n_at_cancel,
              "%s → %s" % (n_at_cancel, js(ws, BOT_N)))
        check("状态没有被迟到结果改回完成/已拒绝",
              (js(ws, STATE) or "").strip() == "已取消", js(ws, STATE))

        # ---------------- T2 忙时再发 ----------------
        print("\nT2 处理中再发一句：不能静默吞掉")
        btnl.click_real(ws, "#nlChips button[data-q]")
        time.sleep(0.4)
        n_before = js(ws, BOT_N)
        btnl.click_real(ws, "#nlChips button[data-q]")
        time.sleep(0.4)
        st = (js(ws, STATE) or "").strip()
        check("给了明确提示（不是没反应）", "上一个还在处理" in st, st)
        check("没有发出第二个请求", js(ws, BOT_N) == n_before,
              "%s → %s" % (n_before, js(ws, BOT_N)))
        press_esc(ws)                     # 收拾干净
        time.sleep(0.5)

        # ---------------- T3 连续两次 ----------------
        # 用「含糊」那句而不是「查看上次数据」：前者不依赖设备在不在线，
        # 才能只测"两次结果互不覆盖"这一件事。设备在线与否是另一条链路的变量，
        # 混进来会让这个测试在板子拔掉时假失败。
        print("\nT3 先后两次提问：各自拿到结果，不互相覆盖")
        n0 = js(ws, BOT_N)
        UNCLEAR_N = ("Array.from(document.querySelectorAll('#nlLog .nlmsg.bot .body'))"
                     ".filter(e => e.innerText.indexOf('没能判断') >= 0).length")
        u0 = js(ws, UNCLEAR_N)
        CHIP_UNCLEAR = "#nlChips button[data-q='看看数据吧']"
        btnl.click_real(ws, CHIP_UNCLEAR)
        check("第一次完成", wait_state(ws, "完成"), js(ws, STATE))
        first = js(ws, LAST_BOT) or ""
        check("第一次拿到了真实回答（追问，而不是报错）",
              "没能判断" in first or "意图不清" in first, first[:60])

        btnl.click_real(ws, CHIP_UNCLEAR)
        check("第二次完成", wait_state(ws, "完成"), js(ws, STATE))
        second = js(ws, LAST_BOT) or ""
        check("第二次同样拿到真实回答", "没能判断" in second or "意图不清" in second, second[:60])
        check("对话多了两条回答（不是合在一起）", js(ws, BOT_N) >= n0 + 2,
              "%s → %s" % (n0, js(ws, BOT_N)))
        check("两次回答不是同一段文字被复用（各占一条气泡）",
              js(ws, UNCLEAR_N) == u0 + 2,
              "含追问的气泡 %s → %s" % (u0, js(ws, UNCLEAR_N)))

        # ---------------- T4 停止播报 ----------------
        # 本机没有扬声器，但"有没有在播"是可观察的：speechSynthesis.speaking。
        # 任务卡「前2学时」要求实现"停止播报"，所以这条不能只靠说"我实现了"。
        print("\nT4 停止播报：按停止要能把正在念的那句掐掉")
        js(ws, "(()=>{const u=new SpeechSynthesisUtterance("
               "'这是一段用来验证停止播报的句子，故意写长一点，"
               "好在它念到一半的时候把它掐断。');"
               "u.lang='zh-CN'; speechSynthesis.speak(u); return 1;})()")
        time.sleep(1.5)
        check("播报确实在跑", js(ws, "speechSynthesis.speaking") is True,
              js(ws, "speechSynthesis.speaking"))
        press_esc(ws)
        time.sleep(0.6)
        check("按 Esc 之后播报立刻停止", js(ws, "speechSynthesis.speaking") is False,
              js(ws, "speechSynthesis.speaking"))

        check("页面无 JS 报错", js(ws, "window.__errs ? window.__errs.length : 0") in (0, None))

        print()
        print("结果：" + ("全部通过" if ok else "有失败项"))
        return 0 if ok else 1
    finally:
        p.terminate()


if __name__ == "__main__":
    sys.exit(main())
