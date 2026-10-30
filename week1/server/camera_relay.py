#!/usr/bin/env python3
"""摄像头 MJPEG 中继 —— 让视频流走服务器，而不是每个网页自己去连板子。

为什么必须这样做，而不是简单地用 requests 转发一下：

 1) 板子扛不住多路。实测 ESP32-S3-EYE 单路 QVGA 4~5 fps，两路并发直接崩到 0.1 fps。
    如果每个浏览器各自申请一路 /stream，两个人同时看网页就没画面了。
    这里改成：服务器跟板子始终保持【一路】连接，解析出 JPEG 帧，再分发给任意多个浏览器。
    板端永远只有 1 个客户端，压力恒定。

 2) 网页不再需要知道板子的 IP。板子 IP 由服务器从最近一条上报记录里自动取，
    换路由器导致 IP 变了也不用改代码。以后做远程访问更是只需要暴露服务器这一个端口。

 3) 浏览器<supported>只认 multipart/x-mixed-replace，所以中继出来的格式必须和板端一致，
    否则 <img src> 只会显示第一帧然后卡住。

用法（由 server.py 调用，不单独运行）:
    relay = CameraRelay()
    relay.subscribe() -> Queue      # 每个观看者一个队列
    relay.unsubscribe(q)
    relay.get_status() -> dict
"""

from __future__ import annotations

import queue
import re
import threading
import time
import urllib.request

# 板子在局域网里，**必须绕开系统代理**。
# 2026-10-30 实测事故：中继进程带着 https_proxy 启动，urlopen 把 http://10.1.41.160:81/stream
# 交给了代理，代理拒绝转发私网地址，回 **403 Forbidden** —— 页面上就是"摄像头显示不了画面"。
# 而同一时刻直接 curl 该地址是 200。所以不能依赖启动环境，代码里显式绕过。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 中继给浏览器的分隔串（与板端那个不一样，避免混淆）
FRAME_BOUNDARY = "week1relayframe"
CLIENT_BOUNDARY = ("--" + FRAME_BOUNDARY).encode("ascii")


