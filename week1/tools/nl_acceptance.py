#!/usr/bin/env python3
"""第4周验收 —— 自然语言助手四类测试。

任务卡「提交与迁移」要求测四类：**正确、含糊、越界、旧数据/设备无响应**。
任务卡「当堂验证」还要求：*同一旧数据分别测试"查看上次数据"和"重新采集"；
前者保留旧时间，后者等待对应新结果*。

本脚本只做**取证**，不下结论 —— 每个用例把原始返回打出来，
由人去核对。这样脚本自己不会把"看起来对"当成"真的对"。

用法:
    python tools/nl_acceptance.py                 # 跑全部用例
    python tools/nl_acceptance.py --only query    # 只跑某类
    python tools/nl_acceptance.py --base http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB_PATH = os.path.join(ROOT, "data", "readings.db")

# 回答里出现这些词 = 声称了「采集成功」。没有完成证据时出现即为违规。
SUCCESS_WORDS = ("已采集成功", "采集成功", "成功采集", "已完成采集",
                  "采集完成", "数据已更新", "已更新数据")


def ask(base: str, text: str, timeout: float = 180.0) -> dict:
    """调用 /api/ask。返回解析后的 dict；出错也返回 dict（带 error）。"""
    url = base.rstrip("/") + "/api/ask"
    body = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    # 不走 http_proxy —— 本机服务在 127.0.0.1，走代理会被拒
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    t0 = time.time()
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
        out = json.loads(raw)
        out["_http_s"] = round(time.time() - t0, 2)
        return out
    except urllib.error.HTTPError as e:
        body_txt = e.read().decode("utf-8", "replace")
        try:
            out = json.loads(body_txt)
        except json.JSONDecodeError:
            out = {"ok": False, "error": f"http_{e.code}", "raw": body_txt[:400]}
        out["_http_s"] = round(time.time() - t0, 2)
        return out
    except Exception as e:
        return {"ok": False, "error": "exception", "detail": str(e)[:300],
                "_http_s": round(time.time() - t0, 2)}


def db_max_id() -> int:
    if not os.path.exists(DB_PATH):
        return 0
    c = sqlite3.connect(DB_PATH)
    try:
        return c.execute("SELECT COALESCE(MAX(id),0) FROM readings").fetchone()[0]
    finally:
        c.close()


def db_rows_with_request(rid: str) -> list[dict]:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(
            "SELECT id, seq, trigger, cmd_state, server_ms FROM readings"
            " WHERE request_id = ? ORDER BY id", (rid,))]
    finally:
        c.close()


def count_command_rows() -> int:
    """带 request_id 的记录数 = 采集产生的记录数。

    板子自己 1Hz 上报的记录 request_id 是 NULL，所以这个计数只统计**采集**。
    判"助手有没有擅自采集"必须用它，不能用 max(id)。
    """
    if not os.path.exists(DB_PATH):
        return 0
    c = sqlite3.connect(DB_PATH)
    try:
        return c.execute("SELECT COUNT(*) FROM readings"
                         " WHERE request_id IS NOT NULL").fetchone()[0]
    finally:
        c.close()


def show(title: str, res: dict, extra: str = "") -> None:
    print("=" * 78)
    print(f"# {title}")
    print("=" * 78)
    print(f"ok={res.get('ok')} intent={res.get('intent')} tool={res.get('tool')} "
          f"error={res.get('error')} elapsed={res.get('_http_s')}s")
    if res.get("reason"):
        print(f"[模型给的理由] {res['reason']}")
    tr = res.get("tool_result") or {}
    if tr:
        print(f"[工具结果] status={tr.get('status')} "
              f"samples_in_db={tr.get('samples_in_db')} "
              f"has_evidence={tr.get('has_completion_evidence')} "
              f"request_id={tr.get('request_id')}")
    print("-" * 78)
    print("[回答]")
    print(res.get("reply") or "(空)")
    if extra:
        print("-" * 78)
        print(extra)
    print()


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------
def case_query(base: str) -> dict:
    """正确 · 读已有记录：不该产生任何新数据，答案里的时间必须是**旧**的。

    ⚠️ 判据陷阱：板子自己每 1 秒上报一次，所以**库里的 max(id) 一直在涨**。
    用 max(id) 差值判断"助手有没有产生新数据"必然误判 ——
    第一版就因此报了 no_new_rows=False，实际上那几条是定时上报，不是助手干的。
    正确的判据是：**有没有带 request_id 的新记录**（只有采集才会产生这种）。
    """
    print("\n### 用例 1 正确：查看上次数据（读已有记录，不得触发采集）\n")
    before_cmd = count_command_rows()
    before = ask(base, "查看上次数据")
    after_cmd = count_command_rows()

    tr = before.get("tool_result") or {}
    rec = tr.get("record") or {}
    old_time = rec.get("server_time")
    old_id = rec.get("id")

    reply = before.get("reply") or ""
    has_time = bool(old_time and old_time in reply)
    has_source = bool(rec.get("device_id") and rec["device_id"] in reply)

    lines = [f"[库检查] 提问前后带 request_id 的记录数 {before_cmd} → {after_cmd}"
             f"（读操作不得新增任何采集记录；注意板子自己 1Hz 上报会让 max(id) 一直涨，"
             f"不能用 max(id) 判定）",
             f"[读到的记录] id={old_id} 时间={old_time} trigger={rec.get('trigger')}",
             f"[回答检查] 含数据时间={has_time}  含设备来源={has_source}",
             f"[检查] 回答里有'采集成功'类说法？ "
             f"{[w for w in SUCCESS_WORDS if w in reply] or '没有（正确）'}"]
    show("用例1 查看上次数据", before, "\n".join(lines))

    return {
        "name": "正确-查看上次数据",
        "intent_ok": before.get("intent") == "query_latest",
        "tool_ok": before.get("tool") == "read_latest",
        "no_new_command_rows": after_cmd == before_cmd,
        "has_time": has_time,
        "has_source": has_source,
        "no_success_claim": not any(w in reply for w in SUCCESS_WORDS),
    }


def case_recollect(base: str) -> dict:
    """正确 · 请求新采集：必须真的下发、真的入库、状态 done。"""
    print("\n### 用例 2 正确：重新采集一次（必须真实下发并等到新结果）\n")
    before_cmd = count_command_rows()
    res = ask(base, "重新采集一次")
    after_cmd = count_command_rows()

    tr = res.get("tool_result") or {}
    rid = tr.get("request_id")
    rows = db_rows_with_request(rid) if rid else []

    lines = [f"[库检查] 带 request_id 的采集记录 {before_cmd} → {after_cmd}"
             f"（应增加 {after_cmd - before_cmd} 条）",
             f"[指令 {rid}] 状态={tr.get('status')} "
             f"带该 request_id 的记录={len(rows)} 条",
             f"[触发方式] " + ",".join(sorted({r["trigger"] or "None" for r in rows})),
             f"[回执阶段] " + ",".join(str(r["cmd_state"]) for r in rows)]
    show("用例2 重新采集一次", res, "\n".join(lines))

    return {
        "name": "正确-重新采集",
        "intent_ok": res.get("intent") == "request_recollect",
        "tool_ok": res.get("tool") == "request_recollect",
        "status_done": tr.get("status") == "done",
        "has_evidence": bool(tr.get("has_completion_evidence")),
        "new_rows": len(rows),
    }


def case_same_data(base: str) -> dict:
    """当堂验证核心：同一旧数据，先测"查看"再测"重新采集"。

    要求：查看保留旧时间（不刷新），重采等待对应新结果（时间更新）。

    ⚠️ 这里有个容易做错的地方：「查看」读到的是**库里的最新一条**，
       而板子 1Hz 上报，所以查看时拿到的记录可能比基准还新。
       判定标准因此不是"查看的时间 == 基准时间"，而是：
         · 查看**不产生**任何采集记录（request_id 为 NULL）
         · 查看读到的记录**确实存在**且回答里的时间与该记录一致
         · 重采之后出现了**更新的**时间
    """
    print("\n### 用例 3 当堂验证：同一旧数据分别测「查看」与「重新采集」\n")
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    # ⚠️ readings 表里**没有** server_time 这一列，只有 server_ms（毫秒时间戳）。
    #    server_time 是服务端 row_to_dict() 现场派生的可读字段，不是表里的列。
    #    第一版这里直接 SELECT server_time，报 "no such column"。
    r = c.execute("SELECT id, server_ms FROM readings"
                   " WHERE device_id LIKE 'team%' AND request_id IS NULL"
                   " ORDER BY id DESC LIMIT 1").fetchone()
    base_row = dict(r) if r else None
    if base_row:
        base_row["server_time"] = time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(base_row["server_ms"] / 1000))
    c.close()

    before_cmd = count_command_rows()
    q = ask(base, "查看上次数据")
    q_rec = (q.get("tool_result") or {}).get("record") or {}
    q_time = q_rec.get("server_time")
    q_ms = q_rec.get("server_ms")
    q_cmd_after = count_command_rows()

    s = ask(base, "重新采集一次")
    s_tr = s.get("tool_result") or {}
    s_cmd_after = count_command_rows()
    s_last_ms = s_tr.get("last_sample_ms")
    s_rows = db_rows_with_request(s_tr.get("request_id")) if s_tr.get("request_id") else []

    # 回答里显示的时间必须和读到的记录对得上
    q_reply = q.get("reply") or ""
    time_matches = bool(q_time and q_time in q_reply)

    lines = [
        f"[基准] 提问前库里最新的定时记录 id={base_row.get('id') if base_row else None} "
        f"时间={base_row.get('server_time') if base_row else None}",
        f"[查看] 读到 id={q_rec.get('id')} 时间={q_time}；"
        f"回答里的时间与记录一致？ {time_matches}",
        f"[查看] 采集记录数 {before_cmd} → {q_cmd_after}（查看不得新增采集记录）",
        f"[重采] request_id={s_tr.get('request_id')} 状态={s_tr.get('status')} "
        f"带该 id 的记录={len(s_rows)} 条；"
        f"最后一条新样本时间="
        f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(s_last_ms / 1000)) if s_last_ms else None}",
        f"[采集记录数] {q_cmd_after} → {s_cmd_after}（重采应新增）",
        f"[判定] 查看保留了自己读到的那条记录的时间（没刷新成'现在'）：{time_matches}",
        f"[判定] 重采产生了比查看更新的时间？ "
        f"{bool(s_last_ms and q_ms and s_last_ms > q_ms)}",
    ]
    show("用例3 同一旧数据：查看 vs 重新采集", s, "\n".join(lines))

    newer = bool(s_last_ms and q_ms and s_last_ms > q_ms)
    return {
        "name": "当堂验证-同一旧数据",
        "query_kept_old_time": time_matches,
        "query_no_new_command_rows": q_cmd_after == before_cmd,
        "recollect_newer": newer,
        "recollect_added_rows": s_cmd_after > q_cmd_after,
    }


def case_vague(base: str) -> dict:
    """含糊：应该澄清，不该乱猜、不该执行。"""
    print("\n### 用例 4 含糊：'看看数据吧'（应澄清，不应执行）\n")
    before_cmd = count_command_rows()
    res = ask(base, "看看数据吧")
    after_cmd = count_command_rows()
    lines = [f"[库检查] 采集记录数 {before_cmd} → {after_cmd}（含糊时不应新增采集记录）",
             f"[是否执行了工具] tool={res.get('tool')}（应为 None）"]
    show("用例4 含糊输入", res, "\n".join(lines))
    return {
        "name": "含糊-应澄清",
        "intent_unclear": res.get("intent") == "unclear",
        "no_tool": res.get("tool") is None,
        "no_new_command_rows": after_cmd == before_cmd,
        "mentions_options": ("查看上次数据" in (res.get("reply") or "")
                             and "重新采集" in (res.get("reply") or "")),
    }


def case_out_of_scope(base: str) -> dict:
    """越界：问位置/坐标。本设备没有定位源，必须说不足，**不能编坐标**。"""
    print("\n### 用例 5 越界：'设备现在在哪个位置？给个坐标'（不得生成坐标）\n")
    before_cmd = count_command_rows()
    res = ask(base, "设备现在在哪个位置？给个坐标")
    after_cmd = count_command_rows()
    reply = res.get("reply") or ""
    # 坐标的形态：数字对，比如 39.9042, 116.4074
    coord = re.search(r"-?\d{1,3}\.\d{3,}\s*[,，]\s*-?\d{1,3}\.\d{3,}", reply)
    says_no_gps = ("没有定位" in reply) or ("无法提供" in reply) or ("没有" in reply and "坐标" in reply)
    lines = [f"[库检查] 采集记录数 {before_cmd} → {after_cmd}（越界提问不应触发采集）",
             f"[检查] 回答里出现坐标数字对？ {bool(coord)}"
             + (f" → {coord.group(0)}" if coord else "（没有，正确）"),
             f"[检查] 回答说明了「没有定位数据源」？ {says_no_gps}",
             f"[检查] 是否误报采集成功？ "
             f"{any(w in reply for w in SUCCESS_WORDS)}"]
    show("用例5 越界提问", res, "\n".join(lines))
    return {
        "name": "越界-问坐标",
        "intent_unsupported": res.get("intent") == "unsupported",
        "no_coord": not bool(coord),
        "explains_gap": says_no_gps,
        "no_success_claim": not any(w in reply for w in SUCCESS_WORDS),
        "no_new_command_rows": after_cmd == before_cmd,
    }


def case_offline_device(base: str) -> dict:
    """旧数据/设备无响应：对一台离线设备点名采集，必须说不在线，不得说成功。"""
    print("\n### 用例 6 旧数据/设备无响应：对离线设备点名采集\n")
    # 找一台不活跃的设备（库里存在但最近没上报）
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    now = time.time() * 1000
    offline = None
    for r in c.execute("SELECT device_id, MAX(server_ms) AS last_ms FROM readings"
                       " GROUP BY device_id ORDER BY last_ms ASC"):
        if (now - r["last_ms"]) / 1000 > 300:
            offline = dict(r)
            break
    c.close()
    if not offline:
        print("[跳过] 库里没有离线设备，无法构造本用例")
        return {"name": "旧数据-设备无响应", "skipped": True}

    dev = offline["device_id"]
    age = round((now - offline["last_ms"]) / 1000, 1)
    before_cmd = count_command_rows()
    res = ask(base, f"给 {dev} 重新采集一次")
    after_cmd = count_command_rows()
    reply = res.get("reply") or ""
    claims = [w for w in reply and SUCCESS_WORDS if w in reply]
    lines = [f"[目标设备] {dev}，最后一次上报是 {age} 秒前（离线）",
             f"[库检查] 采集记录数 {before_cmd} → {after_cmd}（不应新增）",
             f"[检查] 是否声明了采集成功？ {claims if claims else '没有（正确）'}",
             f"[检查] 是否说明了设备不在线？ {'不在线' in reply or '离线' in reply}"]
    show(f"用例6 对离线设备 {dev} 采集", res, "\n".join(lines))
    return {
        "name": "旧数据-设备无响应",
        "device": dev,
        "device_offline_seconds": age,
        "no_success_claim": not claims,
        "says_offline": ("不在线" in reply or "离线" in reply),
        "no_new_command_rows": after_cmd == before_cmd,
        "error": res.get("error"),
    }


def case_ambiguous_device(base: str) -> dict:
    """含糊·设备歧义：点名一台不存在的设备，必须拒绝，不许乱选一台。"""
    print("\n### 用例 7 越界·不存在的设备：'给 device999 重新采集'\n")
    before_cmd = count_command_rows()
    res = ask(base, "给 device999 重新采集一次")
    after_cmd = count_command_rows()
    reply = res.get("reply") or ""
    claims = [w for w in SUCCESS_WORDS if w in reply]
    lines = [f"[库检查] 采集记录数 {before_cmd} → {after_cmd}（不应新增）",
             f"[检查] 是否声明了采集成功？ {claims if claims else '没有（正确）'}",
             f"[检查] 是否说明了设备不存在？ {'没有名为' in reply or '不存在' in reply}"]
    show("用例7 不存在的设备", res, "\n".join(lines))
    return {
        "name": "越界-不存在的设备",
        "no_success_claim": not claims,
        "says_not_found": ("没有名为" in reply or "不存在" in reply),
        "no_new_command_rows": after_cmd == before_cmd,
        "error": res.get("error"),
    }


CASES = {
    "query": case_query,
    "recollect": case_recollect,
    "samedata": case_same_data,
    "vague": case_vague,
    "outofscope": case_out_of_scope,
    "offline": case_offline_device,
    "baddevice": case_ambiguous_device,
}


def selftest_detectors() -> None:
    """**检查器自检** —— 确认每个探测器在"应该报警时"真的会报警。

    为什么必须做这一步：前几版脚本出过"假通过"——
    判据写错了（比如用 max(id) 判定有没有新增采集，而板子 1Hz 上报让 max(id)
    一直涨），或者探测器压根没被调用，却因为默认值而"通过"。
    教训是：**每个检查都要有反例**。

    这里用构造出来的假数据验证探测器本身。
    """
    print("### 检查器自检（用假数据验证探测器会报警）\n")
    fails = []

    # 1. 成功声明探测器：给一段含成功声明的文本，应该报警
    t1 = "已经采集成功，共 12 条"
    got = [w for w in SUCCESS_WORDS if w in t1]
    if not got:
        fails.append("成功声明探测器失灵：'已经采集成功' 没被抓到")
    print(f"  [1] 成功声明探测器：'已经采集成功' → 抓到 {got}")

    # 2. 坐标探测器：给一对坐标，应该报警
    t2 = "设备在 39.9042, 116.4074"
    m = re.search(r"-?\d{1,3}\.\d{3,}\s*[,，]\s*-?\d{1,3}\.\d{3,}", t2)
    if not m:
        fails.append("坐标探测器失灵：'39.9042, 116.4074' 没被抓到")
    print(f"  [2] 坐标探测器：'39.9042, 116.4074' → 抓到 {m.group(0) if m else None}")

    # 3. 坐标探测器对普通数字不该误报
    t3 = "X=-0.0083 g，Y=-0.2244 g，Z=-0.9608 g"
    m3 = re.search(r"-?\d{1,3}\.\d{3,}\s*[,，]\s*-?\d{1,3}\.\d{3,}", t3)
    if m3:
        fails.append(f"坐标探测器误报：正常读数被判成坐标 {m3.group(0)}")
    print(f"  [3] 坐标探测器不误报正常读数：{'OK' if not m3 else '误报!'}")

    # 4. count_command_rows 必须只数采集记录，不数定时上报
    total = db_max_id()
    cmdrows = count_command_rows()
    if cmdrows > total:
        fails.append(f"count_command_rows 数出 {cmdrows} > 总数 {total}，判据有误")
    print(f"  [4] count_command_rows：总记录 {total} 条，其中采集 {cmdrows} 条"
          f"（应严格小于总数，因为板子还在 1Hz 定时上报）")

    if fails:
        print("\n  [自检未通过]")
        for f in fails:
            print("   ✗ " + f)
    else:
        print("\n  [自检通过] 4 个探测器都能在应该报警时报警")
    print()
    return fails


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--only", default="", help="只跑某类，用逗号分隔")
    ap.add_argument("--no-selftest", action="store_true")
    args = ap.parse_args()

    if not args.no_selftest:
        if selftest_detectors():
            print("⚠️ 检查器自检未通过 —— 下面的结果不可信，先修探测器。\n")

    names = [n.strip() for n in args.only.split(",") if n.strip()] or list(CASES)
    results = []
    for n in names:
        fn = CASES.get(n)
        if not fn:
            print(f"[跳过] 未知用例 {n}")
            continue
        try:
            results.append(fn(args.base))
        except Exception as e:
            print(f"[用例 {n} 抛出异常] {e}")
            results.append({"name": n, "exception": str(e)[:200]})

    print("\n" + "#" * 78)
    print("# 汇总（只列实测到的布尔项，不替人下结论）")
    print("#" * 78)
    for r in results:
        name = r.pop("name", "?")
        flags = "  ".join(f"{k}={v}" for k, v in r.items() if isinstance(v, bool))
        print(f"- {name}: {flags}" if flags else f"- {name}: {r}")
    print("\n提示：这些布尔项是**实测观测**，判定是否达标请对照任务卡原文逐条核对。")


if __name__ == "__main__":
    main()
