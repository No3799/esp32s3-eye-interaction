#!/usr/bin/env python3
"""第4周 —— 自然语言助手：意图解析 + 受限工具 + 真实结果回复。

这一周要做的事，用一句话说清楚：
    把一句人话，映射成**两种不同的任务**（读已有记录 / 触发一次新采集），
    然后**用前两周已经验证过的接口**去执行，最后**只根据真实发生的事**回答。

三种角色要分清楚（任务卡「前2学时」明确要求区分）：

  ① 开发助手        = 写这份代码的 AI（WorkBuddy / IDE 里的助手）。
                      它能读文件、跑命令、改代码 —— 权限极大。
  ② 产品运行时模型  = 部署在服务端的语言模型（这里接本地授权模型 qwen3.8-q2）。
                      它**只**做一件事：把用户那句话解析成
                      {intent, device_id, samples, interval_ms}。
                      它不查库、不下发指令、不决定答案内容。
  ③ 受限工具        = 服务端真正干活的函数（read_latest / request_recollect）。
                      参数被校验，设备范围被限制，副作用只有一种。

为什么把 ② 压得这么窄？因为本单元的难点是
「概率性语言输出 → 确定性任务执行」。
如果让模型自由生成答案，它会说「已采集成功」——而设备可能根本没回执。
所以这里的分工是硬性的：

    模型只出「意图 + 参数」  →  服务端校验  →  工具执行真实接口
                             →  服务端按真实结果拼出回答

**回答的每一个事实性内容都由服务端从工具返回值里取，模型不参与。**
模型最多只能对措辞做润色（NL_REPLY_STYLE=model），而且润色结果必须过
「无证据断言闸门」（见 _assert_evidence_gate）：状态不是 done 时，
只要润色文本里出现「已采集成功」这类说法，就直接丢弃润色、回退到模板。

密钥边界：LLM 的地址与密钥只从**服务端环境变量**读
（NL_LLM_BASE_URL / NL_LLM_API_KEY / NL_LLM_MODEL），
任何 API 响应、任何网页元素里都不会出现它们。
"""

from __future__ import annotations

import json
import os
import re
import socket
import time
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# 默认配置（全部可用环境变量覆盖；密钥永远只在服务端）
# ---------------------------------------------------------------------------
DEFAULT_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen3.8-q2:latest"
DEFAULT_TIMEOUT = 120.0        # 冷启动要加载 9.8GB 权重，给足时间
DEFAULT_COLLECT_WAIT = 45.0    # 采集指令最多等多久（accept 30s + exec 30s 的上界内）

# 采集参数的限幅 —— 与第2周 server.py 的限幅保持一致，
# 这样「网页点按钮」和「说一句话」两条路径不会给出不同的行为。
SAMPLES_MIN, SAMPLES_MAX = 1, 100
INTERVAL_MIN, INTERVAL_MAX = 20, 1000

# 本系统**没有**的能力。用户问到这些时必须如实说不足，不能编。
# 这条对应任务卡「无位置或过期位置须说明不足，不能生成坐标」：
# 板子上只有 IMU 和摄像头，没有 GPS / 北斗 / 任何定位源，
# 所以「设备在哪」「给我坐标」这类问题，系统**根本没有数据可答**。
#
# ⚠️ 必须是**扁平**的关键词列表。曾经写成 ((...),(...)) 双层元组，
#    遍历时拿到的是内层元组，kw.lower() 直接抛 AttributeError，
#    表现为「问坐标」这种越界提问 500 报错 —— 恰好是最该正常的路径出错。
UNSUPPORTED_HINTS = (
    "位置", "坐标", "经度", "纬度", "定位", "在哪", "哪里", "gps", "GPS",
    "北斗", "经纬", "定位信息", "什么位置",
)


# ---------------------------------------------------------------------------
# 模型侧失败的归类
# ---------------------------------------------------------------------------
def classify_llm_error(e: BaseException) -> str:
    """把"模型没给出意图"拆成四类可操作的故障。

    为什么要拆：这四类的修法完全不同 ——
      · model_not_found   → 服务在跑但模型没加载（重启服务 / 查 /api/tags）
      · model_unreachable → 服务没起 / 端口不通 / 超时（起服务、查防火墙）
      · model_auth        → 密钥或权限不对（换 key，服务端问题，不是提示词问题）
      · invalid_json      → 服务好好的，是模型输出不合法（改提示词 / 换模型）
    混成一句"服务不可用"，人会去重启一个本来正常的服务。

    **判据以异常类型和 HTTP 状态码为主，文本只作兜底**：
    中文 Windows 上连接被拒的报错是「由于目标计算机积极拒绝，无法连接。」，
    纯英文关键词匹配（refused）会漏掉 —— 这是实测踩出来的，不是假设。
    """
    code = getattr(e, "code", None)
    if isinstance(code, int):
        if code == 404:
            return "model_not_found"
        if code in (401, 403):
            return "model_auth"
        return "model_error"

    reason = getattr(e, "reason", None)
    refused_types = (ConnectionRefusedError, ConnectionResetError, ConnectionAbortedError)
    if isinstance(e, refused_types) or isinstance(reason, refused_types):
        return "model_unreachable"
    if isinstance(e, (TimeoutError, socket.timeout)) or isinstance(reason, (TimeoutError, socket.timeout)):
        return "model_unreachable"

    txt = "%s %s" % (e, reason if reason is not None else "")
    low = txt.lower()
    if "10061" in low or "refused" in low or "拒绝" in txt:
        return "model_unreachable"
    if "timed out" in low or "超时" in txt:
        return "model_unreachable"
    if "404" in low or "not found" in low:
        return "model_not_found"
    if "json" in low:
        return "invalid_json"
    return "model_error"


