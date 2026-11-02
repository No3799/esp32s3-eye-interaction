"""第 8 周：视觉推理结果的存储（输入 / 结果 / 反馈 三者对应）

任务卡要求「保留输入、结果和反馈三者对应」。所以一条记录里必须能同时看到：
- **输入**：哪张快照（snapshot_id + 输入文件的 sha256 前 16 位）
- **结果**：模型给的标签、分数、模型版本（含 digest）
- **反馈**：人有没有纠正、纠正成什么（可以没有 —— "没纠正"本身也是一种反馈）

存法沿用项目的 jsonl 追加式（和 snapshots 一样）：结果一条、反馈一条，
读取时按 id 合并。好处是**反馈不会覆盖原始结果** ——
如果直接把 corrected_label 写回原记录，就再也看不到"模型原来怎么说的"了，
而"模型错了什么"恰恰是第 8 周要比较的东西。
"""
from __future__ import annotations

import json
import os
import threading
import time


class VisionStore:
    def __init__(self, data_dir: str):
        self.dir = os.path.join(data_dir, "vision")
        os.makedirs(self.dir, exist_ok=True)
        self.index = os.path.join(self.dir, "index.jsonl")
        self._lock = threading.Lock()
        self._counters: dict[str, int] = {}

    # ---------- 写 ----------
    def _append(self, rec: dict) -> None:
        with self._lock:
            with open(self.index, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def _new_id(self) -> str:
        ms = int(time.time() * 1000)
        day = time.strftime("%Y%m%d", time.localtime(ms / 1000))
        with self._lock:
            n = self._counters.get(day, 0) + 1
            self._counters[day] = n
        return "VIS-%s-%03d" % (day, n)

    def add_result(self, snapshot_id: str, result: dict) -> dict:
        rec = {
            "type": "result",
            "id": self._new_id(),
            "snapshot_id": snapshot_id,
            "created_ms": int(time.time() * 1000),
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "result": result,
        }
        self._append(rec)
        return rec

    def add_feedback(self, vision_id: str, verdict: str,
                     corrected_label: str | None = None,
                     note: str = "") -> dict:
        """verdict: correct（判对了）/ wrong（判错了）/ skipped（没管）

        `wrong` 必须带 corrected_label —— 只说"错了"而不说"应该是什么"，
        对下一次推理没有任何帮助。
        """
        rec = {
            "type": "feedback",
            "id": vision_id,
            "created_ms": int(time.time() * 1000),
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "verdict": verdict,
            "corrected_label": corrected_label,
            "note": note[:200],
        }
        self._append(rec)
        return rec

    # ---------- 读 ----------
    def _load(self) -> tuple[dict[str, dict], dict[str, dict]]:
        results: dict[str, dict] = {}
        feedbacks: dict[str, dict] = {}
        if not os.path.exists(self.index):
            return results, feedbacks
        with open(self.index, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") == "result":
                    results[rec["id"]] = rec
                elif rec.get("type") == "feedback":
                    feedbacks[rec["id"]] = rec          # 后写的覆盖先写的
        return results, feedbacks

    def all(self, limit: int = 20) -> list[dict]:
        results, feedbacks = self._load()
        items = []
        for vid, rec in results.items():
            merged = dict(rec)
            fb = feedbacks.get(vid)
            merged["feedback"] = fb
            merged["corrected"] = bool(fb and fb.get("verdict") == "wrong")
            items.append(merged)
        items.sort(key=lambda r: r.get("created_ms", 0), reverse=True)
        return items[:limit]

    def get(self, vision_id: str) -> dict | None:
        results, feedbacks = self._load()
        rec = results.get(vision_id)
        if not rec:
            return None
        merged = dict(rec)
        merged["feedback"] = feedbacks.get(vision_id)
        return merged

    def stats(self) -> dict:
        """给第 8 周的"比较高分误判与正常样例"用：按是否纠正分组统计。"""
        items = self.all(limit=10000)
        total = len(items)
        wrong = sum(1 for r in items if r.get("corrected"))
        skipped = sum(1 for r in items if (r.get("feedback") or {}).get("verdict") == "skipped")
        corrected = [r for r in items if r.get("corrected")]
        return {
            "total": total,
            "corrected": wrong,
            "skipped": skipped,
            "unreviewed": total - wrong - skipped -
                          sum(1 for r in items if (r.get("feedback") or {}).get("verdict") == "correct"),
            "correct": sum(1 for r in items if (r.get("feedback") or {}).get("verdict") == "correct"),
            # 被纠正的那些，模型当时自报了多少分 —— 高分误判就藏在这里
            "wrong_scores": [
                {"id": r["id"], "snapshot_id": r.get("snapshot_id"),
                 "label": (r.get("result") or {}).get("label"),
                 "score": (r.get("result") or {}).get("score"),
                 "corrected_label": (r.get("feedback") or {}).get("corrected_label")}
                for r in corrected
            ],
        }
