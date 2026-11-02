"""第 9 周：用视觉事件触发一次主动询问

任务卡原文要点：

- 「讲解事件触发、反馈优先级与主动询问条件；将视觉结果接入原有任务状态，
  补上**确认、忽略与冷却时间**。」
- 当堂验证：「**误判能够纠正，用户拒绝后不反复打扰**；演示中说明模型、设备及服务实际运行位置。」

这个模块只干一件事：**决定现在要不要主动问一句"要重拍吗"**。

主动询问最容易犯的错是"太积极" —— 拍坏一次就问一次，用户会把它关掉，
关了之后就再也不会被提醒。所以这里有三道闸：

1. **只在画面真的不可用时才问**。usable 一律不问（问了就是打扰）。
2. **刚被忽略过就不再问**。同一张图同一个问题，问一次够了。
3. **说过"别再问"就进冷却**。冷却期内同类问题全部静音，到点自动恢复。

冷却用时间而不是次数：次数用完会永远静音，"别再问"就变成了"拆掉功能"。
"""
from __future__ import annotations

import json
import os
import threading
import time

# 冷却时长。10 分钟是个折中：短了像没记住你的选择，长了等于把功能关了。
COOLDOWN_S = 600
# 同一个 snapshot 被忽略过就在这个窗口内不再问
IGNORE_WINDOW_S = 1800


class ProactiveGuard:
    def __init__(self, data_dir: str, cooldown_s: int = COOLDOWN_S):
        self.dir = os.path.join(data_dir, "vision")
        os.makedirs(self.dir, exist_ok=True)
        self.path = os.path.join(self.dir, "proactive.jsonl")
        self.cooldown_s = cooldown_s
        self._lock = threading.Lock()

    # ---------- 读写 ----------
    def _append(self, rec: dict) -> None:
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def _load(self) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        out = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out

    # ---------- 状态 ----------
    def state(self) -> dict:
        now = time.time()
        evts = self._load()
        mute_until = 0.0
        ignored: dict[str, float] = {}
        for e in evts:
            if e.get("action") == "mute":
                mute_until = max(mute_until, (e.get("ms", 0) / 1000.0) + self.cooldown_s)
            elif e.get("action") == "ignore" and e.get("snapshot_id"):
                ignored[e["snapshot_id"]] = e.get("ms", 0) / 1000.0
        return {
            "muted": now < mute_until,
            "mute_until_ms": int(mute_until * 1000) if mute_until else 0,
            "mute_remain_s": max(0, int(mute_until - now)),
            "cooldown_s": self.cooldown_s,
            "recent": evts[-8:],
        }

    def should_ask(self, vision_result: dict, snapshot_id: str) -> tuple[bool, str]:
        """要不要主动问。返回 (问不问, 为什么)。理由要能给人看，不是内部枚举。"""
        st = self.state()
        if st["muted"]:
            return False, f"你刚说过别再问，还有 {st['mute_remain_s']} 秒才恢复"
        if vision_result.get("rejected"):
            # 模型自己都没判出来 —— 这恰恰说明画面对不上，值得问一句
            return True, "模型没判出这张图，需要你确认要不要重拍"

        label = vision_result.get("label")
        if label == "usable" and not vision_result.get("uncertain"):
            return False, "画面能用，不打扰"

        # 被忽略过的同一张，不再问
        now = time.time()
        for e in reversed(self._load()):
            if e.get("action") == "ignore" and e.get("snapshot_id") == snapshot_id:
                if now - e.get("ms", 0) / 1000.0 < IGNORE_WINDOW_S:
                    return False, "这张你已经忽略过了，不再问"

        if label == "blocked":
            return True, "画面被挡住了，问一下要不要重拍"
        if label == "unclear":
            return True, "画面看不清，问一下要不要重拍"
        if vision_result.get("uncertain"):
            return True, "模型不太确定这张画面，问一下要不要重拍"
        return False, "没有需要确认的事"

    # ---------- 记录 ----------
    def record(self, action: str, snapshot_id: str = "", vision_id: str = "",
               reason: str = "") -> dict:
        """action: retake（重拍）/ ignore（这次算了）/ mute（别再问）"""
        if action not in ("retake", "ignore", "mute"):
            raise ValueError("action 只能是 retake / ignore / mute")
        rec = {
            "action": action,
            "snapshot_id": snapshot_id,
            "vision_id": vision_id,
            "reason": reason[:120],
            "ms": int(time.time() * 1000),
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._append(rec)
        return rec