# ---------------------------------------------------------------------------
# 语言模型客户端
# ---------------------------------------------------------------------------
class LLMClient:
    """产品运行时模型（②）的唯一入口。

    支持两种协议，方便换用教师提供的授权服务：
      · ollama   —— POST {base}/api/chat
      · openai   —— POST {base}/v1/chat/completions（带 Bearer 密钥）

    ⚠️ 密钥只在这里用一次（放进请求头），**不会**出现在返回给网页的任何内容里。
    """

    def __init__(self, base_url: str | None = None, model: str | None = None,
                 api_key: str | None = None, timeout: float | None = None,
                 protocol: str | None = None):
        self.base_url = (base_url or os.environ.get("NL_LLM_BASE_URL")
                         or DEFAULT_BASE_URL).rstrip("/")
        self.model = model or os.environ.get("NL_LLM_MODEL") or DEFAULT_MODEL
        self.api_key = api_key or os.environ.get("NL_LLM_API_KEY") or ""
        self.timeout = float(timeout or os.environ.get("NL_LLM_TIMEOUT")
                             or DEFAULT_TIMEOUT)
        proto = (protocol or os.environ.get("NL_LLM_PROTOCOL") or "auto").lower()
        if proto == "auto":
            proto = "openai" if self.base_url.rstrip("/").endswith("/v1") else "ollama"
        self.protocol = proto

    # -- 对外只暴露「主机名」，不暴露完整 URL（完整 URL 里可能带 token） --
    def public_info(self) -> dict:
        host = re.sub(r"^https?://", "", self.base_url).split("/")[0]
        return {"host": host, "model": self.model, "protocol": self.protocol,
                "has_key": bool(self.api_key), "timeout_s": self.timeout}

    def _post(self, url: str, body: dict, headers: dict) -> dict:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        for k, v in headers.items():
            req.add_header(k, v)
        # 关键：**不走环境里的 http_proxy**。本机语言服务在 127.0.0.1，
        # 走代理会被拒（实测 "upstream connect failed: 目标计算机积极拒绝"）。
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=self.timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
        return json.loads(raw)

    def chat_json(self, system: str, user: str) -> dict:
        """让模型只吐一个 JSON 对象。返回解析后的 dict。"""
        if self.protocol == "openai":
            headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
            body = {
                "model": self.model,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}],
                "temperature": 0,
                "response_format": {"type": "json_object"},
            }
            raw = self._post(self.base_url + "/v1/chat/completions", body, headers)
            content = (raw.get("choices") or [{}])[0].get("message", {}).get("content", "")
        else:
            headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
            body = {
                "model": self.model,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}],
                "stream": False,
                "format": "json",              # ollama 的结构化输出：直接约束为合法 JSON
                "options": {"temperature": 0},
            }
            raw = self._post(self.base_url + "/api/chat", body, headers)
            content = (raw.get("message") or {}).get("content", "")

        content = (content or "").strip()
        if content.startswith("```"):                     # 去掉可能出现的 ```json 围栏
            content = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", content).strip()
        try:
            out = json.loads(content)
        except json.JSONDecodeError:
            # 退一步：从正文里抠出第一个 {...} 块再试一次
            m = re.search(r"\{.*\}", content, re.S)
            if not m:
                raise ValueError(f"模型没有返回 JSON：{content[:200]!r}")
            out = json.loads(m.group(0))
        if not isinstance(out, dict):
            raise ValueError(f"模型返回的不是 JSON 对象：{out!r}")
        return out


