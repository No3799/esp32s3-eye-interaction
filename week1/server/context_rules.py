"""第11周：把多来源证据组织成「可解释的上下文」。

三层结构，一层都不许跳
----------------------
1. **原始观测**（observation）：某个来源在某时刻量到的具体数值。只陈述事实。
2. **派生状态**（derived）：按**有限规则 + 有效时间窗**从观测算出来的判断。
   每条派生都带 `from`（用了哪些观测）、`valid_until`（什么时候失效）、`rule`（哪条规则）。
3. **交互决策**（decision）：这一句话能回答什么、必须问什么、哪些干脆不能答。

为什么必须是三层
----------------
把"模型说画面被挡住"直接写成"可能出事了"，跳过的就是第二层到第三层的边界：
遮挡是**观测**，危险是**判断**，而判断需要的是"人有没有求助"这类证据。
中间这一层不写出来，出了问题就查不出是哪一步错的。
"""

from __future__ import annotations

import time

RULES_VERSION = "ctx-rules-1.1"

# 有效时间窗（秒）。派生状态超过窗口就失效，不许再拿它当"当前"。
WINDOWS = {
    "motion": 8.0,      # IMU 1Hz，8 秒内有数据才敢说"现在在不在动"
    "link": 10.0,
    "camera": 300.0,
    "location": 60.0,
    "help": 1800.0,     # 半小时内的求助才算"当前还没处理"
}

RULES = [
    {"id": "R1", "layer": "derived", "text": "IMU 合加速度与 1g 偏差 > 0.15g 判定为「移动中」",
     "window_s": WINDOWS["motion"], "needs": ["imu"]},
    {"id": "R2", "layer": "derived", "text": "网络来源 10 秒内有数据判定为「链路在线」",
     "window_s": WINDOWS["link"], "needs": ["net"]},
    {"id": "R3", "layer": "derived", "text": "视觉模型给出画面可用性，属「模型推断」不是观测",
     "window_s": WINDOWS["camera"], "needs": ["camera", "vision"]},
    {"id": "R4", "layer": "derived", "text": "只有本机真实 GNSS 有有效解且未过期才给位置；"
                                           "回放样例永不用于回答位置",
     "window_s": WINDOWS["location"], "needs": ["gnss"]},
    {"id": "R5", "layer": "decision", "text": "「危险」只能由人的求助动作或用户自述触发，"
                                            "不能由画面遮挡、模型标签或运动状态推出",
     "window_s": WINDOWS["help"], "needs": ["help"]},
    {"id": "R6", "layer": "decision", "text": "任何超过窗口的数据只能表述为「X 分钟前」并标待确认，"
                                            "不得改写成「现在」",
     "window_s": None, "needs": []},
]

# 意图路由：顺序有意义 —— 先安全、再位置，避免"画面危险吗"被归到画面
INTENTS = [
    ("safety", ["危险", "安全", "紧急", "出事", "报警", "要不要紧"]),
    ("help", ["求助", "按了", "求救", "有人需要", "帮忙"]),
    ("location", ["在哪", "位置", "坐标", "哪里", "定位", "地点"]),
    ("motion", ["在动", "动了", "运动", "静止", "晃"]),
    ("camera", ["画面", "摄像头", "看得见", "遮挡", "拍", "图"]),
    ("link", ["网络", "在线", "信号", "联网", "断网", "wifi"]),
    ("freshness", ["新不新", "多久", "过期", "旧", "更新", "什么时候"]),
]

STATE_WORD = {"ok": "有效", "stale": "已过期", "failed": "失败",
              "not_attempted": "未尝试", "absent": "无此来源"}


def _age(now_ms: int, ts_ms: int | None) -> float | None:
    if not ts_ms:
        return None
    return round((now_ms - ts_ms) / 1000.0, 1)


def _human_age(sec: float | None) -> str:
    if sec is None:
        return "时间未知"
    if sec < 60:
        return "%.0f 秒前" % sec
    if sec < 3600:
        return "%.0f 分钟前" % (sec / 60.0)
    return "%.1f 小时前" % (sec / 3600.0)


