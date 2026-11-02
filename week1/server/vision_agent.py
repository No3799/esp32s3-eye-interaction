"""第 8 周：把视觉推理结果变成可理解反馈

任务卡原文要点（逐条对着写，不是概括）：

- 「讲解预测标签、分数、拒识及模型版本；本轮以**画面可用性/遮挡识别**为共同保底任务，
  场景粗分类作为拓展；接入真实模型推理。」
- 「实现结构化结果解析、低分/未知反馈、重新采集和人工纠正；保留输入、结果和反馈三者对应，
  比较高分误判与正常样例。」
- 当堂验证：「**未经校准的模型分数不写成现实正确率**；场景类别不转换为精确位置或人身安全判断。」

所以这个模块有三条硬约束，代码里都能看出来：

1. **分数只叫 score，永远不叫 accuracy**，并且带 `score_kind="self_reported"`。
   模型自报的置信度不是校准过的概率 —— 这一条在返回值、接口文档、页面上三处都标了。
2. **场景标签只用于描述"看到什么"**，不参与任何位置推断或安全判断。
   返回值里没有坐标系字段，也不允许下游拼接出位置。
3. **拒识是一等公民**：模型说"说不准"、输出解析不出来、超时，三种情况都返回
   `label="unknown"` + `rejected=true`，而不是硬给一个标签。

设计取舍：这里不训练模型，直接调本地 Ollama 的视觉模型。
任务卡允许「教师基线与学生候选模型使用相同接口」，本项目按备用路径走 ——
用现成的真实模型跑真推理，接口留成可替换的（`NL_VISION_MODEL` 环境变量）。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request

DEFAULT_BASE = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen2.5vl:3b"
# 低于这个分数就算"不确定"，界面会建议重拍。0.6 是拍的，不是标定的 —— 见 README 说明。
UNCERTAIN_BELOW = 0.6

SYSTEM_PROMPT = """你是一个只看画面能不能用的检查员。只做两件事：

1. 判断这张照片**能不能看清东西**（画面可用性）：
   - "usable"：能看到清晰的物体或场景
   - "blocked"：被手指、纸片、墙面等大面积遮挡，或者几乎全黑/全白/纯色
   - "unclear"：有画面但很模糊，看不清是什么
2. 如果能看清，用**不超过 12 个字**说看到的是什么大类（例如"桌面和键盘""室内墙面""手掌"）。

严格要求：
- 不判断位置、不判断危险、不猜测人的身份或情绪
- confidence 填你自己对这个判断的把握（0 到 1 的小数），拿不准就填低一点
- 只输出 JSON，不要解释、不要代码块

