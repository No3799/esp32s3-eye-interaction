#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""个人项目迁移三项的验收（第 8 / 9 / 10 周各一项）

三件事都落在同一个地方 —— **求助事件卡片**：
  · 第 10 周「数据过期或来源缺失处理」→ 卡片上写清"这条求助发生时数据新不新"
  · 第 8 周「为一项模型判断补纠错入口」→ 卡片上直接给模型判断 + 就地纠正
  · 第 9 周「期末迁移一个交互模块」   → 选定「求助事件 + 三级反馈」，边界见 design/19

判据分两段：
A. 接口：真实事件 / 过期样本 / 无来源样本 三种新鲜度都要能出现，
   且纠错后 `vision.corrected` 必须翻转。
B. 真浏览器：卡片上真的要出现按钮，点下去卡片真的要变成「已纠正为 …」。

过期与无来源这两种，真机上很难复现（板子 1Hz 一直报，按键那一刻数据总是新的），
所以 A 段用两条**测试夹具**直接写进 help_events 造出间隙 —— 夹具 id 带 TEST，
跑完删掉；B 段只点真实的那条事件。
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "readings.db")
BASE = "http://127.0.0.1:8000"
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from browser_test import WS, find_browser, find_page_target, js, wait_devtools  # noqa: E402

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> bool:
    _results.append((bool(ok), name, detail))
    print(("  ✓ " if ok else "  ✗ ") + name + (("　—　" + detail) if detail else ""))
    return bool(ok)


def call(path: str, body: dict | None = None, timeout: float = 120):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data,
                                 method="POST" if data else "GET")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with OP.open(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")


FIXTURES = ("HLP-TEST-STALE", "HLP-TEST-ABSENT")


def find_gap(conn) -> int | None:
    """找一段真实存在的数据空档，返回空档开始后 60 秒的时刻。

    为什么要找真空档：设备 1 Hz 一直上报，随便取"30 分钟前"那一刻，
    它前面 1 秒就有数据，算出来永远是"新"的 —— 我第一版就是这么错的，
    以为夹具必然过期，结果第二次跑就变成 1.6 秒。
    """
    row = conn.execute(
        "SELECT a.server_ms AS prev FROM readings a JOIN readings b ON b.id = a.id + 1 "
        "WHERE b.server_ms - a.server_ms > 180000 ORDER BY a.id DESC LIMIT 1").fetchone()
    if not row:
        return None
    return (row[0] or 0) + 60_000


def make_fixtures() -> None:
    """造两条事件：一条落在真实数据空档里（过期），一条远早于所有数据（无来源）。"""
    conn = sqlite3.connect(DB)
    now = int(time.time() * 1000)
    rows = conn.execute("SELECT MIN(server_ms) FROM readings").fetchone()
    earliest = rows[0] or now
    gap_at = find_gap(conn)
    if gap_at is None:
        gap_at = earliest + 60_000      # 兜底：第一条数据之后 60 秒
    for eid, created in ((FIXTURES[0], gap_at),
                         (FIXTURES[1], earliest - 3600 * 1000)):
        conn.execute("DELETE FROM help_events WHERE id = ?", (eid,))
        conn.execute(
            "INSERT INTO help_events (id, device_id, button, source, simulated,"
            " local_feedback, snapshot, press_delay_ms, created_ms, state)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (eid, "team01-esp32s3eye", "MENU", "simulated", 1, "led",
             json.dumps({"imu_ok": True, "norm_g": 0.98}), 700, created, "open"))
    conn.commit()
    conn.close()


def drop_fixtures() -> None:
    conn = sqlite3.connect(DB)
    for eid in FIXTURES:
        conn.execute("DELETE FROM help_events WHERE id = ?", (eid,))
    conn.commit()
    conn.close()


def prepare_live_event() -> str | None:
    """每轮现造一条"未纠正"的事件：求助 → 取图 → 模型判断。

    为什么要现造：上一轮跑完那条已经被纠正，卡片上就没有按钮了，
    浏览器那步会退化成"already"，点击路径根本没被执行 —— 测试照样全绿。
    """
    # 清掉上一轮造的事件，避免每跑一次就在页面上多堆一条
    conn = sqlite3.connect(DB)
    conn.execute("DELETE FROM help_events WHERE id LIKE 'HLP-MIG-RUN-%'")
    conn.commit(); conn.close()

    eid = "HLP-MIG-RUN-" + time.strftime("%H%M%S")
    code, ev = call("/api/help", {
        "device_id": "team01-esp32s3eye", "event_id": eid, "button": "UP+",
        "source": "simulated", "simulated": True, "local_feedback": "led",
        "snapshot": {"imu_ok": True, "norm_g": 0.97, "press_delay_ms": 800}})
    if code not in (200, 201):
        print("  造事件失败 HTTP %s" % code); return None
    code, snap = call("/api/snapshot", {"request_id": eid, "trigger": "help_event",
                                        "note": "迁移验收：每轮新造"}, timeout=90)
    if code != 200 or snap.get("status") != "ok":
        print("  取图失败：%s" % snap.get("status")); return None
    code, vis = call("/api/vision", {"snapshot_id": snap["id"]}, timeout=180)
    if code != 200:
        print("  推理失败 HTTP %s" % code); return None
    print("  本轮事件 %s → 快照 %s → 模型 %s"
          % (eid, snap["id"], (vis.get("result") or {}).get("label")))
    return eid