class ContextBuilder:
    """把 SourceBoard 的多源状态组织成三层上下文 + 可解释回复。"""

    def __init__(self, board, help_store, vision_store):
        self.board = board
        self.help = help_store
        self.vision = vision_store

    # ---------------- 第一层：原始观测 ----------------
    def _observations(self, snap: dict) -> list[dict]:
        now = snap["now_ms"]
        # 观测层要写清「量的是什么」：只写"惯性 = 0.93"读的人不知道那是加速度。
        FIELD = {
            "imu": ("合加速度", "g"),
            "net": ("信号强度", "dBm"),
            "camera": ("快照编号", ""),
            "gnss": ("坐标", ""),
            "gnss-replay": ("坐标", ""),
            "imu-replay": ("回放记录", ""),
        }
        out = []
        for s in snap["sources"]:
            if s["presence"] == "absent":
                continue                      # 无此来源不进观测层，只在"缺失"里说
            if s["state"] in ("not_attempted",):
                continue
            name, unit = FIELD.get(s["id"], (s["kind"], ""))
            value = None
            if s["id"] == "imu":
                value = s["payload"].get("amag")
            elif s["id"] == "net":
                value = s["payload"].get("rssi")
            elif s["id"] == "camera":
                value = s["payload"].get("snapshot_id")
            elif s["id"].startswith("gnss"):
                if s["payload"].get("lat") is not None:
                    value = s["payload"].get("lat")
                elif s.get("raw_state") == "no_fix":
                    value = "no_fix（本次无有效解）"
            elif s["id"] == "imu-replay":
                rid = s["payload"].get("record_id")
                value = ("#%s" % rid) if rid else "回放批次"
            out.append({
                "layer": "observation",
                "source": s["id"],
                "source_label": s["label"],
                "field": name,
                "unit": unit,
                "value": value,
                "state": s["state"],
                "observed_at_ms": s["observed_at_ms"],
                "age_seconds": s["age_seconds"],
                "age_text": _human_age(s["age_seconds"]),
                "replay": s["replay"],
            })
        return out

    # ---------------- 第二层：派生状态 ----------------
    def _derived(self, snap: dict, visions: list[dict]) -> list[dict]:
        now = snap["now_ms"]
        src = {s["id"]: s for s in snap["sources"]}
        out = []

        imu = src["imu"]
        if imu["state"] == "ok" and imu["age_seconds"] is not None \
                and imu["age_seconds"] <= WINDOWS["motion"]:
            amag = imu["payload"].get("amag")
            moving = bool(amag is not None and abs(amag - 1.0) > 0.15)
            out.append({
                "layer": "derived", "name": "motion",
                "text": "移动中" if moving else "基本静止",
                "value": moving,
                "from": ["imu"], "rule": "R1",
                "valid_until_ms": now + int(WINDOWS["motion"] * 1000),
                "evidence": "合加速度 %.3f g" % amag if amag is not None else "无合加速度",
                "provenance": "observation",
            })
        else:
            out.append({
                "layer": "derived", "name": "motion", "text": "无法判断",
                "value": None, "from": ["imu"], "rule": "R1",
                "valid_until_ms": None,
                "evidence": "IMU %s（%s）" % (STATE_WORD.get(imu["state"], imu["state"]),
                                             _human_age(imu["age_seconds"])),
                "provenance": "observation",
            })

        net = src["net"]
        out.append({
            "layer": "derived", "name": "link",
            "text": "链路在线" if net["state"] == "ok" else "链路状态不确定",
            "value": net["state"] == "ok",
            "from": ["net"], "rule": "R2",
            "valid_until_ms": now + int(WINDOWS["link"] * 1000) if net["state"] == "ok" else None,
            "evidence": ("RSSI %s dBm，%s" % (net["payload"].get("rssi"),
                                              _human_age(net["age_seconds"]))
                         if net["payload"].get("rssi") is not None
                         else "无信号强度数据"),
            "provenance": "observation",
        })

        # 视觉：明确标成 inference —— 这不是观测，是模型对观测的解释
        LABEL_WORD = {"usable": "能看清", "blocked": "被挡住", "unclear": "看不清是什么"}
        v = visions[0] if visions else None
        if v:
            res = v.get("result") or {}
            age = _age(now, v.get("created_ms"))
            label = res.get("label")
            if age is not None and age > WINDOWS["camera"]:
                # 几天前的模型判断不能当"现在画面被挡住"。这条是本周最容易踩的坑：
                # 数据本身没坏，只是过期了 —— 拿它回答现在，就是拿旧结论冒充新事实。
                out.append({
                    "layer": "derived", "name": "camera_view",
                    "text": "模型判断已过期（%s）" % _human_age(age),
                    "value": None,
                    "from": ["camera", "vision"], "rule": "R3",
                    "valid_until_ms": (v.get("created_ms") or 0) + int(WINDOWS["camera"] * 1000),
                    "evidence": "最近一次视觉推理在 %s，超出 %d 秒有效窗，不能代表现在"
                                % (_human_age(age), WINDOWS["camera"]),
                    "provenance": "inference",
                    "stale": True,
                })
            else:
                out.append({
                    "layer": "derived", "name": "camera_view",
                    "text": LABEL_WORD.get(label, "模型未给出结论"),
                    "value": label,
                    "from": ["camera", "vision"], "rule": "R3",
                    "valid_until_ms": (v.get("created_ms") or 0) + int(WINDOWS["camera"] * 1000),
                    "evidence": "模型自报 %.2f（未校准），%s" % (
                        res.get("score") or 0, _human_age(age)),
                    "provenance": "inference",
                    "model": (res.get("model") or {}).get("name")
                             if isinstance(res.get("model"), dict) else None,
                })
        else:
            out.append({
                "layer": "derived", "name": "camera_view", "text": "还没让模型看过",
                "value": None, "from": ["camera", "vision"], "rule": "R3",
                "valid_until_ms": None, "evidence": "没有视觉推理记录",
                "provenance": "inference",
            })

        loc = snap["location"]
        out.append({
            "layer": "derived", "name": "location",
            "text": "有有效位置" if loc["answerable"] else "没有可用位置",
            "value": loc["answerable"],
            "from": ["gnss"], "rule": "R4",
            "valid_until_ms": (now + int(WINDOWS["location"] * 1000)) if loc["answerable"] else None,
            "evidence": loc["reason"],
            "provenance": "observation",
        })
        return out

    # ---------------- 冲突检测（任务卡点名的两组对照） ----------------
    def _conflicts(self, snap: dict) -> list[dict]:
        src = {s["id"]: s for s in snap["sources"]}
        out = []
        net, cam = src["net"], src["camera"]
        if net["state"] == "ok" and cam["state"] in ("stale", "failed"):
            out.append({
                "case": "网络在线，但影像没更新",
                "detail": "链路 %s 有效，影像却是 %s（%s）" % (
                    "在线", cam["state_text"], _human_age(cam["age_seconds"])),
                "explain": "这两件事没有因果关系：上报走的是小包，拍照是另一条链路。"
                           "链路好不代表画面新",
            })
        # 有旧位置 + 本次 no_fix：需要看事件历史
        for sid in ("gnss", "gnss-replay"):
            evs = self.board.events(sid, limit=80)
            if not evs:
                continue
            last = evs[-1]
            fixes = [e for e in evs if e["state"] == "ok"]
            if last["state"] == "no_fix" and fixes:
                old = fixes[-1]
                out.append({
                    "case": "有旧位置，但本次 no_fix",
                    "detail": "上次有效解在 %s，最近一次定位尝试是 no_fix" % (
                        _human_age(_age(snap["now_ms"], old["observed_at_ms"]))),
                    "explain": "旧位置只能说明\"上次在哪里\"，不能填补这一次的空缺；"
                               "no_fix 是本次观测没成，不是设备坏了",
                })
                break
        return out

    # ---------------- 第三层：交互决策 ----------------
    def _pending(self, snap: dict, derived: list[dict]) -> list[dict]:
        now = snap["now_ms"]
        src = {s["id"]: s for s in snap["sources"]}
        d = {x["name"]: x for x in derived}
        out = []

        if d["camera_view"]["value"] in ("blocked", "unclear"):
            out.append({
                "key": "camera_obstructed",
                "question": "现在画面被挡住了。是有人/东西挡在前面，还是设备位置被动了？",
                "why": "画面被遮挡只能说明\"看不见\"，说明不了\"出事了\"。"
                       "要判断要不要处理，得有人确认现场",
                "evidence": d["camera_view"]["evidence"],
                "blocking": False,
            })
        if d["location"]["value"] is False:
            out.append({
                "key": "location_missing",
                "question": "需要位置的话，得先说明：本机没有定位来源，要不要改用\"哪台设备/哪个房间\"来问？",
                "why": "没有证据就不生成坐标 —— 编一个坐标比不给更糟",
                "evidence": snap["location"]["reason"],
                "blocking": False,
            })
        if src["camera"]["state"] == "stale":
            out.append({
                "key": "camera_stale",
                "question": "最近一张画面是 %s 的，要现在重拍一张吗？" % _human_age(
                    src["camera"]["age_seconds"]),
                "why": "旧画面代表那一刻，不代表现在",
                "evidence": src["camera"]["note"],
                "blocking": False,
            })
        if src["imu"]["state"] in ("stale", "not_attempted"):
            out.append({
                "key": "imu_missing",
                "question": "设备最近没有上报，它在开机吗？",
                "why": "IMU 没有新数据时，\"在不在动\"这个问题没有依据",
                "evidence": src["imu"]["note"],
                "blocking": False,
            })
        pending_help = self._pending_help(now)
        if pending_help:
            out.append({
                "key": "help_waiting",
                "question": "有一条求助还没回应（%s），要不要先处理这条？" % _human_age(
                    _age(now, pending_help.get("created_ms"))),
                "why": "人的求助动作是本系统里唯一能直接指向\"需要帮忙\"的证据",
                "evidence": "help_event %s" % pending_help.get("id"),
                "blocking": True,
            })
        return out

    def _pending_help(self, now: int) -> dict | None:
        try:
            rows = self.help.recent(limit=5)
        except Exception:
            return None
        for r in rows:
            if r.get("state") == "open":
                if (now - (r.get("created_ms") or 0)) <= WINDOWS["help"] * 1000:
                    return r
        return None

    # ---------------- 组装 ----------------
    def build(self, question: str = "") -> dict:
        snap = self.board.snapshot()
        now = snap["now_ms"]
        try:
            visions = self.vision.all(limit=1)
        except Exception:
            visions = []
        obs = self._observations(snap)
        der = self._derived(snap, visions)
        pending = self._pending(snap, der)
        conflicts = self._conflicts(snap)
        d = {x["name"]: x for x in der}
        # 缺失清单要同时包含「本机没有这个硬件」和「有硬件但还没数据」。
        # 早先只取了 presence="real"，本机的 GNSS（absent）反而没进清单 ——
        # 结果是"最该说的那句缺失"没说出来。
        missing = [{"source": s["id"], "label": s["label"], "note": s["note"]}
                   for s in snap["sources"]
                   if s["state"] in ("absent", "not_attempted")
                   and s["presence"] in ("real", "absent")]
        return {
            "rules_version": RULES_VERSION,
            "now_ms": now,
            "question": question,
            "layers": {"observations": obs, "derived": der, "decision": {}},
            "pending": pending,
            "conflicts": conflicts,
            "missing": missing,
            "summary": {
                "known": sum(1 for x in der if x["value"] is not None),
                "pending": len(pending),
                "derived": d,
            },
        }

    # ---------------- 问答：有限规则，不调模型 ----------------
    def ask(self, question: str) -> dict:
        q = (question or "").strip()
        ctx = self.build(q)
        snap = self.board.snapshot()
        srcmap = {s["id"]: s for s in snap["sources"]}
        _imu_age = srcmap["imu"]["age_seconds"]
        intent = self._route(q)
        d = ctx["summary"]["derived"]
        now = ctx["now_ms"]
        facts: list[dict] = []
        asks: list[str] = []
        text = ""

        def fact(t: str, source: str, age: float | None, rule: str,
                 provenance: str = "observation") -> None:
            facts.append({
                "text": t, "source": source, "rule": rule,
                "observed_age_seconds": age,
                "observed_text": _human_age(age),
                "provenance": provenance,
            })

        if intent == "location":
            loc = srcmap["gnss"]
            if loc["state"] == "absent" or loc["presence"] == "absent":
                text = ("没有定位数据源，不能给出坐标。本机没有 GNSS 接收机，"
                        "任何坐标都不可能是这个设备的位置。")
                fact("定位来源缺失（无此来源）", "gnss", None, "R4")
            else:
                text = "这次没有有效位置，不能回答设备在哪。"
                fact("最近一次定位尝试：%s" % loc["state_text"], "gnss",
                     loc["age_seconds"], "R4")
            asks.append("要按\"哪台设备 / 哪个房间\"来定位吗？这需要人来登记，系统自己推不出来")

        elif intent == "safety":
            text = ("画面被遮挡不等于危险。遮挡只说明现在看不到画面，"
                    "说明不了有人出事 —— 这个系统里唯一能指向\"需要帮忙\"的证据，"
                    "是人按了求助键。")
            v = d.get("camera_view") or {}
            if v.get("value"):
                fact("视觉模型判断：%s" % v.get("text"), "vision", None, "R3",
                     provenance="inference")
            if any(p["key"] == "help_waiting" for p in ctx["pending"]):
                ph = next(p for p in ctx["pending"] if p["key"] == "help_waiting")
                fact(ph["question"], "help", None, "R5")
                asks.append("先处理那条还没回应的求助")
            else:
                fact("最近没有待回应的求助事件", "help", None, "R5")
                asks.append("如果确实担心，请人去看一眼 —— 系统给不了这个结论")

        elif intent == "help":
            ph = next((p for p in ctx["pending"] if p["key"] == "help_waiting"), None)
            if ph:
                text = "有一条求助还没回应。"
                fact(ph["question"], "help", None, "R5")
            else:
                text = "最近没有待回应的求助。"
                fact("求助事件表里没有未回应的记录", "help", None, "R5")

        elif intent == "motion":
            m = d["motion"]
            if m["value"] is None:
                text = "现在判断不了在不在动：IMU 数据不新鲜。"
                fact(m["evidence"], "imu", None, "R1")
                asks.append("设备重新上报后再问一次")
            else:
                text = "按最近的 IMU 数据，设备%s。" % ("在动" if m["value"] else "基本静止")
                fact(m["evidence"] + "，判定阈值 0.15 g", "imu",
                     _imu_age, "R1")

        elif intent == "camera":
            c = d["camera_view"]
            cam = srcmap["camera"]
            if c.get("stale"):
                text = ("之前让模型看过，但那条判断已经过期，不能代表现在的画面。")
                fact(c["evidence"], "vision", cam["age_seconds"], "R3",
                     provenance="inference")
            elif c.get("value"):
                text = "模型看到的是：%s（这是模型推断，不是观测结论）。" % c["text"]
                fact(c["evidence"], "vision", cam["age_seconds"], "R3",
                     provenance="inference")
            else:
                text = "还没有让模型看过画面。"
                fact("没有视觉推理记录", "camera", cam["age_seconds"], "R3")
            if cam["state"] == "stale":
                text += "另外，最近一张图是 %s 的，代表那一刻。" % _human_age(cam["age_seconds"])
                asks.append("要现在重拍一张吗？")

        elif intent == "link":
            lk = d["link"]
            text = "链路%s。" % ("在线" if lk["value"] else "状态不确定")
            fact(lk["evidence"], "net", None, "R2")
            if not lk["value"]:
                asks.append("设备是不是没开机或没连上 WiFi？")

        elif intent == "freshness":
            rows = []
            for s in snap["sources"]:
                if s["presence"] == "absent":
                    rows.append("%s：无此来源" % s["label"])
                elif s["state"] == "not_attempted":
                    rows.append("%s：未尝试" % s["label"])
                else:
                    rows.append("%s：%s（%s）" % (s["label"], s["state_text"],
                                                 _human_age(s["age_seconds"])))
            text = "各来源新鲜度 —— " + "；".join(rows) + "。"
            for s in snap["sources"]:
                if s["presence"] != "absent" and s["state"] != "not_attempted":
                    fact("%s %s" % (s["label"], s["state_text"]), s["id"],
                         s["age_seconds"], "R6")

        else:
            text = ("我按已知事实回答：%s；%s。"
                    % (d["motion"]["text"], d["link"]["text"]))
            fact(d["motion"]["evidence"], "imu", None, "R1")
            fact(d["link"]["evidence"], "net", None, "R2")
            if ctx["missing"]:
                text += "另外%s没有数据。" % "、".join(m["label"] for m in ctx["missing"])

        # 每条回复都带依据行：来源 / 观测时间 / 规则版本
        basis = "；".join(
            ("%s（%s）" % (f["text"], f["observed_text"]))
            if f["observed_age_seconds"] is not None else f["text"]
            for f in facts[:3]
        ) if facts else "无"
        ctx["layers"]["decision"] = {
            "intent": intent,
            "answerable": True,
            "refused_coordinates": intent == "location" and "不能给出坐标" in text,
            "camera_as_danger": False,
            "facts_count": len(facts),
            "pending_count": len(ctx["pending"]),
        }
        ctx["facts"] = facts
        ctx["reply"] = {
            "text": text,
            "basis": basis,
            "rules_version": RULES_VERSION,
            "asks": asks or [p["question"] for p in ctx["pending"]
                             if p.get("blocking")][:2],
            "inference_marks": [f["text"] for f in facts if f["provenance"] == "inference"],
        }
        ctx["guardrails"] = {
            "camera_is_not_danger": "危险" not in text or intent == "safety",
            "no_coordinates_without_source": not (
                intent == "location" and any(ch.isdigit() for ch in text)
                and "不能给出坐标" not in text),
            "stale_not_presented_as_now": "现在" not in text or "现在判断不了" in text
                                          or "要现在重拍" in text or intent == "safety",
            "inference_marked": all(
                ("推断" in f["text"]) for f in facts if f["provenance"] == "inference"),
        }
        return ctx

    @staticmethod
    def _route(q: str) -> str:
        low = q.lower()
        for name, keys in INTENTS:
            if any(k in low for k in keys):
                return name
        return "general"
