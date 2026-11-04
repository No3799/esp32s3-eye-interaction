"""第10周：多来源观测登记 —— 每个来源独立记录「观测时间」与「接收时间」。

为什么单独一个模块
------------------
第 1—9 周所有数据都来自同一块板子、同一条链路，`server_ms` 既当"什么时候采的"
又当"什么时候到的"，两者从来没分开过。一旦出现补传、离线缓存、或者第二个来源，
这两个时间就不是一回事了：**拿接收时间去冒充观测时间，会把十分钟前的位置说成现在的位置。**

这一模块只做三件事：
1. 记录每个来源的原始观测（含它自己的观测时刻，不是服务器收到的那一刻）；
2. 按来源各自的"新鲜窗口"算出状态：未尝试 / 成功 / 已过期 / 失败 / 无此来源；
3. 明确标出哪些来源本机根本没有 —— 缺了就说缺了，不拿替代品顶上。

设计口径（必须守住的三条）
--------------------------
- **观测时间 ≠ 接收时间**。二者都存，数据年龄按观测时间算。
- **"没试过" ≠ "失败" ≠ "过期"**。三种都是"没数据"，但用户该做的事完全不同。
- **回放数据必须自带标签**。`replay=True` 的来源永远不会被当作"本机当前状态"回答。
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

# 规则版本：任何一条判定口径改动，这里必须跟着改。
# 页面上会显示这个字符串 —— 出了问题能对上是哪一版规则给出的结论。
RULES_VERSION = "src-rules-1.0"

# 各来源的新鲜窗口（秒）。超过它就算"过期"，但**不是失败** ——
# 过期意味着"上次是好的，只是现在没有新数据"；失败意味着"这次试了，没成"。
FRESH_SECONDS = {
    "imu": 5.0,        # 板子 1Hz 上报，5 秒没有就是断了
    "net": 10.0,       # RSSI 随每条上报一起到，给宽一点，避免抖动时误报
    "camera": 300.0,   # 按需拍照，不是流；5 分钟前的画面仍算"有图",但页面上会写清楚多久前
    "gnss": 60.0,          # 回放用
    "gnss-replay": 60.0,
    "imu-replay": 10.0,    # 补传回放：超过 10 秒就该被当作"旧数据"
}

# 本机实际的来源清单。
# `presence` 三个值：
#   real   —— 本机有真实硬件，数据是自己采的
#   absent —— 本机没有这个硬件/传感源，永远不会有数据
#   replay —— 回放样本，用于讲时序，**不代表本机状态**
SOURCES = [
    {"id": "imu", "label": "板载 IMU · QMA6100P", "kind": "惯性",
     "presence": "real", "unit": "g"},
    {"id": "net", "label": "WiFi 链路 · RSSI", "kind": "网络",
     "presence": "real", "unit": "dBm"},
    {"id": "camera", "label": "摄像头 · OV2640", "kind": "影像",
     "presence": "real", "unit": ""},
    {"id": "gnss", "label": "定位 · GNSS", "kind": "定位",
     "presence": "absent", "unit": ""},
    {"id": "gnss-replay", "label": "定位 · 课程回放样例", "kind": "定位",
     "presence": "replay", "unit": ""},
    {"id": "imu-replay", "label": "IMU · 补传回放样例", "kind": "惯性",
     "presence": "replay", "unit": "g"},
]

STATE_TEXT = {
    "not_attempted": "未尝试",
    "ok": "成功",
    "stale": "已过期",
    "failed": "失败",
    "absent": "无此来源",
}


def _now_ms() -> int:
    return int(time.time() * 1000)


class SourceBoard:
    """把三处已有的真实数据 + 一处事件流，统一成"每个来源一条状态"。"""

    def __init__(self, conn, lock: threading.Lock, snapshot_store, event_path: str):
        self.conn = conn
        self.lock = lock
        self.snapshots = snapshot_store
        self.event_path = event_path
        self._mem_lock = threading.Lock()
        os.makedirs(os.path.dirname(event_path), exist_ok=True)

    # ---------- 事件流（GNSS 回放 / 摄像头失败等"推进来"的事件） ----------
    def record_event(self, source: str, state: str, observed_at_ms: int,
                     payload: dict | None = None, note: str = "",
                     replay: bool = True) -> dict:
        rec = {
            "source": source,
            "state": state,
            "observed_at_ms": int(observed_at_ms),
            "received_at_ms": _now_ms(),
            "payload": payload or {},
            "note": note,
            "replay": bool(replay),
        }
        with self._mem_lock:
            with open(self.event_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec

    def _events(self, source: str, limit: int = 400) -> list[dict]:
        out: list[dict] = []
        try:
            with open(self.event_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue          # 半行（写到一半断电）直接跳过，不让一行坏数据拖死整页
                    if rec.get("source") == source:
                        out.append(rec)
        except OSError:
            return []
        return out[-limit:]

    def events(self, source: str, limit: int = 80) -> list[dict]:
        """给上层（第11周上下文）看事件历史用。"""
        return self._events(source, limit=limit)

    # ---------- 时钟锚点 ----------
    def _device_anchor(self, device_id: str | None) -> dict | None:
        """用「服务器收到时刻 − 设备自身毫秒」的最小值当作时钟锚点。

        为什么取最小值：网络只会让数据变晚，不会让它提前。所以观测到的
        `server_ms − device_ms` 里最小的那个，最接近"这块板子的钟比服务器慢多少"，
        其余比它大的差值就是这一条在路上/缓存里耽搁的时间。
        这不是精确对时，但足以把"补传的旧数据"和"刚采的新数据"分开 —— 而这就够了。
        """
        with self.lock:
            if not device_id:
                # 不指定设备时取"最后一条记录是哪台"，避免把模拟器和真机混在一起算钟差
                row = self.conn.execute(
                    "SELECT device_id FROM readings ORDER BY id DESC LIMIT 1").fetchone()
                device_id = row["device_id"] if row else None
            if not device_id:
                return None
            rows = self.conn.execute(
                "SELECT device_ms, server_ms FROM readings WHERE device_id = ? "
                "ORDER BY id DESC LIMIT 300", (device_id,)).fetchall()
            newest = self.conn.execute(
                "SELECT * FROM readings WHERE device_id = ? "
                "ORDER BY id DESC LIMIT 1", (device_id,)).fetchone()
        if not rows or newest is None:
            return None
        skews = [(r["server_ms"] - r["device_ms"]) for r in rows
                 if (r["device_ms"] or 0) > 0 and (r["server_ms"] or 0) > 0]
        if not skews:
            return None
        # ⚠️ 只取最小钟差，**数值仍用最新那一条**。
        # 早先把"钟差最小的那条记录"直接当返回值，结果 age 算成了 20 多秒前 ——
        # 钟差要的是窗口内的最小值，取值要的是最新值，这是两件事。
        return {"skew": min(skews), "record": dict(newest),
                "device_id": device_id}

    def _imu_source(self, now: int) -> dict:
        anchor = self._device_anchor(None)
        if not anchor:
            return self._mk("imu", now, state="not_attempted",
                            note="还没有任何一条上报，不能算失败")
        rec = anchor["record"]
        observed = (rec.get("device_ms") or 0) + anchor["skew"]
        latency = max(0, (rec.get("server_ms") or 0) - observed)
        age = (now - observed) / 1000.0
        state = "ok" if age <= FRESH_SECONDS["imu"] else "stale"
        note = ("正常上报中" if state == "ok"
                else "最近一条是 %.0f 秒前的：设备停了或断网了，不是数据坏了" % age)
        if latency > 3000:
            note = ("这批数据比它自己的观测时间晚了 %.1f 秒才到，"
                    "按时延看属于补传/缓存，不是刚刚采的" % (latency / 1000.0))
        return self._mk("imu", now, state=state, observed_at=observed,
                        received_at=rec.get("server_ms"), note=note,
                        payload={"record_id": rec.get("id"),
                                 "ax": rec.get("ax"), "ay": rec.get("ay"),
                                 "az": rec.get("az"),
                                 "amag": round(((rec.get("ax") or 0) ** 2 +
                                                (rec.get("ay") or 0) ** 2 +
                                                (rec.get("az") or 0) ** 2) ** 0.5, 4),
                                 "device_id": rec.get("device_id")})

    def _net_source(self, now: int) -> dict:
        anchor = self._device_anchor(None)
        if not anchor:
            return self._mk("net", now, state="not_attempted",
                            note="没有上报就无从判断链路")
        rec = anchor["record"]
        observed = (rec.get("device_ms") or 0) + anchor["skew"]
        age = (now - observed) / 1000.0
        rssi = rec.get("rssi")
        if rssi is None:
            return self._mk("net", now, state="failed", observed_at=observed,
                            received_at=rec.get("server_ms"),
                            note="这条上报里没有信号强度字段，链路质量无证据")
        state = "ok" if age <= FRESH_SECONDS["net"] else "stale"
        return self._mk("net", now, state=state, observed_at=observed,
                        received_at=rec.get("server_ms"),
                        note="链路在线，信号强度取自这条上报",
                        payload={"rssi": rssi, "record_id": rec.get("id")})

    def _camera_source(self, now: int) -> dict:
        try:
            items = self.snapshots.all(limit=5)
            real = self.snapshots.last_ok()      # 只算设备真实采集，回放导入不算
        except Exception:
            items, real = [], None
        if not items:
            return self._mk("camera", now, state="not_attempted",
                            note="按需拍照，还没有人点过")
        latest = items[0]
        if latest.get("status") and latest.get("status") != "ok":
            return self._mk("camera", now, state="failed",
                            observed_at=latest.get("captured_ms"),
                            received_at=latest.get("captured_ms"),
                            note="最近一次取图失败（%s），上一次成功的图不能当这次的"
                                 % latest.get("status"),
                            payload={"snapshot_id": latest.get("id"),
                                     "last_status": latest.get("status")})
        if real is None:
            # 只有回放导入、没有设备真实采集：这条来源等于没数据
            return self._mk("camera", now, state="not_attempted",
                            note="只有回放导入的图，设备本身还没采过")
        observed = int(real.get("captured_ms") or 0)
        if not observed:
            return self._mk("camera", now, state="stale",
                            note="有记录但没有采集时间，无法判断新旧")
        age = (now - observed) / 1000.0
        state = "ok" if age <= FRESH_SECONDS["camera"] else "stale"
        note = "设备最近一张真实画面"
        if age > FRESH_SECONDS["camera"]:
            note = ("设备最近一张真实画面是 %.0f 分钟前的，代表那一刻，不代表现在"
                    % (age / 60.0))
        if latest.get("source") == "replay":
            note += "；更晚还有一条回放导入，不代表设备"
        return self._mk("camera", now, state=state, observed_at=observed,
                        received_at=observed, note=note,
                        payload={"snapshot_id": real.get("id"),
                                 "width": real.get("width"),
                                 "height": real.get("height")})

    def _event_source(self, now: int, source_id: str) -> dict:
        evs = self._events(source_id)
        if not evs:
            return self._mk(source_id, now, state="not_attempted",
                            note="还没有收到过定位尝试")
        last = evs[-1]
        observed = last["observed_at_ms"]
        age = (now - observed) / 1000.0
        is_gnss = source_id.startswith("gnss")
        thing = "位置" if is_gnss else "数据"
        if last["state"] == "no_fix":
            # 关键口径：无有效解是**这次定位没成**，不是设备故障。
            return self._mk(source_id, now, state="failed", observed_at=observed,
                            received_at=last["received_at_ms"],
                            note="这次定位没有有效解（no_fix）—— 是本次观测没成，"
                                 "不是接收机坏了",
                            payload={"fix": False, **last.get("payload", {})},
                            replay=last.get("replay", True),
                            raw_state="no_fix")
        if last["state"] == "ok":
            win = FRESH_SECONDS.get(source_id) or 60.0
            state = "ok" if age <= win else "stale"
            note = "有有效解" if is_gnss else "回放批次里最新一条"
            if state == "stale":
                note = ("这条%s是 %.0f 秒前的，已经过期，不能当作当前状态"
                        % (thing, age))
            elif last.get("replay"):
                note += "（回放，不代表设备当前状态）"
            return self._mk(source_id, now, state=state, observed_at=observed,
                            received_at=last["received_at_ms"], note=note,
                            payload=last.get("payload", {}),
                            replay=last.get("replay", True))
        return self._mk(source_id, now, state="failed", observed_at=observed,
                        note=last.get("note", "定位尝试失败"), replay=True)

    # ---------- 组装 ----------
    def _mk(self, source_id: str, now: int, *, state: str,
            observed_at: int | None = None, received_at: int | None = None,
            note: str = "", payload: dict | None = None,
            replay: bool | None = None, raw_state: str | None = None) -> dict:
        spec = next(s for s in SOURCES if s["id"] == source_id)
        age = (now - observed_at) / 1000.0 if observed_at else None
        return {
            "id": source_id,
            "label": spec["label"],
            "kind": spec["kind"],
            "presence": spec["presence"],
            "state": state,
            "state_text": STATE_TEXT[state],
            "raw_state": raw_state or state,
            "observed_at_ms": observed_at,
            "received_at_ms": received_at,
            "age_seconds": round(age, 1) if age is not None else None,
            "fresh_seconds": FRESH_SECONDS.get(source_id),
            "note": note,
            "payload": payload or {},
            "replay": bool(replay) if replay is not None else (spec["presence"] == "replay"),
        }

    def snapshot(self, device_id: str | None = None) -> dict:
        now = _now_ms()
        sources = [
            self._imu_source(now),
            self._net_source(now),
            self._camera_source(now),
            self._event_source(now, "gnss"),
            self._event_source(now, "gnss-replay"),
            self._event_source(now, "imu-replay"),
        ]
        # 本机没有的硬件，状态固定为"无此来源"，不参与任何"当前状态"推断
        for s in sources:
            if s["presence"] == "absent" and s["state"] == "not_attempted":
                s["state"] = "absent"
                s["state_text"] = STATE_TEXT["absent"]
                s["note"] = ("本机没有 GNSS 接收机，这条来源不会有数据。"
                             "缺就写缺，不拿别的来源顶替")
        present = [s for s in sources if s["presence"] == "real"]
        return {
            "now_ms": now,
            "rules_version": RULES_VERSION,
            "device_id": device_id,
            "sources": sources,
            "fresh_count": sum(1 for s in present if s["state"] == "ok"),
            "present_count": len(present),
            "location": self._location_view(sources),
        }

    def _location_view(self, sources: list[dict]) -> dict:
        """位置这一项单独说清楚 —— 任务卡两次强调"无证据时不补造位置"。

        规则：只有「本机真实存在 + 有有效解 + 未过期」才能回答位置。
        回放样例哪怕有坐标、哪怕没过期，也永远不进这个答案，只用于讲时序。
        """
        real_gnss = next(s for s in sources if s["id"] == "gnss")
        if real_gnss["presence"] == "absent":
            return {
                "answerable": False,
                "reason": "本机没有定位硬件（无 GNSS 接收机），没有位置证据",
                "can_give_coordinates": False,
                "text": "没有定位数据源，不能给出坐标",
            }
        if real_gnss["state"] != "ok":
            return {
                "answerable": False,
                "reason": "定位来源状态为「%s」" % real_gnss["state_text"],
                "can_give_coordinates": False,
                "text": "这次没有有效位置，不能说设备在哪",
            }
        return {"answerable": True, "reason": "本机定位有效解且未过期",
                "can_give_coordinates": True, "text": "有有效位置"}

    # ---------- 回放：用真实历史数据演示"补传按时序解释" ----------
    def replay_backfill(self, source_id: str, count: int = 30,
                        device_id: str | None = None,
                        minutes_ago: int | None = None) -> dict:
        """把历史 readings 按它**原本的观测时间**重新灌一遍。

        这不是"设备真的补传了" —— 固件没有缓存补传能力。它演示的是：
        一批数据的观测时间在十分钟前、接收时间在现在，系统应该怎么解释它。
        因此写进事件流时 `replay=True`，页面上也会标"回放"。
        """
        sql = "SELECT * FROM readings"
        args: list[Any] = []
        if device_id:
            sql += " WHERE device_id = ?"
            args.append(device_id)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(max(1, min(count, 200)))
        with self.lock:
            rows = [dict(r) for r in self.conn.execute(sql, args).fetchall()]
        if not rows:
            return {"ok": False, "error": "没有可回放的历史数据"}
        if minutes_ago:
            # 人为把观测时间往前挪，用来构造"十分钟前的旧数据现在才到"
            shift = int(minutes_ago * 60 * 1000)
        else:
            shift = 0
        out = []
        # rows 是 DESC（新→旧）。必须**倒过来写**，让最新那条落在事件流末尾 ——
        # 否则 _event_source 取 evs[-1] 拿到的是这批里最旧的一条，
        # 数据年龄会凭空多出几十秒，看起来像"补传的这批更旧了"。
        for r in reversed(rows):
            obs = (r.get("server_ms") or _now_ms()) - shift
            rec = self.record_event(source_id, "ok", obs,
                                    payload={"record_id": r.get("id"),
                                             "from": "readings"},
                                    note="回放：原始观测时间取自 readings.server_ms",
                                    replay=True)
            out.append(rec)
        return {"ok": True, "source": source_id, "count": len(out),
                "shift_minutes": minutes_ago or 0,
                "first_observed_at_ms": out[0]["observed_at_ms"],
                "last_observed_at_ms": out[-1]["observed_at_ms"],
                "received_at_ms": out[0]["received_at_ms"],
                "replay": True,
                "note": "回放批次，不代表设备当前状态"}
