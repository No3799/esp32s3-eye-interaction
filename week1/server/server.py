#!/usr/bin/env python3
"""AI 交互课 第1周 —— 最小接收与存储服务

只用 Python 标准库实现，不需要 pip install 任何东西，换台电脑拷过去就能跑。

接口:
    POST /api/data          板端上传一条传感记录
    GET  /api/latest        最新一条 + 距今年龄(秒) + 是否过期
    GET  /api/history       最近 N 条记录
    GET  /api/devices       出现过的设备列表
    GET  /api/export.csv    导出历史记录为 CSV 文件（浏览器下载）
                            可选参数：limit=条数 / hours=最近几小时 / device_id=设备
    GET  /api/camera        摄像头 MJPEG 视频流（服务器中转，详见 camera_relay.py）
    GET  /api/camera/status 中继状态（有几个观看者、帧龄、错误信息）
    GET  /                  Web 页面

第2周新增（下行指令与回执，详见 commands.py）:
    POST /api/command       下发一条指令，返回 request_id
                            type = recollect（重新采集）/ pause / resume
    GET  /api/command/<id>  查这条指令的状态与各阶段耗时（证据链）
    GET  /api/commands      最近的指令列表
    POST /api/poll          暂停期间的心跳：不写记录，只为把指令捎带回去
    —— 板子侧不需要新增端口：待执行指令随 /api/data 的**响应体**捎带下发

第3周新增（教学求助与回应/取消，详见 help.py）:
    POST /api/help              设备发起一条教学求助测试消息
    GET  /api/help              求助列表（含三级反馈：本地确认/VPS接收/查看者回应）
    POST /api/help/<id>/reply   查看者「回应」或「取消」，并回传给设备

    ⚠️ 安全边界：本服务只连本组**教学接收端**。没有任何外呼能力 ——
    不打电话、不发短信、不推任何第三方服务。界面上必须让使用者看得见这句话。

运行:
    python server/server.py --port 8000
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import queue
import socket
import socketserver
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from camera_relay import CameraRelay, CLIENT_BOUNDARY, FRAME_BOUNDARY
from commands import CommandStore
from help import HelpStore
from nl_agent import LLMClient, NLAgent

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WEB_DIR = os.path.join(ROOT, "web")
DATA_DIR = os.path.join(ROOT, "data")
DB_PATH = os.path.join(DATA_DIR, "readings.db")

# ---- 第5周：语音识别（离线，跑在独立的虚拟环境里）----
# 为什么不 import vosk：识别引擎装在 week1/.venv-asr，与本服务的 Python 隔离；
# 崩了也不会把业务服务带下去。这里只负责“把 WAV 交给它、把 JSON 拿回来”。
ASR_PY = os.path.join(ROOT, ".venv-asr", "Scripts", "python.exe")
ASR_WORKER = os.path.join(ROOT, "tools", "asr_worker.py")
AUDIO_DIR = os.path.join(ROOT, "audio")
# 单次上传上限 4MB ≈ 16kHz 单声道 2 分钟，够说一句话了；
# 超过就拒，避免有人把整个音频库传上来。
ASR_MAX_BYTES = 4 * 1024 * 1024
# 识别超时（秒）。可用环境变量覆盖 —— 否则"超时了会怎样"这条路径永远测不到：
# 正常识别只要 0.5~1 秒，不可能自然超时，只能把阈值压到比它更小来真触发一次。
ASR_TIMEOUT_S = float(os.environ.get("ASR_TIMEOUT_S", "30"))

# 第5周：设备端录音记录。只留最近 20 条在内存里 —— 它不是业务数据，
# 是"板子刚刚说了什么"的即时回显；重启丢失可以接受（原始 WAV 仍在 audio/）。
AUDIO_LOG = []
AUDIO_LOG_MAX = 20
AUDIO_LOCK = threading.Lock()

# 超过这个秒数没收到新数据，就判定为"未更新"
STALE_SECONDS = 5.0

# 单次 CSV 导出最多多少条（板子 1 秒 1 条，20 万条约等于跑满 2.3 天，够用）
EXPORT_LIMIT = 200000

# ---- 数据库容量上限 ----
# 板子一秒一条，一天就是 8.6 万条，几天下来库会一直涨。
# 超过 MAX_ROWS 就自动删掉**最早**的那批，只保留最近的记录，
# 这样长时间挂机也不会把磁盘写满。想保留更久就调大这个数。
MAX_ROWS = 200000

# 裁剪检查的节流：每插入这么多条、或每过这么多秒才去 COUNT 一次。
# 每条上报都数一遍总数太浪费，板子 1 秒 1 条时不值得。
TRIM_CHECK_EVERY = 500
TRIM_CHECK_SECONDS = 60.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT    NOT NULL,
    seq       INTEGER DEFAULT 0,
    ax REAL, ay REAL, az REAL,
    gx REAL, gy REAL, gz REAL,
    device_ms INTEGER DEFAULT 0,
    server_ms INTEGER NOT NULL,
    src_ip    TEXT
);
CREATE INDEX IF NOT EXISTS idx_readings_device ON readings(device_id, id DESC);
"""

# 后来新增的列：板子自身状态（芯片温度 / 剩余堆内存 / 历史最低堆）
# 老数据库是用 CREATE TABLE IF NOT EXISTS 建的，不会自动补列，
# 所以这里做一次幂等的 ALTER TABLE 迁移，老数据一行不丢。
EXTRA_COLUMNS = {
    "temp_c": "REAL",
    "free_heap": "INTEGER",
    "min_free_heap": "INTEGER",
    "total_heap": "INTEGER",   # 堆总大小，用于算"已用多少"
    "rssi": "INTEGER",         # WiFi 信号强度 dBm，负值，越接近 0 越好
    # ---- 第2周新增：把「这一条是被哪次请求触发的」也存下来 ----
    "request_id": "TEXT",      # 触发本次采集的批次 id（常规定时上报为 NULL）
                               #   CMD-… = 网页下发指令；BTN-… = 板载实体按键
    "trigger": "TEXT",         # 'timer' 定时上报 / 'command' 指令触发 / 'button' 实体按键
    "cmd_state": "TEXT",       # 板子回报的回执阶段：'received' / 'done' / 'failed'
    "cmd_note": "TEXT",        # 设备给的回执备注：失败原因，或"部分送达"的说明
    # ---- 第3周新增：实体按键 ----
    "button": "TEXT",          # 按的是哪个键：MENU / PLAY / UP+ / DN-（非按键触发为 NULL）
    "press_delay_ms": "INTEGER",  # 按下 → 本条采样组装完成 的毫秒数（仅按键批次的首条）
}


