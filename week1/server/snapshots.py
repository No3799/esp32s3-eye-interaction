"""第 7 周 · 按需环境快照（on-demand snapshot）

一句话：**只有在有人（或教学求助事件）明确要求时，才去板子取一张图，
并把"这张图是什么时候拍的、拍成功了没有"落成可查的记录。**

三条设计约束，都是从任务卡原文里逼出来的：

1. 「上次成功图像不得被标为本次采集成功」
   → 每次请求只返回**这一次**的结果。失败就是失败，可以附带"上次成功那张
     是什么时候的"作为参考，但必须显式标 `is_this_attempt: false`。
     绝不拿旧图冒充新图。

2. 「只在请求或教学事件触发后取图」
   → 没有定时抓拍。`trigger` 只接受 user / help_event / replay，
     其它值直接拒绝并记一条 denied。这就是"授权"落到代码里的样子。

3. 「设置最少保留」
   → 默认最多留 MAX_KEEP 张，但**最近一张成功图像永不删除**。
     否则清理逻辑一旦跑过头，连"上次成功"都没了 —— 那第 1 条就没法被执行。

关于"画面不可用"：没有 GPU、没有 numpy，用 PIL 解码后看两个统计量：
平均亮度（全黑/镜头被挡）与标准差（纯色/糊成一片）。
这是启发式判断，不是目标检测；阈值写在下面，判错时能指到具体数值。
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import threading
import time
import uuid
import urllib.error
import urllib.request

# ---- 判定阈值（改了要同步改文档，否则记录里的结论会与代码不一致）----
# 注意：**不要**用"字节数太小"来判可用性。全黑/被遮挡的画面恰恰压缩得极小
# （320x240 纯黑 JPEG 只有 ~1.5KB），用字节数当门槛会把"遮挡"误判成"太小"，
# 两者的处置完全不同。判据顺序因此是：先认 JPEG → 再解码 → 再看尺寸 → 最后看亮度。
MIN_BYTES = 200            # 只用作"连 JPEG 头都不够"的兜底，不做可用性判断
MIN_LUMA = 12.0            # 平均亮度低于此值 → 判为"全黑/被遮挡"
MIN_STDDEV = 3.0           # 标准差低于此值 → 判为"几乎是纯色/画面不可用"
MIN_W, MIN_H = 160, 120    # 太小的图不当作有效环境画面
MAX_KEEP = 50              # 最多保留张数（最近一张成功图不受此限）

TRIGGERS = ("user", "help_event", "replay")

# 板子在局域网里，**不要走系统代理**（本机代理对某些地址会返回 502，
# 之前踩过：同一个地址 curl 能通、urllib 报 Tunnel failed）
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _now() -> tuple[int, str]:
    ms = int(time.time() * 1000)
    return ms, time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ms / 1000))


class SnapshotStore:
    """图片文件 + 一份 append-only 索引。

    为什么不用 SQLite：这些记录是"一张一张的证物"，顺序追加、按时间读，
    用 JSONL 一眼能看懂，出了问题也能直接用文本编辑器查。
    （真正的业务表 readings/commands/help_events 仍然在 SQLite 里。）
    """

    def __init__(self, data_dir: str):
        self.dir = os.path.join(data_dir, "snapshots")
        os.makedirs(self.dir, exist_ok=True)
        self.index_path = os.path.join(self.dir, "index.jsonl")
        self._lock = threading.Lock()
        self._seq = 0
        self._records: list[dict] = []
        self._load()

    # ---------- 索引读写 ----------
    def _load(self):
        if not os.path.exists(self.index_path):
            return
        with open(self.index_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    self._records.append(json.loads(line))
                except json.JSONDecodeError:
                    # 坏行不阻塞启动：跳过并在内存里记一条，避免"索引一坏全站不能用"
                    self._records.append({"id": "BAD-LINE", "status": "index_corrupt",
                                          "captured_at": "", "note": line[:80]})
        if self._records:
            self._seq = len(self._records)

    def _append(self, rec: dict):
        with open(self.index_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def add(self, rec: dict) -> dict:
        with self._lock:
            self._seq += 1
            rec.setdefault("seq", self._seq)
            self._records.append(rec)
            self._append(rec)
        self._prune()
        return rec

    # ---------- 查询 ----------
    def all(self, limit: int = 20) -> list[dict]:
        with self._lock:
            return list(reversed(self._records))[:max(1, min(limit, 200))]

    def get(self, snap_id: str) -> dict | None:
        with self._lock:
            for r in self._records:
                if r.get("id") == snap_id:
                    return r
        return None

    def last_ok(self) -> dict | None:
        """最近一次**设备真实采集**成功的记录。

        刻意把 source=replay 排除在外：回放是拿历史图练习，
        如果它也算"上次成功"，用户会以为"上次真的拍到了"。
        """
        with self._lock:
            for r in reversed(self._records):
                if r.get("status") == "ok" and r.get("source") == "device":
                    return r
        return None

    def path_of(self, rec: dict) -> str:
        f = rec.get("file") or ""
        return os.path.join(self.dir, f) if f else ""

    # ---------- 保留策略 ----------
    def _prune(self):
        """最多留 MAX_KEEP 张，但**最近一张成功图不删**。

        这条"例外"不是心软：任务卡要求"上次成功图像不得被标为本次采集成功"，
        如果连那张图都没了，就没法向用户说明"你上次拍到的是什么"。
        """
        with self._lock:
            oks = [r for r in self._records if r.get("status") == "ok" and r.get("file")]
            keep_ok = oks[-1]["id"] if oks else None
            files = [r for r in self._records if r.get("file")]
            if len(files) <= MAX_KEEP:
                return
            drop = files[: len(files) - MAX_KEEP]
            for r in drop:
                if r["id"] == keep_ok:
                    continue
                p = self.path_of(r)
                try:
                    if p and os.path.exists(p):
                        os.remove(p)
                except OSError:
                    pass
                r["pruned"] = True


def _judge(data: bytes) -> dict:
    """判断这张 JPEG 能不能当"环境画面"用。返回判定字段，不做 IO。"""
    out = {"bytes": len(data)}
    # 先认 JPEG：这一步必须排在字节数判断之前，否则"全黑图很小"会被错判成 too_small
    if not data.startswith(b"\xff\xd8"):
        out.update(status="bad_jpeg", usable=False,
                   reason="不是 JPEG（缺少 SOI 标记，收到 %d 字节）" % len(data))
        return out
    if len(data) < MIN_BYTES:
        out.update(status="bad_jpeg", usable=False,
                   reason="只有 %d 字节，连一个 JPEG 头都不够" % len(data))
        return out
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data)).convert("L")   # 转灰度就够判亮度
        w, h = img.size
        px = list(img.getdata())
    except Exception as e:
        out.update(status="bad_jpeg", usable=False, reason="解码失败：%s" % str(e)[:80])
        return out

    n = len(px) or 1
    mean = sum(px) / n
    var = sum((p - mean) ** 2 for p in px) / n
    std = var ** 0.5
    out.update(width=w, height=h, mean_luma=round(mean, 2), stdev=round(std, 2))

    if w < MIN_W or h < MIN_H:
        out.update(status="too_small", usable=False,
                   reason="分辨率 %dx%d 低于 %dx%d" % (w, h, MIN_W, MIN_H))
    elif mean < MIN_LUMA:
        out.update(status="too_dark", usable=False,
                   reason="平均亮度 %.1f < %.0f，镜头可能被遮挡或全黑" % (mean, MIN_LUMA))
    elif std < MIN_STDDEV:
        out.update(status="flat_frame", usable=False,
                   reason="标准差 %.1f < %.0f，画面几乎是纯色" % (std, MIN_STDDEV))
    else:
        out.update(status="ok", usable=True, reason="")
    return out


class SnapshotService:
    def __init__(self, store: SnapshotStore, board_ip_fn, timeout_s: float = 8.0):
        self.store = store
        self._board_ip = board_ip_fn
        self.timeout_s = timeout_s

    # ---------- 内部工具 ----------
    @staticmethod
    def _new_id(ms: int) -> str:
        return "SNAP-%s-%s" % (time.strftime("%Y%m%d-%H%M%S", time.localtime(ms / 1000)),
                               uuid.uuid4().hex[:4])

    def _save(self, snap_id: str, data: bytes) -> str:
        """图片落盘。失败也要留下记录，不能让"存不下"变成"什么都没发生"。"""
        fname = snap_id + ".jpg"
        try:
            with open(os.path.join(self.store.dir, fname), "wb") as f:
                f.write(data)
            return fname
        except OSError:
            return ""

    # ---------- 真实采集 ----------
    def capture(self, request_id: str, trigger: str, note: str = "",
                replay_of: str = "", force_ip: str = "") -> dict:
        """取一张图并落记录。**只返回这一次的结果。**"""
        ms, at = _now()
        snap_id = self._new_id(ms)
        base = {"id": snap_id, "request_id": request_id or "", "trigger": trigger,
                "captured_ms": ms, "captured_at": at, "note": note,
                "source": "device", "authorized_by": trigger}

        # ---- 授权：没有明确触发来源，直接拒绝（并留痕）----
        if trigger not in TRIGGERS:
            base.update(status="denied", usable=False,
                        reason="trigger=%r 不在 %s 内 —— 没有明确请求不取图"
                               % (trigger, list(TRIGGERS)))
            return self.store.add(base)

        # ---- 回放：不碰设备，只是把一张已存在的成功图像再拿出来 ----
        if trigger == "replay":
            src = self.store.get(replay_of) if replay_of else None
            if not src or src.get("status") != "ok":
                base.update(status="replay_missing", usable=False,
                            reason="回放要指定一张已存在的成功图像（replay_of）")
                return self.store.add(base)
            base.update(source="replay", replay_of=replay_of,
                        file=src.get("file"), width=src.get("width"),
                        height=src.get("height"), bytes=src.get("bytes"),
                        status="ok", usable=True, latency_ms=0,
                        reason="回放 %s（拍摄于 %s）" % (replay_of, src.get("captured_at")))
            return self.store.add(base)

        # ---- 找板子 ----
        ip = force_ip or ""
        if not ip:
            try:
                ip = self._board_ip() or ""
            except Exception:
                ip = ""
        if not ip:
            base.update(status="board_unknown", usable=False,
                        reason="没有任何上报记录能推出板子 IP —— 先让板子上线")
            return self.store.add(base)

        # ---- 取一帧 ----
        url = "http://%s:81/capture" % ip
        t0 = time.time()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "wb-snapshot"})
            with _OPENER.open(req, timeout=self.timeout_s) as r:
                data = r.read()
            latency = int((time.time() - t0) * 1000)
        except urllib.error.HTTPError as e:
            base.update(status="board_error", usable=False,
                        latency_ms=int((time.time() - t0) * 1000),
                        reason="板子返回 HTTP %s" % e.code)
            return self.store.add(base)
        except Exception as e:
            # 超时与连不上要分开：超时是"拍到了但没送回来"，连不上是"根本没拍到"
            m = str(e).lower()
            kind = "timeout" if ("timeout" in m or "timed out" in m) else "unreachable"
            base.update(status=kind, usable=False,
                        latency_ms=int((time.time() - t0) * 1000),
                        reason="%s: %s" % (type(e).__name__, str(e)[:100]))
            return self.store.add(base)

        judged = _judge(data)
        base.update(judged)
        base["latency_ms"] = latency
        base["board_ip"] = ip
        base["sha256"] = hashlib.sha256(data).hexdigest()[:16]

        # 不论好坏都存盘：失败的画面也是证据（"到底拍到了什么"）
        base["file"] = self._save(snap_id, data)
        return self.store.add(base)

    # ---------- 历史图像回放 ----------
    def import_replay(self, src_path: str, request_id: str = "",
                      note: str = "") -> dict:
        """把一张**历史真实图像**导入成回放记录。

        任务卡备用路径：「用获授权的真实历史图像练习，标为回放；随后补验一次真实按需采集」。
        所以这里必须：
          · source 固定 replay，绝不写 device
          · 记录原文件名与 sha256，能追到它是从哪个文件来的
          · 拍摄时间不知道就写不知道，不用导入时间冒充
        """
        ms, at = _now()
        snap_id = self._new_id(ms)
        base = {"id": snap_id, "request_id": request_id or "", "trigger": "replay",
                "captured_ms": ms, "captured_at": at, "note": note,
                "source": "replay", "authorized_by": "replay", "imported_at": at}
        if not os.path.exists(src_path):
            base.update(status="replay_missing", usable=False,
                        reason="找不到源文件：%s" % src_path)
            return self.store.add(base)

        with open(src_path, "rb") as f:
            data = f.read()
        judged = _judge(data)
        if judged.get("usable"):
            judged["reason"] = ("回放自 %s（原图拍摄时间未记录，导入于 %s）"
                                % (os.path.basename(src_path), at))
        base.update(judged)
        base["bytes"] = len(data)
        base["sha256"] = hashlib.sha256(data).hexdigest()[:16]
        base["imported_from"] = os.path.abspath(src_path)
        base["file"] = self._save(snap_id, data)
        return self.store.add(base)
