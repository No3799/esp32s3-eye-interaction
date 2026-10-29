#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第5周 · 当堂验证：错误路径必须给出**可理解的反馈**。

任务卡原文：
    「当堂验证　无声、识别失败、服务超时都产生可理解反馈；文字入口保持可用。」

这个脚本把三条路径逐一打**真实 HTTP 接口**，并且断言的是
**用户会看到的那句话**，不是函数返回了什么字段 ——
因为"返回了 error=silence"和"人看得懂"是两件事。

  ① 无声          全零 PCM          → silence         "没有声音"
  ② 识别失败      440Hz 提示音      → empty_text      "有声音，没听出词"
  ③ 格式不对      不是 WAV          → audio_unreadable "不是合法录音"
  ④ 太长          > 4MB             → too_large        "太长了"
  ⑤ 服务超时      另起一个超时阈值=0.05 秒的实例 → asr_timeout "识别超时"

第 ⑤ 条为什么要另起实例：正常识别只要 0.5~1 秒，**永远不会自然超时**。
不把阈值压到不合理的小值，这条路径一次也跑不到 —— 而它恰恰是
"服务挂了用户会看到什么"的唯一证据。阈值从环境变量 ASR_TIMEOUT_S 读，就是为了这个。

用法：
    python tools/test_asr_paths.py            # 需要 8000 上的主服务在跑
"""

import json
import math
import os
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER_DIR = os.path.join(HERE, "..", "server")
MAIN = "http://127.0.0.1:8000"
TIMEOUT_PORT = 8011
TIMEOUT_BASE = "http://127.0.0.1:%d" % TIMEOUT_PORT


def wav_silence(path, seconds=2.0, rate=16000):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return path


def wav_tone(path, seconds=1.0, rate=16000, freq=440):
    frames = b"".join(
        struct.pack("<h", int(12000 * math.sin(2 * math.pi * freq * i / rate)))
        for i in range(int(rate * seconds))
    )
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)
    return path


def post_wav(base, path_or_bytes, timeout=120):
    if isinstance(path_or_bytes, bytes):
        body = path_or_bytes
    else:
        with open(path_or_bytes, "rb") as f:
            body = f.read()
    req = urllib.request.Request(base + "/api/asr", data=body, method="POST")
    req.add_header("Content-Type", "audio/wav")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"raw": raw[:200]}
    except Exception as e:
        return 0, {"transport_error": "%s: %s" % (type(e).__name__, e)}


def check(name, cond, detail=""):
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + str(detail)) if detail else ""))
    return bool(cond)


def wait_ready(base, timeout=40):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(base + "/api/latest", timeout=3):
                return True
        except Exception:
            time.sleep(0.5)
    return False


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    tmp = tempfile.mkdtemp(prefix="asrpaths_")
    sil = wav_silence(os.path.join(tmp, "silence.wav"))
    tone = wav_tone(os.path.join(tmp, "tone.wav"))
    ok = True

    print("=== 前置：主服务是否在跑 ===")
    if not wait_ready(MAIN, 8):
        print("  [FAIL] 8000 上没有服务，先启动 week1/server/server.py")
        return 1
    print("  [PASS] 8000 可访问")

    print()
    print("=== ①②③④ 四条路径（打主服务）===")

    st, d = post_wav(MAIN, sil)
    ok &= check("① 无声 → %s（HTTP %s）" % (d.get("error"), st),
                d.get("error") == "silence", d.get("reply", ""))
    ok &= check("① 无声：回复说人话且提到没听到声音",
                bool(d.get("reply")) and ("听到声音" in d["reply"] or "没有声音" in d["reply"]),
                d.get("reply", ""))

    st, d = post_wav(MAIN, tone)
    ok &= check("② 有声音没听出词 → %s" % d.get("error"),
                d.get("error") == "empty_text", d.get("reply", ""))
    ok &= check("② 识别失败：回复给了下一步",
                bool(d.get("reply")) and ("文字" in d["reply"] or "再说" in d["reply"]),
                d.get("reply", ""))

    st, d = post_wav(MAIN, b"this is definitely not a wav file, padded to exceed 44 bytes......")
    ok &= check("③ 不是 WAV → %s（HTTP %s）" % (d.get("error"), st),
                d.get("error") == "audio_unreadable" and st == 400, d.get("reply", ""))

    big = b"RIFF" + b"\x00" * (4 * 1024 * 1024 + 1024)
    st, d = post_wav(MAIN, big, timeout=30)
    ok &= check("④ 超过 4MB → %s（HTTP %s）" % (d.get("error"), st),
                d.get("error") == "too_large" and st == 413, d.get("reply", ""))

    print()
    print("=== ⑤ 服务超时（另起一个 0.05 秒超时的实例）===")
    env = dict(os.environ)
    env["ASR_TIMEOUT_S"] = "0.05"       # 比启动识别进程还短，必超时
    py = sys.executable
    proc = subprocess.Popen(
        [py, "-u", "server.py", "--port", str(TIMEOUT_PORT), "--host", "127.0.0.1"],
        cwd=SERVER_DIR, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        if not wait_ready(TIMEOUT_BASE, 40):
            ok &= check("⑤ 超时实例已启动", False, "40 秒内没起来")
        else:
            st, d = post_wav(TIMEOUT_BASE, tone)
            ok &= check("⑤ 服务超时 → %s（HTTP %s）" % (d.get("error"), st),
                        d.get("error") == "asr_timeout" and st == 504, d.get("reply", ""))
            ok &= check("⑤ 超时：回复说明超时并给了替代路径",
                        bool(d.get("reply")) and "识别超时" in d["reply"]
                        and "文字" in d["reply"], d.get("reply", ""))
            ok &= check("⑤ 超时：耗时确实很短（没有真的等 30 秒）",
                        (d.get("elapsed_s") or 99) < 5, d.get("elapsed_s"))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    print()
    print("结果：" + ("全部通过" if ok else "有失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