def migrate(conn):
    """给已存在的老库补上新列。幂等：重复执行无副作用。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(readings)")}
    for name, typ in EXTRA_COLUMNS.items():
        if name not in cols:
            conn.execute("ALTER TABLE readings ADD COLUMN %s %s" % (name, typ))
    conn.commit()


_trim_counter = 0
_trim_last_ts = 0.0


def maybe_trim(conn: sqlite3.Connection, force: bool = False):
    """记录数超过 MAX_ROWS 时，删掉最早的几条，把库压回上限以内。

    平时靠计数器/时间节流，不是每来一条就统计一次总数；
    force=True 用于启动时立刻检查一次（上次运行可能已经超了）。
    """
    global _trim_counter, _trim_last_ts
    _trim_counter += 1
    now = time.time()
    if not force and (_trim_counter < TRIM_CHECK_EVERY
                      and now - _trim_last_ts < TRIM_CHECK_SECONDS):
        return
    _trim_counter = 0
    _trim_last_ts = now

    total = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
    if total <= MAX_ROWS:
        return
    over = total - MAX_ROWS
    # 按 id 升序删最前面的 over 条（id 是自增主键，升序即最早写入的）
    conn.execute(
        "DELETE FROM readings WHERE id IN"
        " (SELECT id FROM readings ORDER BY id ASC LIMIT ?)", (over,))
    conn.commit()
    print("[trim] 记录 %d 条，超过上限 %d，已删除最早的 %d 条"
          % (total, MAX_ROWS, over))


def get_conn() -> sqlite3.Connection:
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    migrate(conn)
    return conn


CONN = get_conn()
LOCK = threading.Lock()

# ---- 每个设备的运行时状态（内存，不落库；板子重启后自然复位为"未暂停"）----
# 为什么需要它：任务卡要求"暂停周期上报、保持命令通道"，用来证明点采集后
# 产生的新数据**必然来自指令**，而不是页面翻出的旧值。
# 板子暂停后不再上报传感数据，但仍每 1 秒发一次 /api/poll 心跳 ——
# 心跳本身不写 readings，却继续把待执行指令捎带回去，所以通道不断。
DEVICE_STATE: dict[str, dict] = {}

# 允许下发的指令类型。
#   recollect —— 让设备立刻采一批新数据（第2周主任务）
#   pause / resume —— 暂停/恢复**周期上报**（任务卡要求：暂停周期上报、保持命令通道）
#   help_reply —— 把「有人回应了 / 求助已取消」回传设备，让它再给一次本地提示
#                （任务卡第3周：**迁移确认/取消机制**）
COMMAND_TYPES = {"recollect", "pause", "resume", "help_reply"}

# 给人看的名字。用于 409 拒绝时的提示文案 —— 直接抛英文类型名，
# 用户看不懂"recollect 还没走完"到底指哪个按钮。
CTYPE_LABEL = {"recollect": "重新采集", "pause": "暂停周期上报",
               "resume": "恢复周期上报", "help_reply": "求助回应回传"}


def device_state(device_id: str) -> dict:
    st = DEVICE_STATE.get(device_id)
    if st is None:
        st = {"paused": False, "last_seen_ms": 0, "last_poll_ms": 0,
              "last_reading_ms": 0, "last_kind": None}
        DEVICE_STATE[device_id] = st
    return st


# ---- 故障注入（测试专用，默认关闭，必须显式 --fault-injection 才生效）----
# 「失败态」如果永远没法被触发，它就是死代码 —— 写在状态机里好看，
# 却从没跑过，等于没有。而真要把板子弄失败（拔网线、掐服务器）既难复现、
# 又会把真机数据搅乱。所以给服务器开一个受限的注入口：
# 让接下来的 N 次「指令采集上报」返回 503，板子那边就会如实回报失败。
# 只影响带 trigger=command 且没有 cmd_state 的样本上报 ——
# 这样「设备回报失败」那条回执本身一定能送到，测试才有确定的终点。
FAULT: dict = {"mode": None, "times": 0}
FAULT_ENABLED = False          # 由 --fault-injection 打开，默认关闭

# 第2周：下行指令 + 回执状态机（实现见 commands.py）
COMMANDS = CommandStore(CONN, LOCK)

# 第3周：教学求助事件与回应/取消（实现见 help.py）
#
# ⚠️ 安全边界：只服务本组教学接收端。没有任何外呼能力 —— 不打电话、不发短信、
# 不推任何第三方服务，唯一动作是写本组数据库 + 在网页上显示。
# 界面上必须把这句话显示给使用者，免得有人误以为这是真实求助系统。
HELP = HelpStore(CONN, LOCK)


def latest_device_ip() -> str:
    """最近一条上报来自哪个 IP —— 摄像头中继靠它自动找到板子，不必写死 IP。"""
    with LOCK:
        row = CONN.execute(
            "SELECT src_ip FROM readings ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return row["src_ip"] if row and row["src_ip"] else ""


RELAY = CameraRelay(latest_ip_fn=latest_device_ip)


# ---- 第4周：自然语言助手（产品运行时模型 + 受限工具）----
# LLM 的地址/密钥只从**服务端环境变量**读（NL_LLM_BASE_URL / NL_LLM_API_KEY /
# NL_LLM_MODEL）。它们不会出现在任何 API 响应里，网页也拿不到 ——
# 密钥留在服务端是任务卡「准备与协同」明确要求的。
#
# ⚠️ 这里的分工是硬性的：模型只解析「意图+参数」，真正的读取和下发由
#    nl_agent 里的受限工具做，回答里的事实全部来自工具的真实返回。
LLM = LLMClient()
AGENT = NLAgent(LLM, CONN, LOCK, COMMANDS, DEVICE_STATE,
                stale_seconds=STALE_SECONDS)


def row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["server_time"] = time.strftime(
        "%Y-%m-%d %H:%M:%S", time.localtime(d["server_ms"] / 1000)
    )
    d["age_seconds"] = round((time.time() * 1000 - d["server_ms"]) / 1000, 2)
    d["stale"] = d["age_seconds"] > STALE_SECONDS
    return d


class Handler(BaseHTTPRequestHandler):
    server_version = "Week1Server/1.0"

    def log_message(self, fmt, *args):
        # 只打印 API 请求，静态资源太吵
        if "/api/" in (self.path or ""):
            super().log_message(fmt, *args)

    # ---------- 工具 ----------
    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code: int, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    # ---------- GET ----------
    def do_GET(self):
        url = urlparse(self.path)
        path = url.path.rstrip("/") or "/"
        qs = parse_qs(url.query)

        if path == "/api/latest":
            return self.api_latest(qs)
        if path == "/api/history":
            return self.api_history(qs)
        if path == "/api/devices":
            return self.api_devices()
        if path == "/api/export.csv":
            return self.api_export(qs)
        if path == "/api/camera":
            return self.api_camera()
        if path == "/api/camera/status":
            return self._send_json(200, RELAY.get_status())
        if path == "/api/commands":
            return self.api_commands(qs)
        if path == "/api/buttons":
            return self.api_buttons(qs)
        if path == "/api/help":
            return self.api_help_list(qs)
        if path == "/api/audio/latest":
            return self.api_audio_latest(qs)
        if path == "/api/agent/info":
            # 只读信息：告诉前端用的是哪个运行时模型。
            # 只回主机名 / 模型名 / 是否配了密钥（布尔），**不回密钥本身**。
            return self._send_json(200, {
                "llm": LLM.public_info(),
                "reply_style": AGENT.reply_style,
                "tools": ["read_latest", "request_recollect"],
                "note": "模型只解析意图与参数；读取与下发由受限工具执行。",
            })
        if path.startswith("/api/help/"):
            eid = path[len("/api/help/"):]
            ev = HELP.get(eid)
            if not ev:
                return self._send_json(404, {"error": "no such help event",
                                             "id": eid})
            return self._send_json(200, self._help_public(ev))
        if path.startswith("/api/command/"):
            cid = path[len("/api/command/"):]
            cmd = COMMANDS.get(cid)
            if not cmd:
                return self._send_json(404, {"error": "no such command", "id": cid})
            return self._send_json(200, CommandStore.to_public(cmd))
        if path in ("/", "/index.html"):
            return self.serve_file(os.path.join(WEB_DIR, "index.html"))
        return self._send_json(404, {"error": "not found", "path": path})

    def api_commands(self, qs):
        """最近的指令列表 —— 验收时用来看整条证据链。"""
        device = (qs.get("device_id") or [None])[0]
        try:
            limit = int((qs.get("limit") or [20])[0])
        except ValueError:
            limit = 20
        rows = COMMANDS.recent(device_id=device, limit=limit)
        return self._send_json(200, {
            "count": len(rows),
            "commands": [CommandStore.to_public(r) for r in rows],
        })

    def api_buttons(self, qs):
        """最近的**实体按键事件** —— 按事件 id 聚合成一条，一次按键算一条。

        为什么单开一个接口，而不是塞进 /api/commands：
        这是两条方向完全不同的路径 ——
          · 网页下发指令：服务器受理 → 下发 → 设备接收 → 完成（四段，都有服务器侧证据）
          · 板载实体按键：**没有下行通道**，所以没有前两段；起点就是设备自己
        硬塞进同一张表会让"阶段"字段一半为空，反而看不清。
        """
        device = (qs.get("device_id") or [None])[0]
        try:
            limit = int((qs.get("limit") or [10])[0])
        except ValueError:
            limit = 10
        limit = max(1, min(limit, 100))

        sql = ("SELECT request_id, button,"
               "       COUNT(*)       AS samples,"
               "       MIN(id)        AS first_id,"
               "       MAX(id)        AS last_id,"
               "       MIN(server_ms) AS first_ms,"
               "       MAX(server_ms) AS last_ms,"
               "       AVG(press_delay_ms) AS avg_press,"
               "       MAX(press_delay_ms) AS max_press"
               "  FROM readings"
               " WHERE trigger = 'button' AND request_id IS NOT NULL")
        args: list = []
        if device:
            sql += " AND device_id = ?"
            args.append(device)
        sql += " GROUP BY request_id ORDER BY last_ms DESC LIMIT ?"
        args.append(limit)

        with LOCK:
            rows = CONN.execute(sql, args).fetchall()

        now = time.time() * 1000
        events = [{
            "event_id":   r["request_id"],
            "button":     r["button"],
            "samples":    r["samples"],
            "first_id":   r["first_id"],
            "last_id":    r["last_id"],
            "first_time": time.strftime("%Y-%m-%d %H:%M:%S",
                                        time.localtime(r["first_ms"] / 1000)),
            "span_ms":    r["last_ms"] - r["first_ms"],   # 首条入库→末条入库的跨度
            "press_delay_ms": (round(r["avg_press"]) if r["avg_press"] is not None
                               else None),                 # 按下 → 首条采样组装完成
            "age_seconds": round((now - r["last_ms"]) / 1000, 2),
        } for r in rows]
        return self._send_json(200, {"count": len(events), "events": events})

    def api_issue_command(self):
        """POST /api/command —— 网页点「重新采集」走这里。

        注意与「刷新页面」的区别：刷新只是 GET 历史记录，一条新数据都不会产生；
        这里会在服务器侧**受理**一条指令，并等板子真的执行完。
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        body = {}
        if length > 0:
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw.decode("utf-8")) or {}
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                return self._send_json(400, {"error": "bad json", "detail": str(e)})

        device_id = str(body.get("device_id") or "").strip()
        if not device_id:
            return self._send_json(400, {"error": "device_id required"})
        ctype = str(body.get("type") or "recollect").strip()
        # 白名单：拼错了要当场 400，而不是受理一条板子永远不认识的指令、
        # 白等 30 秒超时。宁可在这里失败，也不要在超时后才发现是打错字。
        if ctype not in COMMAND_TYPES:
            return self._send_json(400, {
                "error": "unknown command type",
                "type": ctype,
                "allowed": sorted(COMMAND_TYPES),
            })

        # 采集类指令才需要采集参数；暂停/恢复是控制类，带参数没有意义，
        # 给了反而会让验收时误以为它也会产生新观测。
        if ctype == "recollect":
            params = {}
            for k in ("samples", "interval_ms"):
                if body.get(k) is not None:
                    try:
                        params[k] = int(body[k])
                    except (TypeError, ValueError):
                        pass
            params.setdefault("samples", 10)
            params.setdefault("interval_ms", 100)
            params["samples"] = max(1, min(params["samples"], 100))
            params["interval_ms"] = max(20, min(params["interval_ms"], 1000))
        else:
            params = {}

        # —— 闸门：同一台设备的同类指令，同一时刻只允许一条在途 ——
        # 连点 5 次不该产生 5 条排队指令：板子 1 秒只取走 1 条，最后一条会在
        # 几十秒后才执行（那时用户早走开了），前面几条还会撞上 accept 超时，
        # 页面上呈现成一片「完成 / 超时」混杂，看着像 bug。
        # 界面上的 disabled 拦不住"两个标签页同时点"，所以闸门放在服务器。
        busy = COMMANDS.active_for(device_id, ctype)
        if busy:
            busy_pub = COMMANDS.to_public(busy)
            print(f"[cmd] 拒绝重复下发：{device_id} 已有在途的 {ctype} "
                  f"指令 {busy['id']}（{busy['status']}）")
            return self._send_json(409, {
                "error": "busy",
                "existing_request_id": busy["id"],
                "existing_status": busy["status"],
                "created_at": busy_pub.get("created_at"),
                "hint": f"上一次「{CTYPE_LABEL.get(ctype, ctype)}」还没走完"
                        f"（当前 {busy['status']}），等它结束再点。",
                "detail_url": f"/api/command/{busy['id']}",
            })

        cmd = COMMANDS.issue(device_id, ctype, params)
        print(f"[cmd] 受理 {cmd['id']} device={device_id} type={ctype} "
              f"params={params}")
        return self._send_json(202, {
            "ok": True,
            "request_id": cmd["id"],
            "status": cmd["status"],
            "params": params,
            "accept_timeout_s": round(
                (cmd["accept_deadline_ms"] - cmd["created_ms"]) / 1000, 1),
            "note": "已受理。板子每 1 秒上报一次，指令会随下一次上报的响应下发。",
            "detail_url": f"/api/command/{cmd['id']}",
        })

    def api_latest(self, qs):
        device = (qs.get("device_id") or [None])[0]
        sql = "SELECT * FROM readings"
        args: list = []
        if device:
            sql += " WHERE device_id = ?"
            args.append(device)
        sql += " ORDER BY id DESC LIMIT 1"
        with LOCK:
            row = CONN.execute(sql, args).fetchone()
        if row is None:
            return self._send_json(200, {"has_data": False,
                                         "stale_seconds": STALE_SECONDS})

        # 把设备的运行模式一起带出来。少了它网页只有一个"数据年龄"可用，
        # 会把"我们主动让设备别上报"错判成"设备掉线了" —— 那是冤假错案。
        rec = row_to_dict(row)
        ds = DEVICE_STATE.get(rec["device_id"]) or {}
        now = time.time() * 1000
        return self._send_json(200, {
            "has_data": True,
            "stale_seconds": STALE_SECONDS,
            "record": rec,
            "paused": bool(ds.get("paused")),
            "device_seen_seconds": (round((now - ds["last_seen_ms"]) / 1000, 2)
                                    if ds.get("last_seen_ms") else None),
            "last_request_kind": ds.get("last_kind"),
        })

    def api_history(self, qs):
        device = (qs.get("device_id") or [None])[0]
        try:
            limit = int((qs.get("limit") or [20])[0])
        except ValueError:
            limit = 20
        # 上限从 500 提到 4200：网页的趋势图加了「1 小时」时间窗，1Hz 下需要 3600 条
        # 才画得满。原来的 500 会让那一档静默地只剩 8 分钟的数据 —— 界面写着 1 小时、
        # 实际画 8 分钟，属于"看起来对的错数据"，比报错更难发现。
        limit = max(1, min(limit, 4200))

        # 时间窗（分钟）。**必须有这个参数**，光靠 limit 是不够的：
        # limit 是"取最近 N 条"，而设备一旦有离线空档，N 条就可能横跨好几天
        # （实测 limit=3600 返回的 3600 条跨了 4.6 天）。那样界面写着"最近 1 小时"、
        # 实际画的是 4 天，又是一条"看着对其实是错的"证据。
        # 所以时间范围由服务器按时间过滤，limit 只当安全上限用。
        try:
            minutes = int((qs.get("minutes") or [0])[0])
        except ValueError:
            minutes = 0
        minutes = max(0, min(minutes, 60 * 24 * 30))     # 最多 30 天

        where: list = []
        args: list = []
        if device:
            where.append("device_id = ?")
            args.append(device)
        if minutes > 0:
            where.append("server_ms >= ?")
            args.append(int((time.time() - minutes * 60) * 1000))

        sql = "SELECT * FROM readings"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with LOCK:
            rows = CONN.execute(sql, args).fetchall()
        return self._send_json(200, {"count": len(rows),
                                     "minutes": minutes or None,
                                     "records": [row_to_dict(r) for r in rows]})

    def api_devices(self):
        with LOCK:
            rows = CONN.execute(
                "SELECT device_id, COUNT(*) AS n, MAX(server_ms) AS last_ms"
                " FROM readings GROUP BY device_id ORDER BY last_ms DESC"
            ).fetchall()
        now = time.time() * 1000
        with LOCK:
            total = CONN.execute("SELECT COUNT(*) FROM readings").fetchone()[0]

        devices = []
        for r in rows:
            ds = DEVICE_STATE.get(r["device_id"]) or {}
            devices.append({
                "device_id": r["device_id"],
                "count": r["n"],
                "age_seconds": round((now - r["last_ms"]) / 1000, 2),
                # 暂停期间 readings 不再增长，所以 age_seconds 会一直变大。
                # 若不把 paused 带出来，网页会把它显示成「未更新」——
                # 那是**冤假错案**：设备其实活得好好的，只是被我们要求别上报了。
                "paused": bool(ds.get("paused")),
                "seen_seconds": (round((now - ds["last_seen_ms"]) / 1000, 2)
                                 if ds.get("last_seen_ms") else None),
            })
        return self._send_json(200, {
            "devices": devices,
            "total": total,        # 当前库里总条数（网页用来显示"累计 N 条"）
            "max_rows": MAX_ROWS,  # 上限，超出后自动删除最早的记录
        })

    def api_export(self, qs):
        """把历史记录导出成 CSV 文件，交给浏览器下载。

        几个刻意的选择：
        - 顺序改成**时间正序**（id 升序）：曲线/表格看最新在前更方便，
          但导出的数据一般要拿去做分析，按时间从早到晚更顺。
        - 开头写一个 UTF-8 BOM：不然 Excel 双击打开中文表头会是乱码。
        - 表头用 Content-Disposition: attachment，浏览器直接下载而不是在页面里打开。
        """
        device = (qs.get("device_id") or [None])[0]
        try:
            limit = int((qs.get("limit") or [EXPORT_LIMIT])[0])
        except ValueError:
            limit = EXPORT_LIMIT
        limit = max(1, min(limit, EXPORT_LIMIT))

        # hours=24 表示"只要最近 24 小时的"，不传则不限时间
        hours = None
        try:
            if qs.get("hours"):
                hours = float((qs.get("hours") or [0])[0])
        except ValueError:
            hours = None

        sql = "SELECT * FROM readings WHERE 1=1"
        args: list = []
        if device:
            sql += " AND device_id = ?"
            args.append(device)
        if hours and hours > 0:
            sql += " AND server_ms >= ?"
            args.append(int((time.time() - hours * 3600) * 1000))
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with LOCK:
            rows = CONN.execute(sql, args).fetchall()
        rows = list(reversed(rows))

        cols = ["id", "device_id", "seq", "server_time", "device_ms",
                "ax", "ay", "az", "gx", "gy", "gz",
                "temp_c", "free_heap", "min_free_heap", "total_heap", "rssi",
                "trigger", "request_id", "cmd_state", "cmd_note", "button",
                "press_delay_ms", "src_ip"]
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(cols)
        for r in rows:
            d = row_to_dict(r)
            w.writerow(["" if d.get(c) is None else d.get(c) for c in cols])

        body = ("\ufeff" + buf.getvalue()).encode("utf-8")
        fname = "readings_%s.csv" % time.strftime("%Y%m%d_%H%M%S")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition",
                         'attachment; filename="%s"' % fname)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ---------- 摄像头视频流（服务器中转） ----------
    def _write_frame(self, frame: bytes):
        self.wfile.write(CLIENT_BOUNDARY + b"\r\n")
        self.wfile.write(b"Content-Type: image/jpeg\r\n")
        self.wfile.write(("Content-Length: %d\r\n\r\n" % len(frame)).encode("ascii"))
        self.wfile.write(frame)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    def api_camera(self):
        """把板子的 MJPEG 流转发给浏览器。
        注意：服务器与板子之间始终只有 1 路连接，浏览器再多也不会压垮板子。"""
        q = RELAY.subscribe()
        first = queue.Empty
        try:
            # 首帧等久一点：板子刚重启时要先连 WiFi 再起摄像头，8 秒常常不够，
            # 以前会直接 503，页面就陷入「重连→又没帧→再重连」的循环
            first = q.get(timeout=15.0)
        except queue.Empty:
            pass
        except Exception:
            RELAY.unsubscribe(q)
            return
        if first is None or first is queue.Empty:
            RELAY.unsubscribe(q)
            st = RELAY.get_status()
            return self._send_json(503, {"error": "摄像头画面暂不可用", "status": st})
        try:
            self.send_response(200)
            self.send_header(
                "Content-Type",
                f"multipart/x-mixed-replace;boundary={FRAME_BOUNDARY}")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Pragma", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Connection", "close")
            self.end_headers()
            self._write_frame(first)
            while True:
                try:
                    frame = q.get(timeout=3.0)
                except queue.Empty:
                    continue          # 上游还没帧，继续等，别断开
                if frame is None:     # 哨兵：上游结束，收摊让前端重连
                    break
                self._write_frame(frame)
        except (BrokenPipeError, ConnectionResetError):
            pass                      # 浏览器关页面了，正常情况
        except Exception:
            pass
        finally:
            RELAY.unsubscribe(q)

    def serve_file(self, filepath):
        try:
            with open(filepath, "rb") as f:
                body = f.read()
        except OSError:
            return self._send_json(404, {"error": "index.html not found"})
        self._send(200, body, "text/html; charset=utf-8")

    # ---------- POST ----------
    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/api/command":
            return self.api_issue_command()
        if path == "/api/poll":
            return self.api_poll()
        if path == "/api/debug/fault":
            return self.api_debug_fault()
        if path == "/api/help":
            return self.api_help_record()
        if path.startswith("/api/help/") and path.endswith("/reply"):
            return self.api_help_reply(path[len("/api/help/"):-len("/reply")])
        if path == "/api/ask":
            return self.api_ask()
        if path == "/api/asr":
            return self.api_asr()
        if path == "/api/audio":
            return self.api_audio()
        if path != "/api/data":
            return self._send_json(404, {"error": "not found"})

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return self._send_json(400, {"error": "empty body"})

        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            return self._send_json(400, {"error": "bad json", "detail": str(e)})

        device_id = str(data.get("device_id") or "").strip()
        if not device_id:
            return self._send_json(400, {"error": "device_id required"})

        def f(key):
            try:
                return float(data.get(key, 0.0))
            except (TypeError, ValueError):
                return 0.0

        # 设备状态字段允许为 NULL：板子没上报/读数失败时存 NULL 而不是 0，
        # 这样网页能显示"—"，不会把"没测到"伪装成"温度 0 度"。
        def fnum(key):
            v = data.get(key)
            if v is None:
                return None
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        def fint(key):
            v = data.get(key)
            if v is None:
                return None
            try:
                return int(v)
            except (TypeError, ValueError):
                return None

        try:
            seq = int(data.get("seq", 0))
        except (TypeError, ValueError):
            seq = 0
        try:
            device_ms = int(data.get("ts", 0))
        except (TypeError, ValueError):
            device_ms = 0

        server_ms = int(time.time() * 1000)

        # —— 第2/3周：这一条数据是被谁触发的 ——
        # trigger 把三种来源分开：'timer' 板子自己按时上报 / 'command' 网页点按钮
        # 下发的指令 / 'button' 现场的人按板子上的实体键。
        # 这是把「新采集」与「历史记录」区分开的**数据层证据**（不只是界面话术）。
        request_id = data.get("request_id")
        request_id = str(request_id).strip() if request_id else None
        trigger = str(data.get("trigger") or "timer").strip() or "timer"
        cmd_state = data.get("cmd_state")
        cmd_state = str(cmd_state).strip() if cmd_state else None
        cmd_note = data.get("cmd_note")
        cmd_note = str(cmd_note).strip() if cmd_note else None
        button = data.get("button")
        button = str(button).strip() if button else None

        # —— 故障注入（仅测试，默认关闭）——
        # 命中条件刻意排除带 cmd_state 的回执：那些是"设备在汇报结论"，
        # 拦掉它们就等于把设备唯一的求援信道也切了，测试会变成永远超时，
        # 反而验证不了 failed 这条路径。
        if (FAULT["mode"] == "command_data" and FAULT["times"] > 0
                and trigger == "command" and cmd_state is None):
            left = FAULT["times"]
            FAULT["times"] -= 1
            if FAULT["times"] == 0:
                FAULT["mode"] = None
            print(f"[fault] 注入失败（本条第 {left} 次）：拒绝入库 seq={seq}")
            return self._send_json(503, {"error": "injected fault",
                                         "remaining": FAULT["times"]})

        with LOCK:
            # —— 幂等：重复的终态回执不产生第二条记录 ——
            # 上报是"发出去"和"收到回应"两件事，中间断了，设备就分不清
            # 服务器到底处理没处理（经典的两将军问题）。所以设备必须重试，
            # 而服务器必须对重试免疫 —— 否则一次失败会留下三条一模一样的
            # "失败"记录，sample_count 变成 3，页面上显示成
            # "本次新采集入库 4 条"，看起来像证据，其实是错的。
            #
            # 判定很窄，只认"同一 request_id + 同一终态 cmd_state 已经存在"：
            # 终态在状态机里本来就冻结了，第二条不携带任何新信息。
            # （这与 /api/poll 不写 readings 是同一个道理：没信息的请求不留痕。）
            if cmd_state in ("done", "failed") and request_id:
                dup = CONN.execute(
                    "SELECT id FROM readings WHERE request_id = ? AND cmd_state = ?"
                    " ORDER BY id LIMIT 1", (request_id, cmd_state)).fetchone()
                if dup:
                    print(f"[cmd] {request_id} 重复的 {cmd_state} 回执，"
                          f"不重复入库（已有 id={dup['id']}）")
                    return self._send_json(200, {
                        "ok": True, "duplicate": True, "id": dup["id"],
                        "note": "该终态回执已收到过，本次不再入库",
                    })

            cur = CONN.execute(
                "INSERT INTO readings"
                " (device_id, seq, ax, ay, az, gx, gy, gz, device_ms, server_ms, src_ip,"
                "  temp_c, free_heap, min_free_heap, total_heap, rssi,"
                "  request_id, trigger, cmd_state, button, press_delay_ms, cmd_note)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (device_id, seq, f("ax"), f("ay"), f("az"),
                 f("gx"), f("gy"), f("gz"), device_ms, server_ms,
                 self.client_address[0],
                 fnum("temp_c"), fint("free_heap"), fint("min_free_heap"),
                 fint("total_heap"), fint("rssi"),
                 request_id, trigger, cmd_state, button, fint("press_delay_ms"),
                 cmd_note),
            )
            CONN.commit()
            new_id = cur.lastrowid
            maybe_trim(CONN)      # 超上限就删最早的，控制在锁内避免并发问题

        ds = device_state(device_id)
        ds["last_seen_ms"] = server_ms
        ds["last_reading_ms"] = server_ms
        ds["last_kind"] = "data"

        self._apply_cmd_receipt(trigger, request_id, cmd_state, note=cmd_note)

        # —— 第2周：把待执行指令捎带回去 ——
        # 板子只做 POST、不监听端口，无法被"推"。这里在响应体里带上指令，
        # 板子拿到后立刻执行。响应体为空时板子本来也不解析，天然向后兼容。
        resp = {"ok": True, "id": new_id, "server_ms": server_ms}
        # 只在**定时上报**这条路径上捎带指令，理由有两条：
        #   · 板子执行指令期间不该再叠加新指令（避免交叉执行）
        #   · 板子只在定时上报那条路径里解析响应体。第3周的实体按键批次也会
        #     POST，但它不解析响应 —— 若把指令搭在它的响应里，板子收不到而
        #     服务器已标记 delivered，这条指令会一直卡到超时。
        if trigger == "timer":
            self._attach_pending(device_id, resp)
        return self._send_json(200, resp)

    def api_poll(self):
        """POST /api/poll —— 暂停周期上报期间的**心跳**，用来保持命令通道。

        任务卡第2周明确要求「暂停周期上报、保持命令通道」，目的很清楚：
        停掉 1Hz 的传感上报之后，库里任何新记录都只可能来自指令 ——
        这才是"证明开发板进行了新采集"最硬的证据，比页面上比数字强得多。

        所以这个端点**不写 readings**，只做三件事：
          1) 记录设备在线，以及它自报的「当前是否暂停」
          2) 收板子对指令的 received / done 回执（pause/resume 也走这条）
          3) 把待执行指令继续搭车捎回去 —— 通道因此不断
        """
        st, code = self._read_json_body()
        if code:
            return code
        device_id = str(st.get("device_id") or "").strip()
        if not device_id:
            return self._send_json(400, {"error": "device_id required"})

        paused = bool(st.get("paused"))
        now = int(time.time() * 1000)

        ds = device_state(device_id)
        ds["paused"] = paused
        ds["last_seen_ms"] = now
        ds["last_poll_ms"] = now
        ds["last_kind"] = "poll"

        request_id = st.get("request_id")
        request_id = str(request_id).strip() if request_id else None
        cmd_state = st.get("cmd_state")
        cmd_state = str(cmd_state).strip() if cmd_state else None

        if not paused:
            # 板子自己恢复了上报（例如重启后状态复位）—— 如实记录，不假装它还在暂停
            print(f"[poll] {device_id} 上报已恢复（paused=false）")
        self._apply_cmd_receipt("command", request_id, cmd_state)

        resp = {"ok": True, "paused": paused, "server_ms": now}
        self._attach_pending(device_id, resp)
        return self._send_json(200, resp)

    def api_debug_fault(self):
        """POST /api/debug/fault —— 故障注入，只用于测试「失败」这条路径。

        默认关闭：必须用 --fault-injection 启动服务器，否则这里一律 403。
        理由：这个端点能主动让设备上报失败，属于人为制造故障的能力，
        不该在平时开着的服务里存在。

        用法：
            {"mode": "command_data", "times": 6}   让接下来 6 条指令采集上报 503
            {"mode": "off"}                        取消
        """
        if not FAULT_ENABLED:
            return self._send_json(403, {
                "error": "fault injection disabled",
                "hint": "用 --fault-injection 启动服务器才可用（仅供测试）",
            })
        body, code = self._read_json_body()
        if code:
            return code
        mode = str(body.get("mode") or "off").strip()
        if mode not in ("off", "command_data"):
            return self._send_json(400, {"error": "unknown mode", "mode": mode})
        times = 0
        if mode != "off":
            try:
                times = int(body.get("times") or 0)
            except (TypeError, ValueError):
                return self._send_json(400, {"error": "times must be int"})
            if times <= 0:
                return self._send_json(400, {"error": "times must be > 0"})
        FAULT["mode"] = None if mode == "off" else mode
        FAULT["times"] = times
        print(f"[fault] 故障注入 → mode={FAULT['mode']} times={times}")
        return self._send_json(200, {"ok": True, "mode": FAULT["mode"],
                                     "times": FAULT["times"]})

    # ---------- 第3周：教学求助 ----------
    #
    # ⚠️ 只连本组教学接收端：不打电话、不发短信、不推第三方。
    #    所有响应的安全声明字段都由这里统一注入，避免漏掉某一处。
    HELP_SAFETY = ("教学测试用：只发往本组服务器并在本站网页显示，"
                   "不会连接或拨打任何真实紧急服务。")

    def _help_public(self, ev: dict) -> dict:
        out = HELP.to_public(ev)
        out["safety"] = self.HELP_SAFETY
        return out

    def api_help_record(self):
        """POST /api/help —— 设备（或网页模拟）发起一条教学求助测试消息。

        设备侧一次按键产生两样东西：本条求助事件 + 几条环境快照
        （快照走 /api/data，trigger='button'，两条共用同一个 event_id，
        所以能精确对上）。
        """
        body, code = self._read_json_body()
        if code:
            return code

        device_id = str(body.get("device_id") or "").strip()
        if not device_id:
            return self._send_json(400, {"error": "device_id required"})

        event_id = body.get("event_id")
        event_id = str(event_id).strip() if event_id else None

        snapshot = body.get("snapshot")
        if not isinstance(snapshot, dict):
            snapshot = {}

        press_delay = None
        if body.get("press_delay_ms") is not None:
            try:
                press_delay = int(body["press_delay_ms"])
            except (TypeError, ValueError):
                press_delay = None

        ev, dup = HELP.record(
            device_id=device_id,
            event_id=event_id,
            button=(str(body["button"]).strip() if body.get("button") else None),
            local_feedback=(str(body["local_feedback"]).strip()
                            if body.get("local_feedback") else None),
            snapshot=snapshot,
            source=str(body.get("source") or "button").strip() or "button",
            simulated=bool(body.get("simulated")),
            press_delay_ms=press_delay,
        )
        pub = self._help_public(ev)
        if dup:
            # 幂等：设备重试同一条求助不能再造一条（第2周的教训：
            # 重复的标识符被当成新事实，会产出"看起来像证据、其实是错的数字"）
            print(f"[help] {ev['id']} 重复上报，不重复入库")
            return self._send_json(200, dict(pub, duplicate=True))
        print(f"[help] 收到教学求助 {ev['id']} device={device_id} "
              f"button={ev.get('button')} 本地反馈={ev.get('local_feedback')}")
        return self._send_json(201, dict(pub, duplicate=False))

    def api_help_list(self, qs):
        """GET /api/help —— 求助列表（网页面板用）。"""
        device = (qs.get("device_id") or [None])[0]
        state = (qs.get("state") or [None])[0]
        try:
            limit = int((qs.get("limit") or ["20"])[0])
        except ValueError:
            limit = 20
        rows = HELP.recent(device_id=device, state=state, limit=limit)
        now = int(time.time() * 1000)
        return self._send_json(200, {
            "count": len(rows),
            "counts": HELP.counts(device),
            "server_ms": now,
            "safety": self.HELP_SAFETY,
            "events": [self._help_public(r) for r in rows],
        })

    def api_ask(self):
        """POST /api/ask —— 第4周的自然语言入口。

        body: {"text": "查看上次数据"}

        这是一条**受限**的语言入口：
          · 语言模型只负责把话解析成「意图 + 参数」，不查库、不下发、不作答
          · 真正的动作由两个受限工具执行（读已有记录 / 请求重新采集）
          · 回答里的来源、时间、状态全部来自工具的真实返回
          · 没有设备完成证据时，回答里不会出现「已采集成功」
        """
        body, code = self._read_json_body()
        if code:
            return code
        text = str(body.get("text") or "").strip()
        if not text:
            return self._send_json(400, {"error": "text required"})

        try:
            result = AGENT.handle(text)
        except Exception as e:
            # 兜底：助手内部出错时**不能**假装成功，也不能静默
            print(f"[ask] 助手异常: {e}")
            return self._send_json(500, {
                "ok": False,
                "error": "agent_exception",
                "detail": str(e)[:300],
                "reply": "助手内部出错，没有执行任何采集动作。",
            })

        # 注意：**不要**把 llm 的 base_url / api_key 之类回传。
        # AGENT.public_info() 只暴露主机名、模型名、是否有密钥。
        print(f"[ask] 「{text[:40]}」→ intent={result.get('intent')} "
              f"tool={result.get('tool')} ok={result.get('ok')} "
              f"{result.get('elapsed_s')}s")
        return self._send_json(200, result)

    def _asr_run(self, wav_path):
        """跑一次识别，返回统一的 dict（失败也不抛异常）。

        抽出来的原因：/api/asr（浏览器）和 /api/audio（板子）都要跑同一条识别链路。
        复制两份的话，将来改超时或改模型只会改到一处 —— 那种不一致最难查。
        调用方自己决定失败时回什么状态码，因为两边的消费方不同。
        """
        t0 = time.time()
        try:
            p = subprocess.run([ASR_PY, ASR_WORKER, wav_path],
                               capture_output=True, text=True, timeout=ASR_TIMEOUT_S,
                               encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "asr_timeout",
                    "elapsed_s": round(time.time() - t0, 2)}
        except Exception as e:
            return {"ok": False, "error": "asr_error", "detail": str(e)[:200]}

        line = (p.stdout or "").strip().splitlines()
        try:
            res = json.loads(line[-1]) if line else {}
        except json.JSONDecodeError:
            return {"ok": False, "error": "asr_error",
                    "detail": (p.stderr or p.stdout or "")[:300]}
        res["elapsed_s"] = round(time.time() - t0, 2)
        return res

    def _drain_body(self, length: int, cap: int) -> int:
        """把客户端**还在发**的请求体读掉，再回错误。

        为什么非读不可：直接回 413 然后关连接，客户端（浏览器 / 板子 / 脚本）
        可能还在写剩下的字节，Windows 上这会收到 RST ——
        于是它**读不到**那条友好的 JSON，浏览器只显示 "Failed to fetch"，
        "给可理解的反馈"当场落空。这是 test_asr_paths.py 实测出来的，不是推测。

        上限 cap 防止有人用超大请求把服务拖住：超过就停止排空、直接断。
        """
        drained = 0
        limit = min(length, cap)
        while drained < limit:
            chunk = self.rfile.read(min(65536, limit - drained))
            if not chunk:
                break
            drained += len(chunk)
        return drained

    def api_asr(self):
        """POST /api/asr —— 第5周：把一段录音变成文字。

        请求体是**裸 WAV**（不是 multipart）：16kHz / 单声道 / 16bit PCM。
        浏览器侧用 Web Audio 直接采 PCM 再封装成 WAV，所以这里不做格式转换 ——
        换格式需要 ffmpeg，本项目没装，也不该为一句语音引入一个转码依赖。

        返回里**必须**把三件事分开（对应任务卡的"无声、识别失败、服务超时都产生可理解反馈"）：
          · silence          —— 录到了，但里面没声音（麦克风开着却没说话）
          · audio_unreadable —— 传上来的不是合法 WAV（采集侧出了问题）
          · asr_error/timeout—— 识别服务本身异常
        把这三件事混成一句"识别失败"，用户根本不知道该查麦克风还是该重说一遍。
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return self._send_json(400, {"ok": False, "error": "empty_body",
                                         "reply": "没有收到音频。"})
        if length > ASR_MAX_BYTES:
            self._drain_body(length, ASR_MAX_BYTES * 4)
            return self._send_json(413, {"ok": False, "error": "too_large",
                                         "reply": "录音太长了（上限 4MB，约 2 分钟）。"})

        raw = self.rfile.read(length)
        # 先认文件头。浏览器侧如果编码错了，这里就能挡住，
        # 不用等识别进程起来再失败。
        if len(raw) < 44 or raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
            return self._send_json(400, {
                "ok": False, "error": "audio_unreadable",
                "reply": "收到的不是 WAV 音频，录音可能没成功。",
            })

        os.makedirs(AUDIO_DIR, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        wav_path = os.path.join(AUDIO_DIR, "rec-%s-%03d.wav" % (stamp, int(time.time() * 1000) % 1000))
        try:
            with open(wav_path, "wb") as f:
                f.write(raw)
        except OSError as e:
            return self._send_json(500, {"ok": False, "error": "store_failed",
                                         "detail": str(e)[:200]})

        if not os.path.exists(ASR_PY) or not os.path.exists(ASR_WORKER):
            return self._send_json(503, {
                "ok": False, "error": "asr_unavailable",
                "reply": "识别服务没装好（缺少 .venv-asr 或 tools/asr_worker.py），"
                         "文字入口仍然可用。",
            })

        t0 = time.time()
        res = self._asr_run(wav_path)
        if res.get("error") == "asr_timeout":
            res["reply"] = ("识别超时（超过 %g 秒），可以先改用文字输入。"
                            % ASR_TIMEOUT_S)
            return self._send_json(504, res)
        if res.get("error") == "asr_error":
            return self._send_json(500, res)

        # 统一成一句人话，前端不用为每种错误各写一套文案
        if res.get("ok"):
            res["reply"] = "听清了：" + res.get("text", "")
        elif res.get("error") == "silence":
            res["reply"] = "没听到声音 —— 看看麦克风是不是被静音了，或者离远了一点。"
        elif res.get("error") == "empty_text":
            res["reply"] = "有声音，但没听出词。再说一遍，稍微慢一点。"
        elif res.get("error") == "audio_unreadable":
            res["reply"] = "这段录音的格式不对（要 16kHz 单声道），这次没用上。"
        else:
            res.setdefault("reply", "识别出错了，可以先用文字输入。")

        print("[asr] %s ok=%s err=%s text=%r %ss"
              % (os.path.basename(wav_path), res.get("ok"), res.get("error"),
                 (res.get("text") or "")[:30], res["elapsed_s"]))
        return self._send_json(200 if res.get("ok") else 200, res)

    def api_audio(self):
        """POST /api/audio —— 第5周：**板子**录的一段 WAV（不是浏览器录的）。

        和 /api/asr 的分工（两个端点不是重复）：
          · /api/asr   是浏览器录的：识别完把文字回给页面，页面自己再调 /api/ask；
          · /api/audio 是板子录的：板子没有喇叭、也没有页面，所以识别完就在这里
            把第4周那条自然语言链路直接跑完，结果留在 AUDIO_LOG 里让页面来取。

        为什么不让板子自己去调 /api/ask：板子只发一条 HTTP、不看返回，
        把"识别 → 执行 → 留档"放在一步里，板子侧的代码才可能小到能塞进固件。
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return self._send_json(400, {"ok": False, "error": "empty_body"})
        if length > 1024 * 1024:          # 10 秒 16k 单声道约 320KB，1MB 留了一倍余量
            self._drain_body(length, 4 * 1024 * 1024)
            return self._send_json(413, {"ok": False, "error": "too_large"})

        raw = self.rfile.read(length)
        if len(raw) < 44 or raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
            return self._send_json(400, {"ok": False, "error": "audio_unreadable"})

        os.makedirs(AUDIO_DIR, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        wav_path = os.path.join(AUDIO_DIR, "dev-%s-%03d.wav"
                                % (stamp, int(time.time() * 1000) % 1000))
        with open(wav_path, "wb") as f:
            f.write(raw)

        res = self._asr_run(wav_path)
        text = (res.get("text") or "").strip()
        # 板子录满上限被截断时会带 ?truncated=1。这个标记必须留下来 ——
        # "被切成半句"和"识别不准"在页面上长得一模一样，不区分就会冤到模型头上。
        qs = parse_qs(urlparse(self.path).query)
        truncated = (qs.get("truncated", ["0"])[0] or "0").lower() in ("1", "true", "yes")
        entry = {
            "id": os.path.basename(wav_path),
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source": "device-mic",
            "bytes": len(raw),
            "truncated": truncated,
            "asr_elapsed_s": res.get("elapsed_s"),
            "asr_error": res.get("error"),
            "text": text,
            "ok": bool(text),
        }
        if text:
            try:
                answer = AGENT.handle(text)
                entry["reply"] = answer.get("reply")
                entry["intent"] = answer.get("intent")
                entry["tool"] = answer.get("tool")
                entry["ok"] = bool(answer.get("ok"))
            except Exception as e:
                entry["reply"] = "助手内部出错，没有执行任何采集动作。"
                entry["ok"] = False
                print("[audio] agent 异常: %s" % e)
        else:
            entry["reply"] = {
                "silence": "没听到声音 —— 板子麦克风可能没收到声音，靠近一点再说一次。",
                "empty_text": "有声音，但没听出词。再说一遍，稍微慢一点。",
                "audio_unreadable": "这段音频格式不对（要 16k 单声道 WAV）。",
            }.get(res.get("error"), "识别出错了，可以改用网页文字输入。")

        with AUDIO_LOCK:
            AUDIO_LOG.append(entry)
            del AUDIO_LOG[:-AUDIO_LOG_MAX]

        print("[audio] %s bytes=%d text=%r reply=%r"
              % (entry["id"], entry["bytes"], text[:30], (entry.get("reply") or "")[:40]))
        return self._send_json(200, entry)

    def api_audio_latest(self, qs):
        """GET /api/audio/latest —— 页面拉取板子最近的语音记录。"""
        try:
            limit = max(1, min(20, int((qs.get("limit") or ["5"])[0])))
        except ValueError:
            limit = 5
        with AUDIO_LOCK:
            items = AUDIO_LOG[-limit:]
        return self._send_json(200, {"events": list(reversed(items))})

    def api_help_reply(self, eid: str):
        """POST /api/help/<id>/reply —— 查看者「回应」或「取消」。

        body: {"action": "ack" | "cancel", "by": "谁", "reason": "为什么取消"}
        """
        body, code = self._read_json_body()
        if code:
            return code
        action = str(body.get("action") or "").strip()
        by = str(body.get("by") or "").strip() or None
        reason = str(body.get("reason") or "").strip() or None

        ev, res = HELP.reply(eid, action, by=by, reason=reason)
        if res == "notfound":
            return self._send_json(404, {"error": "no such help event", "id": eid})
        if res == "bad_action":
            return self._send_json(400, {"error": "unknown action",
                                         "action": action,
                                         "allowed": ["ack", "cancel"]})
        if res == "closed":
            return self._send_json(409, {
                "error": "already cancelled",
                "id": eid,
                "hint": "这条求助已经取消了，不能再回应。",
                "event": self._help_public(ev),
            })

        reply_sent = None
        if res == "ok":
            # —— 迁移「确认/取消机制」：把结果也送回设备 ——
            # 设备没有下行通道，仍然靠搭车（它下次上报/心跳时随响应取走）。
            # 回传的目的不是让设备"记状态"，而是让**站在设备旁边的人**知道
            # 远端有没有人应他 —— 这正是任务卡问的那句"自己和查看信息的人
            # 分别应获得什么反馈"。
            try:
                cmd = COMMANDS.issue(ev["device_id"], "help_reply",
                                     {"event_id": eid, "action": action,
                                      "by": by})
                HELP.set_reply_cmd(eid, cmd["id"])
                reply_sent = cmd["id"]
                print(f"[help] {eid} {action} → 回传指令 {cmd['id']} "
                      f"（搭车下发，设备取走后会再给一次本地提示）")
            except Exception as e:      # 回传失败不影响网页侧的回应本身
                print(f"[help] {eid} {action} 已记录，但回传指令下发失败: {e}")
        else:
            print(f"[help] {eid} {action} 重复操作（幂等，不再回传）")

        out = self._help_public(ev)
        out["result"] = res
        # 只有本次**新生成**了回传指令才覆盖；重复操作时保留原来那条的 id，
        # 否则界面上会显示"没有回传指令"，而实际上早就有一条了。
        if reply_sent:
            out["reply_command_id"] = reply_sent
        out["reply_command_created"] = bool(reply_sent)
        return self._send_json(200, out)

    # ---------- 第2 / 3 周共用的几段逻辑 ----------
    def _read_json_body(self):
        """读并解析请求体。返回 (body, 错误响应或 None)。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}, None
        raw = self.rfile.read(length)
        try:
            return (json.loads(raw.decode("utf-8")) or {}), None
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            return {}, self._send_json(400, {"error": "bad json", "detail": str(e)})

    def _apply_cmd_receipt(self, trigger, request_id, cmd_state, note=None):
        """处理板子回报的回执，推进指令状态机。

        只认 trigger=="command"：实体按键批次也会带 request_id（BTN-…），
        但它压根不是指令的产物，不该进指令状态机（否则日志里会凭空多出
        一堆"处理回执失败：没有这条指令"的噪音）。

        三个终态的区别（这是这一版新增 failed 的意义所在）：
            done    —— 设备明确回报"干完了"
            failed  —— 设备明确回报"我试了，没干成"，并带上原因
            timeout —— 服务器什么回音都没听到，只能判定"不知道"
        failed 和 timeout 必须分开：前者要去看设备，后者要去看网络。
        """
        if not (trigger == "command" and request_id
                and cmd_state in ("received", "done", "failed")):
            return
        try:
            if cmd_state == "received":
                COMMANDS.mark_received(request_id)
                print(f"[cmd] {request_id} 板子已接收（设备侧确认）")
            elif cmd_state == "done":     # done：顺便把统计结果补全
                with LOCK:
                    row = CONN.execute(
                        "SELECT COUNT(*) n, MIN(id) a, MAX(id) b"
                        " FROM readings WHERE request_id = ?",
                        (request_id,)).fetchone()
                COMMANDS.mark_done(request_id, sample_count=row["n"],
                                   first_id=row["a"], last_id=row["b"],
                                   note=note)
                extra = f"（备注：{note}）" if note else ""
                print(f"[cmd] {request_id} 执行完成，新采集 {row['n']} 条{extra}")
            else:                          # failed：设备明确说它干不成
                reason = note or "设备回报执行失败（未给出原因）"
                COMMANDS.mark_failed(request_id, reason)
                print(f"[cmd] {request_id} 设备回报执行失败：{reason}")
        except Exception as e:        # 回执处理失败不能影响数据入库
            print(f"[cmd] 处理回执失败 {request_id}: {e}")

    def _attach_pending(self, device_id, resp):
        """把待执行指令塞进响应体 —— 板子只 POST 不监听，推不了它，只能搭车。"""
        pend = COMMANDS.pending_for(device_id)
        if not pend:
            return False
        resp["command"] = {
            "request_id": pend["id"],
            "type": pend["type"],
            "params": json.loads(pend["params"] or "{}"),
            "issued_at_ms": pend["created_ms"],
        }
        print(f"[cmd] {pend['id']} 已随响应下发（delivered）")
        return True

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()


def lan_ip() -> str:
    """取本机在局域网里的 IP。

    板子和别的设备（手机、别人的电脑）要靠这个地址访问，只打印 127.0.0.1 没用 ——
    那就是板子自己。这里用 UDP socket"连"一下外部地址，让内核告诉我们
    "这个目的地你会从哪块网卡出去"，**不会真的发包，也不需要联网**。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()


class Server(ThreadingHTTPServer):
    """带一个必要的修正：**启动时不做反向 DNS 查询**。

    `http.server.HTTPServer.server_bind()` 里有一句 `socket.getfqdn(host)`，
    它会为绑定地址做一次反向解析。这台机器 DNS 不稳（github 都连不上），
    于是整台服务器卡在那里：**端口已绑定、但从不 listen**，
    日志一个字都没有，`netstat` 也看不到 LISTEN ——
    表现像"服务根本没启动"，实际是卡在启动的第一行网络调用上。

    这里只保留真正的 bind，server_name 直接用绑定地址。
    """
    daemon_threads = True

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host or "localhost"
        self.server_port = port


def main():
    # 必须在用到 MAX_ROWS/STALE_SECONDS 之前声明（它们要给 argparse 当默认值）；
    # 不声明 global 的话，下面只是改了局部变量，命令行参数不会生效。
    global MAX_ROWS, STALE_SECONDS, FAULT_ENABLED
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--stale", type=float, default=STALE_SECONDS,
                    help="判定未更新的秒数阈值")
    ap.add_argument("--camera-url", default="",
                    help="板子视频流地址，留空则自动从上报表里找最近的设备 IP")
    ap.add_argument("--max-rows", type=int, default=MAX_ROWS,
                    help="数据库最多保留多少条记录，超出后自动删除最早的")
    ap.add_argument("--fault-injection", action="store_true",
                    help="打开 /api/debug/fault（测试专用：人为让设备上报失败）")
    args = ap.parse_args()

    MAX_ROWS = max(1, args.max_rows)
    # --stale 改的是全局阈值，自然语言助手也用同一个阈值判断"数据过期"，
    # 不然会出现「网页说数据是新的、助手说数据过期了」这种自相矛盾。
    AGENT.stale_seconds = args.stale
    STALE_SECONDS = args.stale
    FAULT_ENABLED = bool(args.fault_injection)
    if args.camera_url:
        RELAY.set_board_url(args.camera_url)

    maybe_trim(CONN, force=True)   # 启动时先检查一次：上次跑可能已经超上限了

    # —— 第2周：超时看门狗 ——
    # 指令超时不能只靠"查的时候顺手算"，必须由服务器**主动记下**超时这件事，
    # 否则"超时"就没有服务器侧的证据。这里每秒扫一次并落库。
    def sweep_loop():
        while True:
            try:
                for item in COMMANDS.sweep_timeouts():
                    print(f"[cmd] {item['id']} 超时（阶段={item['stage']}）")
            except Exception as e:
                print(f"[cmd] 超时扫描异常: {e}")
            time.sleep(1.0)

    threading.Thread(target=sweep_loop, daemon=True, name="cmd-timeout").start()

    srv = Server((args.host, args.port), Handler)
    lan = lan_ip()
    print("[week1] 服务已启动")
    print(f"[week1]   本机访问  : http://127.0.0.1:{args.port}")
    if lan:
        print(f"[week1]   局域网访问: http://{lan}:{args.port}"
              f"   ← 板子靠这个地址上报；同一 WiFi 下的手机/电脑也能用它打开页面")
    else:
        print("[week1]   局域网访问: （没探测到局域网 IP，可能没接网线/没连 WiFi）")
    print(f"[week1] 数据库: {DB_PATH}")
    print(f"[week1] 未更新阈值: {STALE_SECONDS} 秒")
    print(f"[week1] 记录上限: {MAX_ROWS} 条（超出自动删除最早的）")
    print(f"[week1] 摄像头中转: http://127.0.0.1:{args.port}/api/camera"
          f"（无需知道板子 IP）")
    print(f"[week1] 下行指令: POST http://127.0.0.1:{args.port}/api/command"
          f"（状态查询 /api/command/<id>）")
    print(f"[week1] 教学求助: POST http://127.0.0.1:{args.port}/api/help"
          f"（回应/取消 /api/help/<id>/reply）")
    print("[week1] ⓘ 教学求助只连本组教学接收端：不打电话、不发短信、"
          "不推第三方服务")
    if FAULT_ENABLED:
        # 启动横幅上明确喊出来 —— 这是个能人为制造故障的开关，
        # 万一忘了关，日志里必须一眼能看见，而不是靠记得。
        print("[week1] ⚠️ 故障注入已开启（--fault-injection，测试专用）"
              ": POST /api/debug/fault 可让设备上报失败")
    print("[week1] Ctrl+C 停止")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[week1] 已停止")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
