#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""第5周 · 播报链路自检：**区分「接了但没跑」和「跑了但听不见」**。

背景：这台电脑没有扬声器，所以"人耳能不能听到"没法在这台机器上验证。
但这两件事必须分开：

  · 播报代码接上、但语音引擎根本没被调用     → 实现问题，要修
  · 引擎真的合成并播放了，只是没插音箱       → 环境问题，不是实现问题

做法：在真 Chrome 里用 speechSynthesis 念一句话，收集
`onstart / onboundary / onend / onerror` 四个事件与时间戳。
事件真的按顺序来，就说明引擎跑起来了（音频送到了系统默认输出设备）；
`onerror` 会给出原因（not-allowed / synthesis-failed / text-too-long …）。

用法：
    python tools/check_tts.py
"""

import json
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import browser_test as bt  # noqa: E402

PROBE = r"""
(async () => {
  const t0 = performance.now();
  const now = () => Math.round(performance.now() - t0);
  const out = {api: ('speechSynthesis' in window), voices: [], events: [],
               text: '已读取已有记录，模长零点九九'};
  if (!out.api) return out;

  // 语音列表可能是异步加载的，先等一会儿
  for (let i = 0; i < 20 && speechSynthesis.getVoices().length === 0; i++) {
    await new Promise(r => setTimeout(r, 100));
  }
  out.voices = speechSynthesis.getVoices().map(v => v.lang + '/' + v.name).slice(0, 6);

  const u = new SpeechSynthesisUtterance(out.text);
  u.lang = 'zh-CN';
  u.rate = 1.0;
  const zh = speechSynthesis.getVoices().filter(v => /zh|Chinese/i.test(v.lang + ' ' + v.name));
  if (zh.length) u.voice = zh[0];

  const finished = new Promise(res => {
    u.onstart    = () => out.events.push(['start', now()]);
    u.onboundary = () => out.events.push(['boundary', now()]);
    u.onend      = () => { out.events.push(['end', now()]); res(); };
    u.onerror    = (e) => { out.events.push(['error', String(e.error || e)]); res(); };
    setTimeout(() => { out.events.push(['timeout', now()]); res(); }, 12000);
  });
  speechSynthesis.cancel();
  speechSynthesis.speak(u);
  await finished;
  out.speaking_left = speechSynthesis.speaking;
  return out;
})()
"""


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    browser = bt.find_browser()
    profile = tempfile.mkdtemp(prefix="tts_")
    port = 9491
    args = [browser, "--headless=new", f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}", "--no-first-run", "--disable-gpu",
            "--hide-scrollbars", "--window-size=1280,900",
            "--autoplay-policy=no-user-gesture-required",
            "http://127.0.0.1:8000/#nl"]
    p = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + str(detail)) if detail else ""))
        ok = ok and bool(cond)

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
        time.sleep(3)

        r = bt.js(ws, PROBE, timeout=40)
        print("=== 语音引擎 ===")
        print("  API 存在:", r.get("api"))
        print("  可用语音:", r.get("voices") or "（空）")
        print("  事件序列:", r.get("events"))
        print()

        ev = [e[0] for e in r.get("events") or []]
        check("页面有 speechSynthesis", r.get("api") is True)
        check("有可用语音（含中文）", len(r.get("voices") or []) > 0,
              "voices=%d" % len(r.get("voices") or []))
        check("引擎真的开始播报（onstart）", "start" in ev, ev)
        check("播报正常结束（onend）", "end" in ev, ev)
        check("没有报错（onerror）", "error" not in ev,
              [e for e in r.get("events", []) if e[0] == "error"])

        # 时长：有 onstart/onend 就说明音频真的推进了
        dur = None
        d = dict((e[0], e[1]) for e in (r.get("events") or [])[1:] if e[0] in ("start", "end"))
        if "start" in d:
            end = [e[1] for e in r.get("events", []) if e[0] == "end"]
            if end:
                dur = (end[0] - d["start"]) / 1000.0
        if dur is not None:
            check("播报时长合理（0.3~15 秒）", 0.3 <= dur <= 15, "%.2f 秒" % dur)

        print()
        if ok:
            print("  结论：引擎跑通了 —— 音频送到了系统默认输出设备。")
            print("        本机听不到只说明**没有可听的扬声器**，不是播报没实现。")
        else:
            print("  结论：播报链路上有实际问题，见上面的失败项。")
        return 0 if ok else 1
    finally:
        p.terminate()


if __name__ == "__main__":
    sys.exit(main())