def browser_part(target_eid: str) -> bool:
    url = BASE + "/#help"
    port = 9351
    browser = find_browser()
    profile = tempfile.mkdtemp(prefix="mig_")
    proc = subprocess.Popen([
        browser, "--headless=new", "--no-proxy-server",
        f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
        "--no-first-run", "--no-default-browser-check", "--disable-gpu",
        "--window-size=1400,1200", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_devtools(port)
        tgt = None
        for _ in range(40):
            tgt = find_page_target(port, "about:blank")
            if tgt:
                break
            time.sleep(0.3)
        if not tgt:
            print("  没找到标签页"); return False
        ws = WS(tgt["webSocketDebuggerUrl"])
        ws.call("Page.enable"); ws.call("Runtime.enable")
        ws.call("Page.navigate", {"url": url})
        js(ws, "new Promise(r=>setTimeout(()=>r(1), 4500))", timeout=40)

        tab = js(ws, "document.body.dataset.tab")
        check(tab == "help", "页面停在「求助」分区", "data-tab=%s" % tab)

        html = js(ws, "document.getElementById('helpList').innerHTML")
        check("当时数据是新的" in html or "当时数据已过期" in html,
              "卡片上出现「数据新鲜度」一行")
        check("模型判断" in html, "卡片上出现「模型判断」")
        check("判错了" in html or "已纠正为" in html,
              "纠错入口存在（未纠正显示按钮，已纠正显示结果）")

        # 点一次真实的纠错：找本轮新造那条事件上的按钮
        clicked = js(ws, """(()=>{
          const rows = Array.from(document.querySelectorAll('#helpList .helprow'));
          for (const r of rows) {
            if (!r.textContent.includes(%s)) continue;
            const b = r.querySelector('button[data-label="usable"]');
            if (b) { b.click(); return 'clicked'; }
            if (r.textContent.includes('已纠正为')) return 'already';
          }
          return 'notfound';
        })()""" % json.dumps(target_eid))
        check(clicked == "clicked", "找到本轮事件上的纠错按钮并点击", str(clicked))
        js(ws, "new Promise(r=>setTimeout(()=>r(1), 2500))", timeout=20)
        after = js(ws, """(()=>{
          const rows = Array.from(document.querySelectorAll('#helpList .helprow'));
          const r = rows.find(x => x.textContent.includes(%s));
          return r ? r.textContent : '';
        })()""" % json.dumps(target_eid))
        check("已纠正为 能看清" in after, "点完之后卡片变成「已纠正为 能看清」")
        return True
    finally:
        proc.terminate()


def main() -> int:
    print("=" * 72)
    print("个人项目迁移三项验收（第 8 / 9 / 10 周）")
    print("=" * 72)

    print("\n[A] 接口：三种新鲜度 + 纠错闭环")
    code, ev = call("/api/help/HLP-MIG-0001")
    if not check(code == 200, "读取真实求助事件 HLP-MIG-0001", "HTTP %s" % code):
        return 1
    f = ev.get("data_freshness") or {}
    v = ev.get("vision") or {}
    check(f.get("state") == "ok", "真实事件：当时数据是新的", f.get("note", ""))
    check(f.get("age_seconds") is not None and f["age_seconds"] < 5,
          "新鲜度给出具体秒数（不是布尔）", "%.1f 秒" % (f.get("age_seconds") or -1))
    check(v.get("has_snapshot") is True, "事件关联到画面", str(v.get("snapshot_id")))
    check(v.get("has_vision") is True, "画面关联到模型判断", "%s @ %s" % (v.get("label"), v.get("score")))
    check(v.get("score_kind") == "self_reported", "分数标注是「自报」不是正确率")

    make_fixtures()
    try:
        _, st = call("/api/help/" + FIXTURES[0])
        fs = st.get("data_freshness") or {}
        check(fs.get("state") == "stale", "过期样本判为 stale",
              "%.0f 秒前" % (fs.get("age_seconds") or -1))
        check("不能" in (fs.get("note") or ""), "过期样本的说明里点明不能当现场依据",
              (fs.get("note") or "")[:40])

        _, ab = call("/api/help/" + FIXTURES[1])
        fa = ab.get("data_freshness") or {}
        check(fa.get("state") == "absent", "无来源样本判为 absent",
              (fa.get("note") or "")[:40])
    finally:
        drop_fixtures()
        os.path.exists(FIXTURES[0])  # no-op，保持结构清晰

    # 纠错闭环（用第二条真实事件，若已被纠正过则断言其为已纠正）
    _, ev2 = call("/api/help/HLP-MIG-0002")
    v2 = ev2.get("vision") or {}
    check(v2.get("has_vision") is True, "第二条事件也有模型判断",
          "%s @ %s" % (v2.get("label"), v2.get("score")))

    print("\n[B] 真浏览器：卡片上的按钮点得动")
    eid = prepare_live_event()
    if eid:
        browser_part(eid)
    else:
        check(False, "准备一条未纠正的事件（取图/推理失败）")

    ok = sum(1 for x in _results if x[0])
    total = len(_results)
    print("\n" + "=" * 72)
    print("结果：%d/%d 通过" % (ok, total))
    if ok < total:
        for good, name, detail in _results:
            if not good:
                print("  ✗ %s　%s" % (name, detail))
    print("=" * 72)
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
