#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第 8 周验收：视觉推理 → 可理解反馈

任务卡要证明四件事，这个脚本就按这四件事组织：

1. **接入真实模型推理** —— 每张图都真的过了一遍 qwen2.5vl，记录模型版本与 digest
2. **结构化结果解析、低分/未知反馈** —— 标签 + 分数 + 不确定标记，拒识不硬编标签
3. **重新采集和人工纠正** —— 判错了要能改成对的，改完原判还留着
4. **比较高分误判与正常样例** —— 单独把"模型很自信但判错"的挑出来

样例来源分两类，脚本会如实标出来：
- **真实**：板子按需拍到的画面（data/snapshots 里 source=device）
- **合成**：从真实画面生成的遮挡/纯色/模糊图（只为覆盖失败路径，不是真实拍摄）

用法：
    python tools/test_week8_vision.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call(path: str, body=None, method=None, timeout=180):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"))
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with OPENER.open(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")
    except Exception as e:
        return 0, {"error": f"{type(e).__name__}: {e}"}


SYNTHETIC = [
    ("debug_logs/_test_blocked.jpg", "遮挡（合成）：整块深色盖住画面"),
    ("debug_logs/_test_flat.jpg", "纯色（合成）：整张压成灰色"),
    ("debug_logs/_test_blurry.jpg", "模糊（合成）：强高斯模糊"),
]


def main():
    print("=" * 74)
    print("第 8 周验收 · 视觉推理 → 可理解反馈")
    print("=" * 74)

    code, info = call("/api/vision/info")
    m = (info or {}).get("model") or {}
    if not m.get("available"):
        print("✗ 视觉模型不可用：", m.get("error"))
        return 1
    print(f"模型: {m['name']}  {m.get('parameter_size')}  {m.get('quantization')}  "
          f"digest={m.get('digest')}")
    print(f"分数口径: {info.get('score_kind')}（不是正确答案率）")
    print(f"不确定阈值: {info.get('uncertain_below')}\n")

    # ---------- 1. 真实快照 ----------
    code, lst = call("/api/snapshot/list?limit=10")
    real = [it for it in (lst.get("snapshots") or [])
            if it.get("source") == "device" and it.get("width")]
    print(f"[1] 真实快照 {len(real)} 张，取最新一张做推理")

    # ---------- 2. 导入合成样例 ----------
    print(f"[2] 导入 {len(SYNTHETIC)} 张合成样例（covered 失败路径，非真实拍摄）")
    imported = []
    for path, note in SYNTHETIC:
        code, d = call("/api/snapshot/replay", {"file": path, "note": note,
                                                "request_id": "w8-synth"})
        if code == 200:
            imported.append((d.get("id"), note))
            print(f"    ✓ {os.path.basename(path)} -> {d.get('id')}  status={d.get('status')}")
        else:
            print(f"    ✗ {path}: {code} {d.get('error')}")

    # ---------- 3. 逐张推理 ----------
    cases = []
    if real:
        cases.append((real[0]["id"], "真实：板子拍到的画面", real[0].get("status")))
    for sid, note in imported:
        cases.append((sid, note, None))

    print(f"\n[3] 逐张推理（共 {len(cases)} 张，每张都要真的过模型）")
    results = []
    for sid, note, rule_status in cases:
        t0 = time.time()
        code, d = call("/api/vision", {"snapshot_id": sid})
        dt = time.time() - t0
        if code != 200:
            print(f"    ✗ {sid}: {code} {d.get('error')}")
            continue
        r = d["result"]
        h = d["human"]
        print(f"\n    {sid}  —— {note}")
        print(f"      规则判定: {d.get('snapshot_status')}   模型: {r['label']} "
              f"@ {r['score']}（自报）   不确定={r['uncertain']}  拒识={r['rejected']}")
        print(f"      {h['head']}  |  {h['confidence_text']}")
        if h.get("advice"):
            print(f"      建议: {h['advice']}")
        if d.get("agree_with_rule") is False:
            print(f"      ⚠ 规则与模型不一致：规则={d.get('snapshot_status')} 模型={r['label']}")
        print(f"      端到端 {dt:.1f}s（模型 {r['latency_ms']}ms）  主动询问={d['proactive']['ask']}")
        results.append({"sid": sid, "note": note, "payload": d, "wall_s": dt})
        if d["proactive"]["ask"]:
            # 这里显式回答一次"这次算了"，把第 9 周的忽略路径也走一遍
            call("/api/proactive/respond", {"action": "ignore", "snapshot_id": sid,
                                            "vision_id": d["vision_id"],
                                            "reason": "验收脚本自动忽略"})

    # ---------- 4. 人工纠正 ----------
    print("\n[4] 人工纠正：把第一张（真实快照）判错的地方改过来")
    if results:
        first = results[0]["payload"]
        vid = first["vision_id"]
        print(f"    模型原判: {first['result']['label']} @ {first['result']['score']}")
        code, d = call(f"/api/vision/{vid}/feedback",
                       {"verdict": "wrong", "corrected_label": "usable",
                        "note": "画面只是失焦，不是被遮挡"})
        if code == 200:
            v = d["vision"]
            print(f"    ✓ 已纠正为 usable；原判仍保留: "
                  f"{v['result']['label']} @ {v['result']['score']}")
            print(f"      反馈记录: {v['feedback']['verdict']} -> "
                  f"{v['feedback']['corrected_label']}")
        else:
            print(f"    ✗ 纠正失败: {code} {d.get('error')}")

    # 反例：判错但不说正确标签，必须被拒
    code, d = call(f"/api/vision/{vid}/feedback", {"verdict": "wrong"})
    print(f"    反例（判错但不说改成什么）: HTTP {code} —— "
          f"{'✓ 被拒' if code == 400 else '✗ 竟然通过了'}")

    # ---------- 5. 汇总 ----------
    code, lst = call("/api/vision/list?limit=50")
    st = lst.get("stats") or {}
    print("\n[5] 汇总")
    print(f"    总记录 {st.get('total')} 条；人工确认 {st.get('correct')} 条、"
          f"纠正 {st.get('corrected')} 条、跳过 {st.get('skipped')} 条")
    print("    被纠正的（含高分误判）:")
    for w in (st.get("wrong_scores") or []):
        print(f"      {w['id']}  {w['snapshot_id']}  模型说 {w['label']} @ {w['score']}"
              f"  → 实际 {w['corrected_label']}")
    print("\n" + "=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