class CameraRelay:
    """一个上游连接 + 多个下游订阅者"""

    def __init__(self, board_url: str = "", poll_seconds: float = 2.0,
                 latest_ip_fn=None):
        """latest_ip_fn: 返回最近上报的设备 IP 的函数，由 server.py 注入。
        传进来是为了避免反向 import server（会形成循环依赖）。"""
        self._latest_ip_fn = latest_ip_fn
        self._lock = threading.Lock()
        self._subs: "set[queue.Queue]" = set()
        self._pump = None              # 上游读取线程
        self._stop = threading.Event()
        # 没人观看后多久断开上游（留一点余量，避免刷新页面就重连）
        self._idle_before_stop = 5.0
        # 统计
        self.last_frame_at = 0.0       # 最近一帧到达时间
        self.frames_total = 0
        self.state = "idle"            # idle / connecting / streaming / error
        self.last_error = ""
        self.board_url_resolved = ""
        # board_url 为空时，由 resolve_url() 动态从数据库里的最新 src_ip 推导
        self._board_url_fixed = board_url
        self._poll = poll_seconds
        # 自愈相关：看门狗保证「有人看但线程死了」时能自动拉起来
        self.reconnects = 0          # 累计重连次数
        self._watchdog = threading.Thread(target=self._watchdog_loop, daemon=True)
        self._watchdog.start()

    # ---------- 板子地址 ----------
    def set_board_url(self, url: str):
        """命令行 --camera-url 指定后写死；不指定则自动从上报记录里找。"""
        self._board_url_fixed = url

    def resolve_url(self) -> str:
        """优先用命令行指定的固定地址；否则从最近一条上报记录取板子 IP。
        这样板子换 IP（DHCP 重新分配）时不需要改任何代码。"""
        if self._board_url_fixed:
            return self._board_url_fixed
        ip = self._latest_ip()
        if ip:
            self.board_url_resolved = f"http://{ip}:81/stream"
            return self.board_url_resolved
        return ""

    def _latest_ip(self) -> str:
        if self._latest_ip_fn:
            try:
                return self._latest_ip_fn() or ""
            except Exception:
                return ""
        return ""

    # ---------- 订阅管理 ----------
    def subscribe(self) -> "queue.Queue":
        q: "queue.Queue" = queue.Queue(maxsize=2)
        with self._lock:
            self._subs.add(q)
            self._maybe_start_pump_locked()
        return q

    def unsubscribe(self, q: "queue.Queue"):
        with self._lock:
            self._subs.discard(q)

    def _maybe_start_pump_locked(self):
        if self._pump and self._pump.is_alive():
            return
        self._stop.clear()
        self._pump = threading.Thread(target=self._run, daemon=True)
        self._pump.start()

    def _has_subs(self) -> bool:
        with self._lock:
            return len(self._subs) > 0

    def _watchdog_loop(self):
        """看门狗：每 2 秒检查一次。

        之前踩过的坑：上游是板子，板子会因为重启 / WiFi 抖动随时把这条 TCP 掐断。
        旧代码里线程一旦退出就只能等下一个观看者到来才重启；如果那一刻没人看，
        中继就永远停在 error —— 再来人也是 503，对外表现就是「摄像头彻底坏了」。
        这里补上兜底：只要还有订阅者而线程不在了，立刻重启。
        """
        while True:
            time.sleep(2)
            try:
                alive = bool(self._pump and self._pump.is_alive())
                if not alive and self._has_subs():
                    self._restart_pump("watchdog")
            except Exception:
                pass

    def _restart_pump(self, why: str):
        alive = bool(self._pump and self._pump.is_alive())
        if alive:
            return
        self.reconnects += 1
        self.last_error = f"重连中（原因：{why}）"
        self._stop.clear()
        self._pump = threading.Thread(target=self._run, daemon=True)
        self._pump.start()

    # ---------- 上游读取 ----------
    def _run(self):
        """外层：不断重连的壳。只有「连续没人看超过宽限时间」才真正退出。"""
        backoff = 1.0
        while not self._stop.is_set():
            if not self._has_subs():
                # 没人看：再等等，别让刷新页面就白白重连一次上游
                if self.last_frame_at and time.time() - self.last_frame_at > 20:
                    break
                time.sleep(0.3)
                continue

            reason = self._connect_and_stream()
            if reason == "no_viewers":
                self.state = "idle"
                break
            if reason == "no_ip":
                # 还不知道板子在哪，慢慢试，没必要狂重试
                time.sleep(2.0)
                continue
            if self._stop.is_set():
                break
            # 其余情况（超时 / 板端关闭 / 卡死）都重连
            self.state = "reconnecting"
            time.sleep(backoff)
            backoff = min(backoff * 1.6, 6.0)
        # 收摊：通知所有还在的请求结束，让页面自己重连
        self._wake_all()

    def _connect_and_stream(self) -> str:
        """连一次上游并持续读帧。返回值说明这一次是怎么结束的。"""
        url = self.resolve_url()
        if not url:
            self.state = "error"
            self.last_error = "还没收到过板子的上报，无法确定板子 IP"
            return "no_ip"

        self.state = "connecting"
        try:
            req = urllib.request.Request(url)
            # 超时不要设太长：宁可早点发现断流去重连，也不要干等。
            # 用 _OPENER（不走代理）而不是 urlopen，见文件头注释。
            resp = _OPENER.open(req, timeout=8)
        except Exception as e:
            self.state = "error"
            self.last_error = f"连不上板子 {url}: {e}"
            return "connect_fail"

        ctype = resp.headers.get("Content-Type", "")
        m = re.search(r'boundary=(?:"([^"]+)"|([^\s;]+))', ctype)
        if not m:
            self.state = "error"
            self.last_error = f"板端流格式不对: {ctype}"
            resp.close()
            return "bad_format"
        boundary = (m.group(1) or m.group(2)).encode("latin-1")
        marker = b"--" + boundary

        self.state = "streaming"
        self.last_error = ""
        buf = b""
        last_activity = time.time()
        last_frame_seen = time.time()
        reason = "read_fail"
        try:
            while not self._stop.is_set():
                now = time.time()
                with self._lock:
                    nsub = len(self._subs)
                if nsub == 0:
                    if now - last_activity > self._idle_before_stop:
                        return "no_viewers"
                    time.sleep(0.2)
                    continue
                # 卡死检测：连着 12 秒一帧都没解出来，多半是这条连接已经废了
                if now - last_frame_seen > 12:
                    self.last_error = "上游超过 12 秒没有画面，判定为断流"
                    reason = "stalled"
                    break
                last_activity = now

                try:
                    chunk = resp.read(16384)
                except Exception as e:
                    self.last_error = f"读取中断: {e}"
                    reason = "read_fail"
                    break
                if not chunk:
                    self.last_error = "板端关闭了连接"
                    reason = "closed"
                    break

                buf += chunk
                buf, got = self._extract_frames(buf, marker)
                if got:
                    last_frame_seen = time.time()
                    last_activity = last_frame_seen
                if len(buf) > 1_000_000:
                    buf = b""
        finally:
            try:
                resp.close()
            except Exception:
                pass
        return reason

    def _extract_frames(self, buf: bytes, marker: bytes):
        """从 MJPEG 流里切出 JPEG 帧。返回 (剩余未处理数据, 是否解出帧)"""
        got = False
        while True:
            i = buf.find(marker)
            if i < 0:
                # 还没走到帧边界，保留尾部一小段防止边界被截断
                keep = len(marker) + 4
                return (buf[-keep:] if len(buf) > keep else buf), got
            rest = i + len(marker)
            hdr_end = buf.find(b"\r\n\r\n", rest)
            if hdr_end < 0:
                return buf[i:], got
            header = buf[rest:hdr_end].decode("latin-1", "ignore")
            body = hdr_end + 4
            mlen = re.search(r"Content-Length:\s*(\d+)", header, re.I)
            if not mlen:
                buf = buf[body:]
                continue
            n = int(mlen.group(1))
            if len(buf) < body + n:
                # 数据还没收全，等下一轮
                return buf[i:], got
            frame = buf[body:body + n]
            buf = buf[body + n:]
            if frame[:3] == b"\xff\xd8\xff":
                got = True
                self.frames_total += 1
                self.last_frame_at = time.time()
                self._emit(frame)

    def _emit(self, frame: bytes):
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(frame)
            except queue.Full:
                # 队列满 = 这个观看者网络慢。丢掉旧帧保最新，绝不能让帧堆积成延迟
                try:
                    q.get_nowait()
                    q.put_nowait(frame)
                except queue.Empty:
                    pass
            except Exception:
                pass

    def _wake_all(self):
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(None)   # None = 哨兵，表示上游结束
            except Exception:
                pass

    # ---------- 对外状态 ----------
    def get_status(self) -> dict:
        with self._lock:
            n = len(self._subs)
        age = round(time.time() - self.last_frame_at, 2) if self.last_frame_at else None
        url = self._board_url_fixed or self.board_url_resolved or self._latest_ip()
        pumping = bool(self._pump and self._pump.is_alive())
        return {
            "state": self.state,
            "viewers": n,
            "frames_total": self.frames_total,
            "last_frame_age": age,
            "board_stream": f"http://{url}:81/stream" if url and not url.startswith("http") else url,
            "error": self.last_error,
            "upstream_running": pumping,
            "reconnects": self.reconnects,
            "state_since": round(time.time() - (self.last_frame_at or time.time()), 2),
        }
