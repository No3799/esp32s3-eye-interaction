#!/usr/bin/env python3
"""第6周 · 有限多轮上下文：记住"上一轮问了什么"，好接住一句短回答。

为什么只需要记"一个问题"，而不是整段对话：

  用户说「看看数据吧」→ 系统问「你是想看已有记录，还是让设备再采一次？」
  → 用户回「查看」。这时要的不是聊天历史，而是**那一句封闭式问题**。
  记下问题 + 可选答案，就够把「查看」落到 query_latest 上。

所以这里的"有限"是三层都有限：
  · 每个会话只留 **1 条**待答问题（不是完整 transcript）
  · **TTL 180 秒**：过了就当没问过 —— 半小时后说"查看"不该接上上一轮
  · 最多 **200 个会话**，超了按最久未用淘汰（内存不随访问量增长）

为什么要 TTL 而不是永久记：上下文过期是一种**保护**。
一句"好"如果永远能接到上一个问题上，系统就会对莫名其妙的话做出动作。
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict


class TurnContext:
    def __init__(self, ttl: float = 180.0, max_sessions: int = 200):
        self.ttl = ttl
        self.max_sessions = max_sessions
        self._d = OrderedDict()          # sid -> (ts, pending)
        self._lock = threading.Lock()

    def set_pending(self, sid: str, pending: dict) -> None:
        if not sid:
            return
        with self._lock:
            self._d[sid] = (time.time(), dict(pending))
            self._d.move_to_end(sid)
            while len(self._d) > self.max_sessions:
                self._d.popitem(last=False)

    def get_pending(self, sid: str) -> dict | None:
        if not sid:
            return None
        with self._lock:
            item = self._d.get(sid)
            if not item:
                return None
            ts, pending = item
            if time.time() - ts > self.ttl:
                self._d.pop(sid, None)      # 过期即删，不留给下一次误接
                return None
            self._d.move_to_end(sid)
            return dict(pending)

    def clear(self, sid: str) -> None:
        with self._lock:
            self._d.pop(sid, None)

    def stats(self) -> dict:
        with self._lock:
            return {"sessions": len(self._d), "ttl_s": self.ttl}
