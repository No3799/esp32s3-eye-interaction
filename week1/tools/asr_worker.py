#!/usr/bin/env python3
"""离线中文识别 worker —— 读一个 WAV 文件，输出 JSON 到 stdout。

为什么要单独一个进程，而不是在 server.py 里 import vosk：
  · vosk 装在本项目的独立虚拟环境 `.venv-asr` 里（不进用户环境、不进仓库）；
  · 业务服务用的是另一个 Python，两者隔离，互不影响；
  · 识别崩了不会把业务服务带下去，失败也更好定位（stderr 单独拿到）。

用法：
    .venv-asr/Scripts/python.exe tools/asr_worker.py <wav 路径>

输出（stdout，单行 JSON）：
    {"ok": true, "engine": "vosk", "model": "...", "text": "查看上次数据",
     "duration_s": 1.8, "elapsed_s": 0.6}
  失败：
    {"ok": false, "error": "silence" | "audio_unreadable" | "empty_text", ...}

错误码的含义（这三件事必须分开，不能都叫"识别失败"）：
    silence          —— 音频是静音/没人说话。用户要知道"麦克风开着但没声音"
    audio_unreadable —— 文件不是合法的 16k 单声道 WAV（是采集/转码出错了）
    empty_text       —— 音频有声音，但没识别出词。可能说得太轻或太快
"""
import json
import os
import sys
import time
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR = os.path.join(HERE, "models", "vosk-model-small-cn-0.22")
SAMPLE_RATE = 16000


def out(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    sys.stdout.write("\n")
    sys.stdout.flush()


def main():
    if len(sys.argv) < 2:
        out({"ok": False, "error": "audio_unreadable", "detail": "缺少 WAV 路径参数"})
        return 2
    path = sys.argv[1]
    if not os.path.exists(path):
        out({"ok": False, "error": "audio_unreadable", "detail": "文件不存在"})
        return 2

    try:
        with wave.open(path, "rb") as w:
            ch, sw, fr, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
            frames = w.readframes(n)
    except Exception as e:
        out({"ok": False, "error": "audio_unreadable",
             "detail": "%s（要求 16kHz 单声道 16bit PCM WAV）" % e})
        return 2

    duration = (n / float(fr)) if fr else 0.0
    if ch != 1 or sw != 2 or fr != SAMPLE_RATE:
        out({"ok": False, "error": "audio_unreadable",
             "detail": "格式是 %d 声道 / %d 字节 / %d Hz，要求 1 / 2 / %d"
                       % (ch, sw, fr, SAMPLE_RATE)})
        return 2

    # 静音判定：先看样本幅度。全零或几乎全零，直接报 silence，
    # 不去麻烦模型 —— 也让"麦克风没收到声音"和"识别不出词"能分开。
    peak = 0
    if frames:
        mv = memoryview(frames)
        for i in range(0, len(mv) - 1, 2):
            v = mv[i] | (mv[i + 1] << 8)
            if v >= 32768:
                v -= 65536
            if v < 0:
                v = -v
            if v > peak:
                peak = v
    if peak < 200:
        out({"ok": False, "error": "silence", "peak": peak,
             "duration_s": round(duration, 2),
             "detail": "录音里没有声音（峰值 %d / 32768）" % peak})
        return 0

    if not os.path.isdir(MODEL_DIR):
        out({"ok": False, "error": "model_missing", "detail": MODEL_DIR})
        return 3

    t0 = time.time()
    try:
        from vosk import Model, KaldiRecognizer, SetLogLevel
        SetLogLevel(-1)
        model = Model(MODEL_DIR)
        rec = KaldiRecognizer(model, SAMPLE_RATE)
        rec.SetWords(False)
        for i in range(0, len(frames), 8000):
            rec.AcceptWaveform(frames[i:i + 8000])
        res = json.loads(rec.FinalResult())
        text = (res.get("text") or "").replace(" ", "").strip()
    except Exception as e:
        out({"ok": False, "error": "asr_error", "detail": str(e)})
        return 4

    out({"ok": bool(text), "engine": "vosk", "model": os.path.basename(MODEL_DIR),
         "text": text, "duration_s": round(duration, 2),
         "elapsed_s": round(time.time() - t0, 2),
         "error": None if text else "empty_text"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