# ---------------------------------------------------------------------------
# 意图解析的提示词
# ---------------------------------------------------------------------------
INTENT_SYSTEM = """你是传感数据平台的**意图解析器**。你的唯一职责：
把用户的一句中文，转成一个 JSON 对象。你**不查数据、不执行命令、不回答问题**。

可用能力只有两种（除此之外的一律输出 unsupported）：
  1. query_latest     —— 读取**已有的最新一条**记录（"查看上次数据"、"现在数据是多少"、
                          "最近读数"、"看一下当前值"）
  2. request_recollect —— 请求设备**现在再采一次**（"重新采集"、"再采一次"、
                          "刷新一下数据"、"重新取一次数"）
  3. unsupported      —— 用户问的是系统**没有**的能力
                          （例如位置/坐标/经纬度 —— 本设备没有定位传感器）
  4. unclear          —— 用户的话**不足以判断**要读还是要采
                          （例如"看看数据吧"、"搞一下"、"能用的吗"）

判定要点：
  · "查看/显示/读/是多少/最近/上次" 且**没有**"重新/再/新采" → query_latest
  · "重新采集/再采一次/重新取/刷新数据/现在采一次" → request_recollect
  · 只是问"能不能/好不好/怎么用" → unclear
  · 问位置、坐标、经纬度、GPS、你在哪 → unsupported
  · 用户提了数量（"采 20 个"）就填 samples；提了间隔（"每 500 毫秒"）就填 interval_ms
  · 没提就填 null（服务端会填默认值并在回答里说明用了默认值）

只输出一个 JSON 对象，不要任何解释文字：
{"intent": "...", "device_id": "用户说的设备名或空字符串", "samples": null, "interval_ms": null, "reason": "一句话中文理由"}
"""


# ---------------------------------------------------------------------------
# 断言闸门：没有证据就不许说成功
# ---------------------------------------------------------------------------
SUCCESS_CLAIM_PATTERNS = (
    "已采集成功", "采集成功", "成功采集", "已成功采集", "重新采集成功",
    "已完成采集", "采集完成", "数据已更新", "已更新数据", "已刷新",
    "已经采到", "取到了新数据",
)


def _assert_evidence_gate(text: str, allowed: bool) -> tuple[str, bool, str]:
    """检查一句自然语言里有没有「无证据的成功声明」。

    allowed=False 表示当前**没有**设备完成证据，
    这时文本里只要出现成功类说法，就必须把它判为不合格。

    返回 (文本, 是否通过, 原因)
    """
    if allowed:
        return text, True, ""
    low = text.lower()
    for p in SUCCESS_CLAIM_PATTERNS:
        if p.lower() in low:
            return text, False, f"无完成证据却声称「{p}」"
    return text, True, ""


