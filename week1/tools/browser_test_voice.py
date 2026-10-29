#!/usr/bin/env python3
"""第5周：真实浏览器验收 —— 语音入口的完整链路。

做的是什么：
  用 Chrome 无头 + **假麦克风**（--use-fake-device-for-media-stream，浏览器会持续输出一个提示音），
  点「开始录音」→ 等 2.5 秒 → 点「结束录音」→ 等识别返回，逐项核对页面上的状态与消息。

为什么假麦克风也值得跑：
  它验证的是一条**除了"词汇准确率"以外的全部链路** ——
  麦克风权限、AudioContext 采集、降采样到 16k、WAV 封装、上传、服务端调用识别进程、
  错误分类回显、四状态切换。这些环节任何一处坏了，真实说话也照样失败。

它证明不了的：
  "说『查看上次数据』能否被正确识别成这几个字" —— 那需要真人说话，
  见 docs/design/14 的验收记录"真实语音"一节。

用法：python tools/browser_test_voice.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import browser_test as bt  # noqa: E402
import browser_test_nl as btnl  # noqa: E402  （复用它里面的真鼠标点击）


def js(ws, expr):
    return bt.js(ws, expr)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    no_mic = "--no-mic" in sys.argv
    base = "http://127.0.0.1:8000/"
    browser = bt.find_browser()
    profile = tempfile.mkdtemp(prefix="voice_")
    port = 9488 if not no_mic else 9489
    args = [browser, "--headless=new", f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}", "--no-first-run", "--disable-gpu",
            "--hide-scrollbars", "--window-size=1280,1000"]
    if no_mic:
        # 不给假麦克风：无头浏览器此时 enumerates 不到任何输入设备，
        # getUserMedia 抛 NotFoundError —— 正好用来验证"错误的说法对不对"。
        args.append("--deny-permission-prompts")
    else:
        args += ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"]
    args += ["--autoplay-policy=no-user-gesture-required", base + "#nl"]
    p = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ok_n = bad_n = 0

    def check(name, cond, detail=""):
        nonlocal ok_n, bad_n
        if cond:
            ok_n += 1
            print(f"  ✓ {name}")
        else:
            bad_n += 1
            print(f"  ✗ {name}  {detail}")

    try:
        bt.wait_devtools(port)
        t = None
        for _ in range(60):
            t = bt.find_page_target(port, "127.0.0.1")
            if t:
                break
            time.sleep(0.3)
        ws = bt.WS(t["webSocketDebuggerUrl"])
        ws.call("Page.enable")
        ws.call("Runtime.enable")
        time.sleep(6)

        check("语言分区已切换", js(ws, "document.body.getAttribute('data-tab')") == "nl",
              js(ws, "document.body.getAttribute('data-tab')"))
        check("录音按钮存在", js(ws, "!!document.getElementById('nlMic')"))
        check("播报开关存在", js(ws, "!!document.getElementById('nlSpeakOn')"))
        check("状态标签可见", js(ws, """(()=>{const e=document.getElementById('nlVoiceState');
            if(!e)return false; const r=e.getBoundingClientRect(); return r.width>0&&r.height>0;})()"""))
        check("浏览器支持 speechSynthesis", js(ws, "'speechSynthesis' in window") is True)

        if no_mic:
            js(ws, "document.getElementById('nlMic').click(); 1")
            time.sleep(1.5)
            msg = js(ws, "document.getElementById('nlVoiceState').innerText") or ""
            print("    无麦克风时的提示:", msg)
            check("无麦克风：提示说清了原因", "没有可用的麦克风" in msg, msg)
            check("无麦克风：给出了可走的路", "选录音文件" in msg, msg)
            check("无麦克风：按钮没有卡在「结束录音」",
                  js(ws, "document.getElementById('nlMic').innerText").strip() == "开始录音",
                  js(ws, "document.getElementById('nlMic').innerText"))

            # —— 语音刚失败过，文字入口必须照样能用（任务卡「当堂验证」点名的）——
            # 这一条防的是"语音坏了把整页拖死"：按钮 disabled 没复位、
            # 或者 busy 卡住，都会让文字也发不出去，但页面上看不出来。
            js(ws, "document.getElementById('nlInput').value = '查看上次数据'; 1")
            rc = btnl.click_real(ws, "#nlSend")
            check("文字入口：发送按钮可见且点得到", rc.get("clicked"), str(rc))
            got = ""
            deadline = time.time() + 90
            while time.time() < deadline:
                got = js(ws, "document.body.innerText") or ""
                if ("已读取" in got) or ("请求失败" in got) or ("已拒绝" in got):
                    break
                time.sleep(1)
            check("文字入口：语音失败后仍能拿到回答", "已读取" in got,
                  (got[-200:].replace("\n", " ") if got else "页面没有文字"))
            check("文字入口：按钮没有一直禁用",
                  js(ws, "document.getElementById('nlSend').disabled") is False,
                  js(ws, "document.getElementById('nlSend').disabled"))
            ws.close()
            print()
            print("  共 %d 项，通过 %d，未通过 %d" % (ok_n + bad_n, ok_n, bad_n))
            return 0 if bad_n == 0 else 1

        # 点「开始录音」
        js(ws, "document.getElementById('nlMic').click(); 1")
        time.sleep(1.2)
        check("录音中：按钮文案变了",
              js(ws, "document.getElementById('nlMic').innerText").strip() == "结束录音",
              js(ws, "document.getElementById('nlMic').innerText"))
        check("录音中：状态有提示",
              "录音中" in (js(ws, "document.getElementById('nlVoiceState').innerText") or ""),
              js(ws, "document.getElementById('nlVoiceState').innerText"))
        time.sleep(2.0)

        # 点「结束录音」→ 上传 → 识别
        js(ws, "document.getElementById('nlMic').click(); 1")
        time.sleep(0.8)
        mid = js(ws, "document.getElementById('nlVoiceState').innerText") or ""
        check("结束后进入识别/结果状态",
              any(k in mid for k in ("识别", "没听到", "听出", "出错", "失败")), mid)

        # 等识别返回（最多 25 秒）
        final = ""
        for _ in range(50):
            time.sleep(0.5)
            final = js(ws, "document.getElementById('nlVoiceState').innerText") or ""
            if any(k in final for k in ("没听到", "听出", "听清了", "识别出错", "格式", "太短")):
                break
        print("    最终状态:", final)
        check("识别有明确结果（不是一直转圈）",
              any(k in final for k in ("没听到", "听出", "听清了", "识别出错", "格式", "太短")), final)
        check("按钮已复位为「开始录音」",
              js(ws, "document.getElementById('nlMic').innerText").strip() == "开始录音")

        # 假麦克风是提示音：没有词 → 期望"有声音但没听出词"
        check("提示音被判为「有声音、没听出词」",
              "没听出词" in final or "听清了" in final, final)

        check("页面无 JS 报错", js(ws, "window.__errs ? window.__errs.length : 0") in (0, None))
        ws.close()
    finally:
        p.terminate()
        try:
            p.wait(timeout=8)
        except Exception:
            pass
        shutil.rmtree(profile, ignore_errors=True)

    print()
    print("  共 %d 项，通过 %d，未通过 %d" % (ok_n + bad_n, ok_n, bad_n))
    return 0 if bad_n == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