输出格式：
{"usability":"usable|blocked|unclear","scene":"...","confidence":0.0,"reason":"不超过20字"}
"""


def _strip_code_fence(s: str) -> str:
    s = s.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    return s.strip()


class VisionAgent:
    """对一张 JPEG 做「画面可用性」推理。纯标准库，不依赖 torch。"""

    def __init__(self, base_url: str | None = None, model: str | None = None,
                 timeout: float | None = None):
        self.base_url = (base_url or os.environ.get("NL_VISION_BASE_URL")
                         or os.environ.get("NL_LLM_BASE_URL") or DEFAULT_BASE).rstrip("/")
        self.model = model or os.environ.get("NL_VISION_MODEL") or DEFAULT_MODEL
        self.timeout = float(timeout or os.environ.get("NL_VISION_TIMEOUT") or 90)
        self._info_cache: dict | None = None
        self._info_at = 0.0

    # ---------- 模型元信息（任务卡要求"模型版本"） ----------
    def model_info(self, refresh: bool = False) -> dict:
        now = time.time()
        if not refresh and self._info_cache and now - self._info_at < 60:
            return self._info_cache
        info = {"name": self.model, "available": False, "digest": None,
                "family": None, "parameter_size": None, "quantization": None,
                "error": None}
        try:
            with urllib.request.urlopen(self.base_url + "/api/tags", timeout=8) as r:
                tags = json.loads(r.read().decode())
            for m in tags.get("models", []):
                if m.get("name") == self.model or m.get("model") == self.model:
                    info.update({
                        "available": True,
                        "digest": (m.get("digest") or "")[:12] or None,
                        "family": m.get("details", {}).get("family"),
                        "parameter_size": m.get("details", {}).get("parameter_size"),
                        "quantization": m.get("details", {}).get("quantization_level"),
                        "size_bytes": m.get("size"),
                    })
                    break
            else:
                info["error"] = f"模型 {self.model} 未安装（ollama pull {self.model}）"
        except Exception as e:
            info["error"] = f"连不上语言服务：{type(e).__name__}"
        self._info_cache, self._info_at = info, now
        return info

    def available(self) -> bool:
        return bool(self.model_info().get("available"))

    # ---------- 推理 ----------
    def infer(self, jpeg: bytes, snapshot_id: str = "") -> dict:
        """返回结构化结果。任何失败都走 rejected=true，不硬编一个标签。"""
        t0 = time.time()
        info = self.model_info()
        out = {
            "snapshot_id": snapshot_id,
            "input_sha256": hashlib.sha256(jpeg).hexdigest()[:16],
            "input_bytes": len(jpeg),
            "model": {
                "name": self.model,
                "digest": info.get("digest"),
                "family": info.get("family"),
                "parameter_size": info.get("parameter_size"),
                "quantization": info.get("quantization"),
            },
            "score_kind": "self_reported",   # ← 永远不是 accuracy
            "label": "unknown",
            "score": None,
            "uncertain": True,
            "rejected": True,
            "scene": None,
            "reason": "",
            "raw": "",
            "latency_ms": None,
            "error": None,
        }

        if not info.get("available"):
            out["error"] = info.get("error") or "视觉模型不可用"
            out["latency_ms"] = int((time.time() - t0) * 1000)
            return out

        body = {
            "model": self.model,
            "prompt": SYSTEM_PROMPT + "\n\n现在看这张图片，只输出 JSON。",
            "images": [base64.b64encode(jpeg).decode()],
            "stream": False,
            "format": "json",
            "options": {"temperature": 0, "num_predict": 160},
        }
        try:
            req = urllib.request.Request(
                self.base_url + "/api/generate",
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                resp = json.loads(r.read().decode())
            out["raw"] = (resp.get("response") or "")[:800]
        except Exception as e:
            out["error"] = f"推理请求失败：{type(e).__name__}: {str(e)[:120]}"
            out["latency_ms"] = int((time.time() - t0) * 1000)
            return out

        # 解析结构化结果
        try:
            data = json.loads(_strip_code_fence(out["raw"]))
        except Exception:
            out["error"] = "模型输出不是合法 JSON，按拒识处理"
            out["latency_ms"] = int((time.time() - t0) * 1000)
            return out

        usability = str(data.get("usability") or "").strip().lower()
        if usability not in ("usable", "blocked", "unclear"):
            out["error"] = f"模型给了无法识别的可用性取值：{usability[:20]!r}"
            out["latency_ms"] = int((time.time() - t0) * 1000)
            return out

        try:
            conf = float(data.get("confidence"))
        except (TypeError, ValueError):
            conf = None
        if conf is not None:
            conf = max(0.0, min(1.0, conf))

        out.update({
            "label": usability,
            "score": conf,
            "scene": (str(data.get("scene") or "").strip() or None),
            "reason": (str(data.get("reason") or "").strip())[:60],
            "rejected": False,
            # 不确定有三种来源：模型自己说 unclear、分数低于阈值、没给分数
            "uncertain": (usability == "unclear") or (conf is None) or (conf < UNCERTAIN_BELOW),
            "latency_ms": int((time.time() - t0) * 1000),
        })
        return out

    # ---------- 给页面用的人话 ----------
    @staticmethod
    def humanize(result: dict) -> dict:
        """把结构化结果翻成"看到了什么 / 还不确定什么"，供界面直接显示。

        两件事在这里被强制分开：
        · 看到什么 —— 来自 label + scene
        · 不确定什么 —— 来自 uncertain + reason
        分数只作为"模型自报的把握"，措辞里不出现"准确率""正确率"。
        """
        if result.get("rejected"):
            return {
                "head": "这次没判出来",
                "detail": result.get("error") or "模型没能给出可用的判断",
                "confidence_text": "—",
                "advice": "重拍一张，或者换个角度再试",
            }
        label = result.get("label")
        scene = result.get("scene")
        conf = result.get("score")
        conf_text = "—" if conf is None else f"模型自报 {conf:.2f}（未校准）"

        if label == "usable":
            head = f"看到：{scene}" if scene else "看到画面，能看清东西"
            detail = result.get("reason") or ""
            advice = "" if not result.get("uncertain") else "但模型自己也不太确定，可以再拍一张对照"
        elif label == "blocked":
            head = "看到：画面被挡住了" + (f"（{scene}）" if scene else "")
            detail = result.get("reason") or ""
            advice = "把镜头前的遮挡物移开，再拍一张"
        else:  # unclear
            head = "看到：有画面，但看不清是什么"
            detail = result.get("reason") or ""
            advice = "凑近一点、或者换个光线更好的角度重拍"

        if result.get("uncertain") and not advice:
            advice = "模型不太确定，建议重拍一张对照"
        return {"head": head, "detail": detail, "confidence_text": conf_text, "advice": advice}