# ---------------------------------------------------------------------------
# 自然语言助手
# ---------------------------------------------------------------------------
class NLAgent:
    """把一句自然语言 → 两个受限工具之一 → 基于真实结果的回答。

    依赖注入进来的是**已有的、已验证过的东西**：
      · conn / lock  —— 第1周的 SQLite 库
      · commands     —— 第2周的 CommandStore（下发 + 回执状态机）
      · device_state —— 第1/2周的设备在线状态表
    所以第4周**没有重写任何底层通信协议**，只是在前两周的接口上加了一层入口。
    """

    def __init__(self, llm: LLMClient, conn, lock, commands, device_state,
                 stale_seconds: float = 5.0, collect_wait: float | None = None,
                 reply_style: str | None = None):
        self.llm = llm
        self.conn = conn
        self.lock = lock
        self.commands = commands
        self.device_state = device_state
        self.stale_seconds = stale_seconds
        self.collect_wait = float(collect_wait if collect_wait is not None
                                   else os.environ.get("NL_COLLECT_WAIT",
                                                       DEFAULT_COLLECT_WAIT))
        self.reply_style = (reply_style or os.environ.get("NL_REPLY_STYLE")
                            or "template").lower()

    # ---------------- 设备范围 ----------------
    # 设备范围要按"**当前还活着的**设备"来定，而不是"库里曾经出现过的一切"。
    #
    # 为什么：本工程的历史库里留着一台第1周的数据模拟器 selftest-sim
    # （68 条，2026-09-23 之后再没上报过）。它是一条**真实存在的历史记录**，
    # 不能删；但如果把它算进"当前设备"，那么"查看上次数据"这句话就会变成
    # 歧义（库里有两台设备，得让用户指明），而用户明明只有一台真板子。
    #
    # 判据：一条设备在**最近 ACTIVE_WINDOW 秒**内有上报，才算"当前设备"。
    # 超时的老设备仍然能通过"明确点名"来查询（那是查历史，本来就该能查），
    # 但不参与"未指明时选哪台"的判断。
    ACTIVE_WINDOW = 300.0   # 5 分钟

    def known_devices(self, active_only: bool = False) -> list[dict]:
        """库里出现过的设备。**这是设备范围的唯一真相**。

        模型说出来的设备名必须落在这个集合里，否则不予执行 ——
        否则模型一句「给 device999 采一次」就能往系统里塞一个不存在的设备。
        """
        with self.lock:
            rows = self.conn.execute(
                "SELECT device_id, COUNT(*) AS n, MAX(server_ms) AS last_ms"
                "  FROM readings GROUP BY device_id ORDER BY last_ms DESC"
            ).fetchall()
        now = time.time() * 1000
        out = []
        for r in rows:
            age = round((now - r["last_ms"]) / 1000, 1) if r["last_ms"] else None
            out.append({
                "device_id": r["device_id"],
                "samples": r["n"],
                "last_ms": r["last_ms"],
                "age_seconds": age,
                "active": bool(age is not None and age <= self.ACTIVE_WINDOW),
            })
        if active_only:
            out = [d for d in out if d["active"]]
        return out

    def _resolve_device(self, asked: str | None) -> tuple[str | None, str, list[str]]:
        """把模型给的设备名落到真实设备上。

        返回 (device_id 或 None, 说明, 候选列表)

        规则：
          · 未指明 → 在**当前活跃设备**里选。只有一台就用它；
            多台才需要用户澄清；一台都没有就说库里没有数据。
          · 指名了 → 在**全部已知设备**里匹配（包括已停止上报的老设备），
            因为"查历史"本来就应该查得到历史。
          · 采集类操作额外要求设备是活跃的 —— 给一台一个月没响应的板子
            下指令，只能等到超时，那不叫"完成"，也不该假装能成。
        """
        all_devices = self.known_devices()
        active = [d["device_id"] for d in all_devices if d["active"]]
        ids = [d["device_id"] for d in all_devices]
        asked = (asked or "").strip()
        if not asked:
            if len(active) == 1:
                return active[0], f"未指定设备，当前只有一台活跃设备（{active[0]}），已采用", active
            if not active:
                return None, "库里还没有任何**活跃**设备的数据（可能设备已离线）", []
            return None, "当前有 " + "、".join(active) + " 共 " + str(len(active)) + " 台活跃设备，需要你指明", active
        # 精确匹配
        for did in ids:
            if asked == did:
                return did, "设备名精确匹配", ids
        # 宽松匹配：忽略大小写 / 空格 / 下划线
        key = lambda s: re.sub(r"[\s_\-]", "", s.lower())
        for did in ids:
            if key(asked) == key(did):
                return did, "设备名模糊匹配", ids
        # 子串包含
        cands = [did for did in ids if key(asked) in key(did) or key(did) in key(asked)]
        if len(cands) == 1:
            return cands[0], "设备名按包含关系匹配", ids
        if len(cands) > 1:
            return None, "「" + asked + "」同时匹配多台设备：" + "、".join(cands), ids
        return None, "系统里没有名为「" + asked + "」的设备（现有：" + "、".join(ids) + "）", ids

    # ---------------- 工具①：读已有记录 ----------------
    def read_latest(self, device_id: str | None) -> dict:
        """受限工具①：读取**已有**的最新一条记录。

        刻意**不**触发任何采集，也不改任何状态 —— 它就是"读"，
        所以返回的记录时间是这条记录**自己的**时间（可能是几分钟前）。
        """
        sql = "SELECT * FROM readings"
        args: list = []
        if device_id:
            sql += " WHERE device_id = ?"
            args.append(device_id)
        sql += " ORDER BY id DESC LIMIT 1"
        with self.lock:
            row = self.conn.execute(sql, args).fetchone()
        if row is None:
            return {"ok": False, "error": "no_data",
                    "message": "库里还没有这条设备的记录"}
        rec = dict(row)
        # server_time 是**派生的可读字段**，不是表里的列 ——
        # readings 表只有 server_ms（毫秒时间戳）。回答里要给人看时间，
        # 所以这里自己补一个格式化字段，别指望表里有。
        rec["server_time"] = time.strftime("%Y-%m-%d %H:%M:%S",
                                          time.localtime(rec["server_ms"] / 1000))
        now = time.time() * 1000
        age = (now - rec["server_ms"]) / 1000
        ds = self.device_state.get(rec["device_id"]) or {}
        return {
            "ok": True,
            "tool": "read_latest",
            "record": rec,
            "age_seconds": round(age, 1),
            "stale": age > self.stale_seconds,
            "device_paused": bool(ds.get("paused")),
            "device_seen_seconds": (round((now - ds["last_seen_ms"]) / 1000, 1)
                                    if ds.get("last_seen_ms") else None),
            "source": {
                "kind": "sqlite",
                "table": "readings",
                "record_id": rec["id"],
                "device_id": rec["device_id"],
                "request_id": rec.get("request_id"),
                "trigger": rec.get("trigger"),
            },
        }

    # ---------------- 工具②：请求一次新采集 ----------------
    def request_recollect(self, device_id: str, samples: int, interval_ms: int) -> dict:
        """受限工具②：请求设备**现在再采一次**（复用第2周的指令状态机）。

        关键：这里返回的是**证据链**，不是一个结论。
        是不是"成功"，由设备回执（done）和新观测条数决定，不由本函数说了算。
        """
        busy = self.commands.active_for(device_id, "recollect")
        if busy:
            return {"ok": False, "error": "busy",
                    "request_id": busy["id"], "status": busy["status"],
                    "message": "上一次「重新采集」还在进行中（当前状态 " + str(busy["status"])
                               + "），等它结束再发起新的。"}

        cmd = self.commands.issue(device_id, "recollect",
                                  {"samples": samples, "interval_ms": interval_ms})
        cid = cmd["id"]

        deadline = time.time() + self.collect_wait
        last = None
        while time.time() < deadline:
            last = self.commands.get(cid)
            if not last:
                break
            if last.get("status") in ("done", "failed", "timeout"):
                break
            time.sleep(0.4)

        last = self.commands.get(cid) or last or {}
        status = last.get("status", "unknown")

        # 终态 done 时，还要在 readings 里找到**带这个 request_id** 的新记录。
        # 只有"回执说完成"或"库里有新记录"都不够 —— 必须是同一批。
        samples_in_db = 0
        first_ms = last_ms = None
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, server_ms FROM readings"
                " WHERE request_id = ? ORDER BY id ASC", (cid,)).fetchall()
        samples_in_db = len(rows)
        if rows:
            first_ms = rows[0]["server_ms"]
            last_ms = rows[-1]["server_ms"]

        has_evidence = (status == "done" and samples_in_db > 0)
        return {
            "ok": True,
            "tool": "request_recollect",
            "request_id": cid,
            "status": status,
            "params": {"samples": samples, "interval_ms": interval_ms},
            "samples_in_db": samples_in_db,
            "first_sample_ms": first_ms,
            "last_sample_ms": last_ms,
            "has_completion_evidence": has_evidence,
            "timeout_stage": last.get("timeout_stage"),
            "note": last.get("note"),
            "command": last,
            "source": {
                "kind": "command",
                "request_id": cid,
                "device_id": device_id,
                "created_at": last.get("created_at"),
                "done_at": last.get("done_at"),
            },
        }

    # ---------------- 回复拼装（事实全部来自工具返回） ----------------
    def _reply_read_latest(self, tool_res: dict, device_note: str) -> str:
        rec = tool_res["record"]
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(rec["server_ms"] / 1000))
        lines = [
            "已读取**已有记录**（没有触发新采集）。",
            "",
            f"- 数值：X={rec['ax']:.4f} g，Y={rec['ay']:.4f} g，Z={rec['az']:.4f} g"
            f"，模长 |a|={ (rec['ax']**2 + rec['ay']**2 + rec['az']**2) ** 0.5:.4f} g",
            f"- 数据时间：{ts}（距今约 {tool_res['age_seconds']} 秒）",
            f"- 来源：设备 {rec['device_id']} 的记录 id={rec['id']}"
            f"，触发方式 trigger={rec.get('trigger') or 'timer'}",
        ]
        if rec.get("request_id"):
            lines.append(f"- 关联请求：{rec['request_id']}（这条数据是一次采集产生的）")
        if tool_res["stale"]:
            lines.append(f"- ⚠️ 这条数据已**过期**（超过 {self.stale_seconds} 秒未更新），"
                         "反映的是历史值，不代表设备当前状态。")
        if tool_res.get("device_paused"):
            lines.append("- ℹ️ 设备当前处于「暂停周期上报」状态，它的最新值本来就停在旧记录上。")
        if device_note:
            lines.append(f"- 说明：{device_note}")
        return "\n".join(lines)

    def _reply_recollect(self, tool_res: dict, device_note: str,
                         used_default: str | None) -> str:
        st = tool_res["status"]
        lines = []
        if tool_res.get("error") == "busy":
            lines.append(f"没有发起新的采集：上一次「重新采集」还在进行中"
                         f"（请求 {tool_res['request_id']}，当前状态 {tool_res['status']}）。")
            lines.append(f"- 建议：等它结束再试，或先查 /api/command/{tool_res['request_id']} 看进展。")
            return "\n".join(lines)

        if st == "done" and tool_res["has_completion_evidence"]:
            ts = ""
            if tool_res.get("last_sample_ms"):
                ts = time.strftime("%Y-%m-%d %H:%M:%S",
                                   time.localtime(tool_res["last_sample_ms"] / 1000))
            lines.append("采集已完成（设备回执 + 新观测入库，两项证据齐全）。")
            lines.append(f"- 请求 id：{tool_res['request_id']}")
            lines.append(f"- 状态：done")
            # 这里要区分两个数：samples 是**下发给设备的样本条数**，
            # samples_in_db 是**实际入库的记录条数**。
            # 两者不相等是正常的：固件会额外发一条 received 回执和一条 done 回执
            # （第2周的设计：样本不带 cmd_state，采完单独发结论），
            # 所以入库数 = samples + 2。混着说会让人以为多出了数据。
            lines.append(f"- 本次请求的样本数：{tool_res['params'].get('samples')} 条"
                         f"（间隔 {tool_res['params'].get('interval_ms')} ms）")
            lines.append(f"- 实际入库：{tool_res['samples_in_db']} 条"
                         "（= 样本数 + 首条 received 回执 + 末条 done 回执）"
                         + (f"，最后一条时间 {ts}" if ts else ""))
            lines.append(f"- 来源：设备 {tool_res['source']['device_id']}，"
                         f"指令 {tool_res['request_id']}（第2周的指令状态机）")
        elif st == "failed":
            lines.append("采集**失败**：设备回报了失败，没有完成证据。")
            lines.append(f"- 请求 id：{tool_res['request_id']}")
            lines.append(f"- 状态：failed")
            if tool_res.get("note"):
                lines.append(f"- 设备备注：{tool_res['note']}")
            lines.append(f"- 新入库观测：{tool_res['samples_in_db']} 条")
        elif st == "timeout":
            stage = tool_res.get("timeout_stage") or "未知阶段"
            lines.append("采集**超时**，没有完成证据。")
            lines.append(f"- 请求 id：{tool_res['request_id']}")
            lines.append(f"- 状态：timeout（卡在 {stage} 阶段）")
            lines.append(f"- 新入库观测：{tool_res['samples_in_db']} 条")
            if stage == "accept":
                lines.append("- 含义：设备一直没来取这条指令（掉线或重启），"
                             "**不能据此判定硬件坏了**。")
            else:
                lines.append("- 含义：设备取走了指令但没报完成。")
        else:
            lines.append(f"采集请求已下发，但**还没有完成证据**（当前状态 {st}）。")
            lines.append(f"- 请求 id：{tool_res['request_id']}")
            lines.append(f"- 新入库观测：{tool_res['samples_in_db']} 条")
            lines.append(f"- 可以查 /api/command/{tool_res['request_id']} 看后续进展。")

        if used_default:
            lines.append(f"- 采集参数：{used_default}")
        if device_note:
            lines.append(f"- 说明：{device_note}")
        return "\n".join(lines)

    def _reply_unsupported(self, text: str) -> str:
        low = text.lower()
        for kw in UNSUPPORTED_HINTS:
            if kw.lower() in low:
                return ("这个系统**没有定位数据源**（板子上只有 IMU 加速度计和摄像头，"
                        "没有 GPS / 北斗 / 任何能给出经纬度的传感器），"
                        "所以无法提供位置或坐标。\n\n"
                        "我不会凭空生成一个坐标给你。能做的是：\n"
                        "- 查看已有记录（加速度、温度、采集时间）\n"
                        "- 请求设备现在重新采一次")
        return ("这件事超出了本系统当前的能力范围。\n\n"
                "本系统目前只支持两件事：读取已有记录、请求设备重新采集一次。")

    def _reply_unclear(self, text: str, devices: list[str]) -> str:
        opts = "、".join(devices) if devices else "（暂无）"
        return ("这句话我没能判断出是要**读已有记录**还是**重新采一次**。\n\n"
                f"现有设备：{opts}\n\n"
                "你可以直接说：\n"
                "- 「查看上次数据」 —— 读库里已有的最新一条\n"
                "- 「重新采集一次」 —— 让设备现在再采一批")

    # ---------------- 可选：让模型润色措辞（且要过闸门） ----------------
    def _polish(self, draft: str, allowed_success_claim: bool) -> tuple[str, dict]:
        if self.reply_style != "model":
            return draft, {"polished": False, "reason": "reply_style=template"}
        try:
            system = ("你是措辞润色助手。把给用户的回答改得更口语一点，"
                      "**不得增加任何新的事实、数字、状态或承诺**。"
                      "只输出改写后的正文，不要解释。")
            out = self.llm.chat_json(system, draft)
            # 模型有时会把它包成 {"text": "..."}，两种都收
            cand = out.get("text") or out.get("reply") or out.get("content") or ""
            if not isinstance(cand, str) or not cand.strip():
                raise ValueError("润色结果为空")
            cand = cand.strip()
        except Exception as e:
            return draft, {"polished": False, "reason": "润色失败：" + str(e)[:120]}
        ok, why = _assert_evidence_gate(cand, allowed_success_claim)
        if not ok:
            return draft, {"polished": False, "reason": "闸门拦下：" + why}
        # 数字不许变：比较两边的数字集合
        if sorted(re.findall(r"\d+\.?\d*", cand)) != sorted(re.findall(r"\d+\.?\d*", draft)):
            return draft, {"polished": False, "reason": "润色改动了数字，已回退"}
        return cand, {"polished": True, "reason": "ok"}

    # ---------------- 主入口 ----------------
    def handle(self, text: str) -> dict:
        """处理一句自然语言，返回结构化结果 + 一段可直接展示的回答。"""
        started = time.time()
        trace: list[dict] = []
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "empty", "reply": "请说一句话，比如「查看上次数据」。",
                    "trace": trace}

        # —— ① 模型只负责出意图+参数 ——
        try:
            parsed = self.llm.chat_json(INTENT_SYSTEM, "用户说：" + text)
            trace.append({"step": "intent_parse", "ok": True, "raw": parsed})
        except Exception as e:
            # 备用路径：模型侧失败时**不猜**意图、**不执行任何动作**。
            # 这里必须再分一层：四种失败的修法完全不同，混成一句"服务不可用"
            # 会让人去重启服务，而真正的原因可能是模型输出不是 JSON。
            err = str(e)[:200]
            kind = classify_llm_error(e)

            trace.append({"step": "intent_parse", "ok": False,
                          "error": err, "error_kind": kind})

            # 结构化样例：给**具体实例**而不是占位符。占位符只能说明字段名，
            # 没法直接拿去比对；实例可以逐字段对照模型的实际输出。
            sample = {"intent": "unclear", "device_id": "", "samples": None,
                      "interval_ms": None,
                      "reason": "模型侧失败（%s），未解析出意图" % kind}
            hint = {
                "model_not_found": "服务在跑，但模型没加载。先看 /api/tags 是否为空；"
                                   "为空就用 week1/启动语言服务.bat 重启（显式指定 OLLAMA_MODELS）。",
                "model_unreachable": "模型服务没起来。检查 11434 端口是否在监听，必要时启动服务。",
                "invalid_json": "服务是好的，模型输出不是合法 JSON。看 trace 里的原文，"
                                "多半要收窄提示词或换更稳的模型。",
                "model_auth": "服务连上了但拒绝授权。检查 NL_LLM_API_KEY 是否有效。",
                "model_error": "看 trace 里的原始报错。",
            }[kind]

            return {
                "ok": False,
                "error": "llm_unavailable",
                "error_kind": kind,
                # 机器可读的"什么都没做"凭据：验收时可以断言这个列表为空
                "actions_taken": [],
                "parse_sample": sample,
                "reply": ("语言模型服务暂时不可用，我没法判断这句话的意图，"
                          "所以**没有执行任何采集动作**（也没有下发任何指令）。\n\n"
                          "失败类别：%s\n"
                          "排查方向：%s\n\n"
                          "结构化样例（用于定位解析问题）：\n%s"
                          % (kind, hint, json.dumps(sample, ensure_ascii=False, indent=2))),
                "llm": self.llm.public_info(),
                "trace": trace,
            }

        intent = str(parsed.get("intent") or "").strip().lower()
        asked_device = str(parsed.get("device_id") or "").strip()
        reason = str(parsed.get("reason") or "").strip()

        # —— ② 服务端校验：意图必须在白名单内 ——
        if intent not in ("query_latest", "request_recollect", "unsupported", "unclear"):
            trace.append({"step": "validate_intent", "ok": False, "value": intent})
            intent = "unclear"

        # —— ③ 设备范围校验 ——
        device_id, device_note, device_ids = self._resolve_device(asked_device)
        trace.append({"step": "resolve_device", "asked": asked_device,
                      "resolved": device_id, "note": device_note,
                      "known": device_ids})

        if intent == "unsupported":
            reply = self._reply_unsupported(text)
            return self._pack(ok=True, intent=intent, reply=reply, trace=trace,
                              device_id=device_id, reason=reason,
                              started=started, tool=None, tool_args={})

        if intent == "unclear":
            reply = self._reply_unclear(text, device_ids)
            return self._pack(ok=True, intent=intent, reply=reply, trace=trace,
                              device_id=device_id, reason=reason,
                              started=started, tool=None, tool_args={})

        if device_id is None:
            # 设备范围不通过：明确说不执行，**不猜一个设备替用户做主**
            reply = ("设备范围校验没通过，所以**没有执行任何动作**。\n\n"
                     + device_note
                     + "\n\n现有设备：" + ("、".join(device_ids) if device_ids else "（无）"))
            return self._pack(ok=False, intent=intent, reply=reply, trace=trace,
                              device_id=None, reason=reason, started=started,
                              tool=None, tool_args={}, error="device_not_allowed")

        # —— ④ 参数限幅（与第2周一致）——
        used_default = None
        if intent == "request_recollect":
            raw_s = parsed.get("samples")
            raw_i = parsed.get("interval_ms")
            try:
                samples = int(raw_s) if raw_s is not None else 10
            except (TypeError, ValueError):
                samples = 10
                used_default = "samples 无法解析，用了默认 10 条"
            if raw_s is None:
                used_default = (used_default or "") + \
                    ("；" if used_default else "") + "samples 未指定，用了默认 10 条"
            try:
                interval = int(raw_i) if raw_i is not None else 100
            except (TypeError, ValueError):
                interval = 100
                used_default = (used_default or "") + \
                    ("；" if used_default else "") + "interval_ms 无法解析，用了默认 100ms"
            if raw_i is None:
                used_default = (used_default or "") + \
                    ("；" if used_default else "") + "interval_ms 未指定，用了默认 100ms"
            # 越界要**说明**，不能默默改
            clamped = []
            if samples < SAMPLES_MIN or samples > SAMPLES_MAX:
                clamped.append(f"samples {samples} → {max(SAMPLES_MIN, min(samples, SAMPLES_MAX))}")
                samples = max(SAMPLES_MIN, min(samples, SAMPLES_MAX))
            if interval < INTERVAL_MIN or interval > INTERVAL_MAX:
                clamped.append(f"interval_ms {interval} → {max(INTERVAL_MIN, min(interval, INTERVAL_MAX))}")
                interval = max(INTERVAL_MIN, min(interval, INTERVAL_MAX))
            if clamped:
                used_default = (used_default or "") + \
                    ("；" if used_default else "") + "参数越界已限幅：" + "、".join(clamped)
            tool_args = {"samples": samples, "interval_ms": interval}
        else:
            tool_args = {}

        # —— ⑤ 执行受限工具 ——
        if intent == "query_latest":
            tool_res = self.read_latest(device_id)
            if not tool_res.get("ok"):
                reply = (f"读取失败：{tool_res.get('message') or '没有数据'}。"
                         "如果这是刚上电的设备，可能还没有上报过任何记录。")
                return self._pack(ok=False, intent=intent, reply=reply, trace=trace,
                                  device_id=device_id, reason=reason, started=started,
                                  tool="read_latest", tool_args=tool_args,
                                  tool_result=tool_res, error=tool_res.get("error"))
            reply = self._reply_read_latest(tool_res, device_note)
            allowed_claim = False      # 读操作永远不会说"采集成功"
        else:
            # 采集前再查一次设备是否**活跃**。
            # 读历史可以读一台停了很久的板子（那是查历史，本来就该查得到），
            # 但给一台不响应的板子下发指令，只会等到超时 —— 那不是"完成"，
            # 与其白等 30 秒再报超时，不如当场说清"这台设备不在线"。
            dev_map = {d["device_id"]: d for d in self.known_devices()}
            dev = dev_map.get(device_id)
            if dev and not dev.get("active"):
                reply = (f"设备 {device_id} 当前**不在线**（最后一次上报是 "
                         f"{dev.get('age_seconds')} 秒前），所以没有下发采集指令。\n\n"
                         f"- 这台设备在库里共有 {dev.get('samples')} 条历史记录，"
                         "可以改成「查看上次数据」来读它。")
                return self._pack(ok=False, intent=intent, reply=reply, trace=trace,
                                  device_id=device_id, reason=reason, started=started,
                                  tool=None, tool_args=tool_args, error="device_offline")
            tool_res = self.request_recollect(device_id, tool_args["samples"],
                                              tool_args["interval_ms"])
            reply = self._reply_recollect(tool_res, device_note, used_default)
            allowed_claim = bool(tool_res.get("has_completion_evidence"))

        trace.append({"step": "tool", "ok": True, "tool": tool_res.get("tool"),
                      "args": tool_args, "result_summary": {
                          "status": tool_res.get("status"),
                          "samples_in_db": tool_res.get("samples_in_db"),
                          "has_completion_evidence": tool_res.get("has_completion_evidence"),
                      }})

        # 模板本身也要过一遍闸门（双保险）
        reply, gate_ok, gate_why = _assert_evidence_gate(reply, allowed_claim)
        if not gate_ok:
            reply = ("（安全闸门拦下了这条回答：检测到无完成证据却声称成功。）\n" + reply)

        polished, polish_info = self._polish(reply, allowed_claim)
        trace.append({"step": "reply", "ok": True, "gate": gate_ok,
                      "gate_reason": gate_why, "polish": polish_info})

        return self._pack(ok=True, intent=intent, reply=polished, trace=trace,
                          device_id=device_id, reason=reason, started=started,
                          tool=tool_res.get("tool"), tool_args=tool_args,
                          tool_result=tool_res)

    # ---------------- 打包 ----------------
    def _pack(self, ok, intent, reply, trace, device_id, reason, started,
              tool=None, tool_args=None, tool_result=None, error=None) -> dict:
        out = {
            "ok": ok,
            "intent": intent,
            "device_id": device_id,
            "reason": reason,
            "reply": reply,
            "tool": tool,
            "tool_args": tool_args or {},
            "tool_result": tool_result,
            "error": error,
            "elapsed_s": round(time.time() - started, 2),
            "llm": self.llm.public_info(),
            "trace": trace,
        }
        # 二次保险：任何情况下，工具结果里没有完成证据，
        # 就把回复里的成功声明清掉。这一层与闸门独立，
        # 目的是防止将来有人改了拼装逻辑就漏掉这条。
        res = tool_result or {}
        if res.get("tool") == "request_recollect" and not res.get("has_completion_evidence"):
            fixed, ok2, why = _assert_evidence_gate(out["reply"], False)
            if not ok2:
                out["reply"] = fixed
                out["reply_guarded"] = why
        return out
