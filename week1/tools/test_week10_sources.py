#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第10周验收：多来源观测与当前状态

判据全部是**能从接口/文件里量出来的东西**，不看"我觉得对不对"：
- 四种状态是否真的能同时出现（未尝试 / 成功 / 已过期 / 失败 / 无此来源）
- 补传的数据年龄是否按**观测时间**算（而不是按收到时间算成 0）
- GNSS 无有效解是否**没有**被写成设备故障
- 没有定位来源时，接口里到底有没有冒出坐标

用法：
    python tools/test_week10_sources.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.environ.get("WB_BASE", "http://127.0.0.1:8000")
EVENTS = os.path.join(ROOT, "data", "sources", "events.jsonl")

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_results: list[tuple[bool, str, str]] = []


def call(path: str, body: dict | None = None, timeout: float = 20.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data,
                                 method="POST" if data else "GET")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with OPENER.open(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"raw": raw}


def check(ok: bool, name: str, detail: str = "") -> bool:
    _results.append((bool(ok), name, detail))
    print(("  ✓ " if ok else "  ✗ ") + name + (("　—　" + detail) if detail else ""))
    return bool(ok)


def by_id(snap: dict) -> dict:
    return {s["id"]: s for s in snap["sources"]}


def main() -> int:
    t0 = time.time()
    print("=" * 74)
    print("第10周验收 · 多来源观测与当前状态")
    print("=" * 74)

    # ---------- 0. 基线 ----------
    code, snap = call("/api/sources")
    if not check(code == 200 and "sources" in snap, "GET /api/sources 可用",
                 "HTTP %s" % code):
        return 1
    src = by_id(snap)
    print("\n[0] 基线：当前各来源")
    for s in snap["sources"]:
        print("    %-13s %-6s %-6s age=%s" % (
            s["id"], s["presence"], s["state_text"],
            "—" if s["age_seconds"] is None else "%.1fs" % s["age_seconds"]))

    check("rules_version" in snap, "响应带规则版本", snap.get("rules_version", ""))

    # ---------- 1. 本机没有的来源必须直说 ----------
    print("\n[1] 缺失来源：本机没有 GNSS")
    gnss = src["gnss"]
    check(gnss["presence"] == "absent" and gnss["state_text"] == "无此来源",
          "GNSS 标为「无此来源」", gnss["note"])
    check(gnss["age_seconds"] is None,
          "无此来源不给数据年龄（不编一个 0 秒）")

    # ---------- 2. 真实来源：成功与过期能同时出现 ----------
    print("\n[2] 真实来源的状态")
    check(src["imu"]["state"] == "ok", "IMU 状态=成功",
          "age %.1fs" % (src["imu"]["age_seconds"] or -1))
    check(src["net"]["state"] == "ok", "网络状态=成功",
          "RSSI %s" % src["net"]["payload"].get("rssi"))
    cam = src["camera"]
    check(cam["state"] in ("stale", "ok"), "摄像头状态可计算",
          "%s，age %.0fs" % (cam["state_text"], cam["age_seconds"] or -1))
    # 这一条是本项目真实状态：网络在线、影像却是几天前的 —— 正好是任务卡要的对照
    if cam["state"] == "stale" and src["net"]["state"] == "ok":
        check(True, "「网络在线但影像未更新」在真实状态里成立",
              "链路 %s / 影像 %s" % (src["net"]["state_text"], cam["state_text"]))

    # ---------- 3. 补传：观测时间旧、接收时间新 ----------
    print("\n[3] 补传数据按原观测时间解释")
    code, rp = call("/api/sources/replay",
                    {"source": "imu-replay", "count": 30, "minutes_ago": 10})
    check(code == 200 and rp.get("count") == 30, "灌入 30 条补传样本",
          "HTTP %s" % code)
    _, snap2 = call("/api/sources")
    rs = by_id(snap2)["imu-replay"]
    age = rs["age_seconds"] or 0
    check(570 <= age <= 660, "数据年龄按观测时间算（约 600 秒）",
          "实测 %.0f 秒" % age)
    check(rs["state"] == "stale", "补传的旧数据判为「已过期」", rs["state_text"])
    check("不能当作当前状态" in rs["note"], "说明里明确写了不能当当前状态",
          rs["note"][:40])

    # 同一条事件的观测/接收时间差
    if os.path.exists(EVENTS):
        last = None
        with open(EVENTS, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                if rec.get("source") == "imu-replay":
                    last = rec
        if last:
            gap = (last["received_at_ms"] - last["observed_at_ms"]) / 1000.0
            check(580 <= gap <= 680, "接收时间比观测时间晚约 10 分钟",
                  "实测 %.0f 秒" % gap)
            check(last["replay"] is True, "事件被标记为 replay，不混进真实来源")

    # ---------- 4. GNSS 无有效解 ≠ 设备故障 ----------
    print("\n[4] GNSS 无有效解（no_fix）")
    now_ms = int(time.time() * 1000)
    call("/api/sources/gnss", {"fix": True, "observed_at_ms": now_ms - 120_000})
    code, r2 = call("/api/sources/gnss", {"fix": False, "observed_at_ms": now_ms})
    check(code == 200, "写入 no_fix 事件", "HTTP %s" % code)
    _, snap3 = call("/api/sources")
    s3 = by_id(snap3)
    rep = s3["gnss-replay"]
    check(rep["state"] == "failed", "no_fix 记为「本次失败」", rep["state_text"])
    check(rep.get("raw_state") == "no_fix", "原始状态保留为 no_fix",
          str(rep.get("raw_state")))
    check("不是接收机坏了" in rep["note"] or "不是设备" in rep["note"],
          "说明里区分「本次没成」与「设备故障」", rep["note"])
    check(s3["gnss"]["state_text"] == "无此来源",
          "回放事件没有把本机 GNSS 变成「有定位」")

    # ---------- 5. 无证据不补造位置 ----------
    print("\n[5] 无证据不补造位置")
    loc = snap3["location"]
    check(loc["can_give_coordinates"] is False,
          "location.can_give_coordinates=False")
    check("不能给出坐标" in loc["text"], "回答原话是「没有定位数据源，不能给出坐标」",
          loc["text"])
    blob = json.dumps(snap3, ensure_ascii=False)
    check("31.2304" not in blob and "121.4737" not in blob,
          "来源状态响应里没有出现回放样例坐标")

    # ---------- 6. 对照案例 ----------
    print("\n[6] 两组对照案例")
    _, ctx = call("/api/context/build")
    cases = [c["case"] for c in ctx.get("conflicts", [])]
    print("    实际检出：", "、".join(cases) if cases else "（无）")
    check(any("no_fix" in c for c in cases), "检出「有旧位置，但本次 no_fix」")
    check(any("影像" in c or "位置未更新" in c for c in cases),
          "检出「网络在线但影像/位置未更新」")

    # ---------- 7. 汇总 ----------
    ok = sum(1 for r in _results if r[0])
    total = len(_results)
    print("\n" + "=" * 74)
    print("结果：%d/%d 通过，耗时 %.1f 秒" % (ok, total, time.time() - t0))
    if ok < total:
        print("未通过项：")
        for good, name, detail in _results:
            if not good:
                print("  ✗ %s　%s" % (name, detail))
    print("=" * 74)
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
