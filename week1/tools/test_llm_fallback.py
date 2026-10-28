#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""第4周 · 备用路径自检：模型侧失败时，系统的反应是否正确。

任务卡原文（第4周·备用路径）：
    「服务异常时使用结构化样例定位解析问题；真实模型调用在校内服务或共享站补验。」

这个脚本跑两类断言：
  A. 四种模型侧失败被**分开归类**（model_not_found / model_unreachable /
     invalid_json / model_error）—— 归类不同，修法完全不同。
  B. 失败时**没有任何动作被执行**：actions_taken 为空、工具没被调用。

其中两个用例是**真实故障注入**（真的去连一个不存在的模型 / 不存在的端口），
不是模拟异常文本 —— 因为"我自己编的异常字符串"和"环境真的坏了"不是一回事。

用法：
    python tools/test_llm_fallback.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))

from nl_agent import LLMClient, NLAgent  # noqa: E402


class FakeLLM:
    """把指定的异常原样抛出，用来固定住"异常文本 → 归类"这一步。"""

    def __init__(self, exc):
        self.exc = exc
        self.called = 0

    def chat_json(self, system, user):
        self.called += 1
        raise self.exc

    def public_info(self):
        return {"host": "fake", "model": "fake", "protocol": "fake",
                "has_key": False, "timeout_s": 0}


class SpyAgent(NLAgent):
    """只测降级路径，不需要库/指令/设备状态。凡走到工具调用就记一笔。"""

    def __init__(self, llm):
        self.llm = llm
        self.tool_calls = []

    def read_latest(self, device_id=None):
        self.tool_calls.append(("read_latest", device_id))
        raise AssertionError("降级路径不该调用 read_latest")

    def request_recollect(self, *a, **kw):
        self.tool_calls.append(("request_recollect", a))
        raise AssertionError("降级路径不该调用 request_recollect")


def run(agent, text="查看上次数据"):
    return agent.handle(text)


def check(name, cond, detail=""):
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + detail) if detail else ""))
    return bool(cond)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ok = True
    print("=== A. 四种失败是否被分开归类 ===")

    cases = [
        ("model_not_found", ValueError("HTTP Error 404: Not Found")),
        ("model_unreachable", ValueError("Connection refused (os error 10061)")),
        ("invalid_json", ValueError("模型没有返回 JSON：'我觉得可以'")),
        ("model_error", ValueError("something odd happened")),
    ]
    for want, exc in cases:
        ag = SpyAgent(FakeLLM(exc))
        res = run(ag)
        got = res.get("error_kind")
        ok &= check("%-18s → %s" % (want, got), got == want, "" if got == want else "期望 " + want)
        # 每一种都必须声明没动作
        ok &= check("%-18s actions_taken 为空" % want, res.get("actions_taken") == [])
        ok &= check("%-18s 没碰工具" % want, ag.tool_calls == [] and res.get("tool") is None)
        # 结构化样例必须是**具体实例**，不能是占位符
        s = res.get("parse_sample") or {}
        ok &= check("%-18s 样例字段齐全且无占位符" % want,
                    set(s) == {"intent", "device_id", "samples", "interval_ms", "reason"}
                    and s["intent"] in ("query_latest", "request_recollect", "unsupported", "unclear")
                    and s["device_id"] == "" and s["samples"] is None and s["interval_ms"] is None,
                    json.dumps(s, ensure_ascii=False)[:80])

    print()
    print("=== B. 真实故障注入（真的连错的东西，不是编异常文本）===")

    # B1 真 404：服务在，模型名不存在
    real404 = LLMClient(base_url="http://127.0.0.1:11434",
                        model="no-such-model-xyz", protocol="ollama", timeout=20)
    ag = SpyAgent(real404)
    res = run(ag)
    ok &= check("真·模型不存在 → %s" % res.get("error_kind"),
                res.get("error_kind") == "model_not_found")
    ok &= check("真·模型不存在 actions_taken 为空", res.get("actions_taken") == [])

    # B2 真连不上：端口没人监听
    dead = LLMClient(base_url="http://127.0.0.1:11439",
                     model="qwen3.8-q2", protocol="ollama", timeout=10)
    ag = SpyAgent(dead)
    res = run(ag)
    ok &= check("真·端口不通 → %s" % res.get("error_kind"),
                res.get("error_kind") == "model_unreachable")
    ok &= check("真·端口不通 actions_taken 为空", res.get("actions_taken") == [])

    print()
    print("=== C. 一次真实失败的人话输出 ===")
    ag = SpyAgent(LLMClient(base_url="http://127.0.0.1:11434",
                            model="no-such-model-xyz", protocol="ollama", timeout=20))
    res = run(ag)
    print(res.get("reply", ""))

    print()
    print("结果：" + ("全部通过" if ok else "有失败项 —— 修掉再往下"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
