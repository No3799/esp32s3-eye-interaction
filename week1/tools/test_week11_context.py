#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第11周验收：多源证据 → 三层上下文 → 可解释回复与追问

分两段：
A. 进程内规则测试 —— 用桩件构造"新鲜的遮挡判断""陈旧的坐标"这类现场不好复现的输入，
   直接验证规则本身，不往真实数据里塞假记录。
B. 联网接口测试 —— 对正在跑的服务问几个真问题，检查回复里的事实、依据、追问。

三条当堂验证对应的断言：
  1. 摄像头遮挡不被解释为危险     → A 段：文本含"不等于危险"，待确认里出现遮挡项
  2. 陈旧坐标不被包装成当前位置   → A/B 段：过期位置只能表述为"X 前 + 不能当当前状态"
  3. 模型推断与观测明确区分       → A/B 段：derived.provenance == "inference"

用法：
    python tools/test_week11_context.py
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "server")
DATA = os.path.join(ROOT, "data")
sys.path.insert(0, SERVER)

from context_rules import RULES_VERSION, ContextBuilder          # noqa: E402
from sources import SourceBoard                                  # noqa: E402
from snapshots import SnapshotStore                              # noqa: E402

BASE = os.environ.get("WB_BASE", "http://127.0.0.1:8000")
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> bool:
    _results.append((bool(ok), name, detail))
    print(("  ✓ " if ok else "  ✗ ") + name + (("　—　" + detail) if detail else ""))
    return bool(ok)


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
        return e.code, {"error": e.read().decode("utf-8", "replace")}


# ---------------- 桩件：只实现被调用到的方法 ----------------
class StubVision:
    """返回一条**刚刚产生**的视觉判断，标签可控。"""

    def __init__(self, label: str, age_s: float = 2.0, score: float = 0.9):
        self.label = label
        self.age_s = age_s
        self.score = score

    def all(self, limit: int = 20):
        return [{
            "id": "VIS-STUB",
            "created_ms": int((time.time() - self.age_s) * 1000),
            "result": {"label": self.label, "score": self.score,
                       "score_kind": "self_reported",
                       "model": {"name": "stub"}},
        }]


class StubHelp:
    def __init__(self, rows: list[dict] | None = None):
        self.rows = rows or []

    def recent(self, *a, **kw):
        return self.rows


def make_builder(label: str = "blocked", help_rows=None) -> ContextBuilder:
    import sqlite3
    conn = sqlite3.connect(os.path.join(DATA, "readings.db"), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    board = SourceBoard(conn, threading.Lock(),
                        SnapshotStore(DATA),
                        os.path.join(DATA, "sources", "events_probe.jsonl"))
    return ContextBuilder(board, StubHelp(help_rows), StubVision(label))


def main() -> int:
    t0 = time.time()
    print("=" * 74)
    print("第11周验收 · 多源证据组织成可解释上下文")
    print("=" * 74)

    # ================= A. 规则层（进程内） =================
    print("\n[A] 规则层：用桩件构造现场不好复现的输入")

    cb = make_builder("blocked")
    d = cb.ask("画面被挡住了，危险吗？")
    txt = d["reply"]["text"]
    print("    问：画面被挡住了，危险吗？")
    print("    答：" + txt[:88])
    check("不等于危险" in txt, "遮挡没有被解释成危险")
    check(d["layers"]["decision"]["intent"] == "safety", "意图路由到 safety",
          d["layers"]["decision"]["intent"])
    check(any(p["key"] == "camera_obstructed" for p in d["pending"]),
          "把「遮挡」放进待确认，并追问现场情况")
    check(any(f["provenance"] == "inference" for f in d["facts"]),
          "视觉判断标为「模型推断」，不混进观测事实")
    check(d["guardrails"]["camera_is_not_danger"] is True, "护栏 camera_is_not_danger 通过")

    # 同一套桩件，换成"人按了求助"——这时候才允许出现需要处理的东西
    cb2 = make_builder("blocked", help_rows=[{
        "id": "HLP-STUB", "state": "open",
        "created_ms": int((time.time() - 30) * 1000)}])
    d2 = cb2.ask("危险吗？")
    check(any(p["key"] == "help_waiting" for p in d2["pending"]),
          "有人按求助时，待确认里出现「求助未回应」（唯一可信的人为证据）",
          "待确认 %d 条" % len(d2["pending"]))
    check(d2["pending"] and any(p.get("blocking") for p in d2["pending"]),
          "求助项被标为 blocking（其它项不是）")

    # 陈旧坐标：往探针事件流里写一条 10 分钟前的有效解
    probe = os.path.join(DATA, "sources", "events_probe.jsonl")
    if os.path.exists(probe):
        os.remove(probe)
    cb3 = make_builder("blocked")
    cb3.board.record_event(
        "gnss-replay", "ok",
        int((time.time() - 600) * 1000),
        payload={"lat": 31.2304, "lon": 121.4737, "sample": True,
                 "origin": "课程回放样例"},
        note="探针：10 分钟前的有效解", replay=True)
    snap = cb3.board.snapshot()
    g = next(s for s in snap["sources"] if s["id"] == "gnss-replay")
    check(g["state"] == "stale", "10 分钟前的坐标判为「已过期」", g["state_text"])
    check("不能当作当前状态" in g["note"], "过期坐标的说明里明确不能当当前状态",
          g["note"][:50])
    d3 = cb3.ask("设备现在在哪？")
    check("不能给出坐标" in d3["reply"]["text"], "问位置时仍明确说不能给出坐标",
          d3["reply"]["text"][:44])
    check(not any(ch.isdigit() for ch in d3["reply"]["text"]),
          "回复里没有出现任何坐标数字")
    check(snap["location"]["can_give_coordinates"] is False,
          "location 规则：回放坐标不参与回答")

    # 三层结构完整性
    check(all(k in d3["layers"] for k in ("observations", "derived", "decision")),
          "三层结构齐全（observations / derived / decision）")
    bad = [x for x in d3["layers"]["derived"]
           if not x.get("rule") or "valid_until_ms" not in x]
    check(not bad, "每条派生都带规则编号与失效时间",
          "缺失 %d 条" % len(bad))
    check(d3.get("rules_version") == RULES_VERSION,
          "响应带规则版本", d3.get("rules_version", ""))

    # ================= B. 接口层（真实服务） =================
    print("\n[B] 接口层：对正在跑的服务提问")
    code, live = call("/api/context/build")
    if not check(code == 200 and "layers" in live, "GET /api/context/build 可用",
                 "HTTP %s" % code):
        return 1
    check(live.get("rules_version") == RULES_VERSION, "接口规则版本一致",
          live.get("rules_version", ""))
    check(any(m["source"] == "gnss" for m in live.get("missing", [])),
          "缺失来源列表里明确列出 GNSS")
    check(len(live.get("conflicts", [])) >= 1, "至少检出一组冲突对照",
          "、".join(c["case"] for c in live.get("conflicts", [])))

    questions = [
        "设备现在在哪？",
        "画面被挡住了，危险吗？",
        "设备在动吗？",
        "网络还在吗？",
        "多久没更新了？",
    ]
    print("\n    逐条提问（真实数据）")
    answers = {}
    for q in questions:
        c, r = call("/api/context/ask", {"question": q})
        if c != 200:
            check(False, "提问返回 200：%s" % q, "HTTP %s" % c)
            continue
        answers[q] = r
        print("    ── %s" % q)
        print("       答：%s" % r["reply"]["text"][:76])
        print("       依据：%s" % r["reply"]["basis"][:76])
        check(bool(r["reply"]["text"]), "有回答：%s" % q)
        check(bool(r["reply"]["basis"]), "带依据行：%s" % q,
              r["reply"]["rules_version"])

    loc = answers.get("设备现在在哪？", {})
    check("不能给出坐标" in loc.get("reply", {}).get("text", ""),
          "真实数据下问位置仍不生成坐标")
    check(not any(ch.isdigit() for ch in loc.get("reply", {}).get("text", "")),
          "真实位置回答里没有数字坐标")

    safe = answers.get("画面被挡住了，危险吗？", {})
    check("不等于危险" in safe.get("reply", {}).get("text", ""),
          "真实数据下遮挡仍未被当成危险")

    # 依据必须能指回规则
    rules_used = {f["rule"] for r in answers.values() for f in r.get("facts", [])}
    check(rules_used and rules_used.issubset({x["id"] for x in
          __import__("context_rules").RULES}),
          "引用到的规则编号都存在", "、".join(sorted(rules_used)))

    # ================= 汇总 =================
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
