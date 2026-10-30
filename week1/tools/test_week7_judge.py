#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""第7周 · 画面可用性判定与保留策略（离线单元测试，不碰服务、不碰板子）

为什么要单独测这两块：
  · 「镜头遮挡/画面不可用」是任务卡点名的四种反馈之一，但真去遮镜头很难复现，
    所以用合成图确定性地覆盖「全黑」「纯色」「太小」「不是 JPEG」四条分支。
  · 「设置最少保留」要么删多了（连上次成功都没了），要么删少了（磁盘涨）。
    它属于清理逻辑，平时不会有人盯着，必须用测试锁住行为。

用法：python tools/test_week7_judge.py
"""

import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server"))
import snapshots as sn  # noqa: E402

OK = True


def check(name, cond, detail=""):
    global OK
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + str(detail)) if detail else ""))
    OK = OK and bool(cond)


def jpeg(color, size=(320, 240), quality=80):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    print("=== A. 画面可用性判定（_judge）===")
    cases = [
        ("全黑 → too_dark", jpeg((0, 0, 0)), "too_dark"),
        ("纯色灰 → flat_frame", jpeg((128, 128, 128)), "flat_frame"),
        ("正常渐变 → ok", None, "ok"),
        ("太小 → too_small", jpeg((120, 120, 120), size=(80, 60)), "too_small"),
        ("不是 JPEG → bad_jpeg", b"this is definitely not a jpeg" * 200, "bad_jpeg"),
    ]

    # 正常图：用一张真抓帧（第1周的板载摄像头图像），不用合成图自欺
    real = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "debug_logs", "cam_frame.jpg")
    real_bytes = open(real, "rb").read() if os.path.exists(real) else None

    for name, data, want in cases:
        if data is None:
            if real_bytes is None:
                print("  [SKIP] 正常图：找不到 cam_frame.jpg")
                continue
            data = real_bytes
        r = sn._judge(data)
        got = r.get("status")
        extra = ""
        if r.get("mean_luma") is not None:
            extra = "luma=%.1f std=%.1f" % (r.get("mean_luma"), r.get("stdev"))
        check(name, got == want, "status=%s %s" % (got, extra))

    print("\n=== B. 阈值边界 ===")
    r = sn._judge(real_bytes) if real_bytes else {}
    if r:
        check("真帧被判为可用", r.get("usable") is True, r.get("reason", ""))
        check("真帧有尺寸", (r.get("width") or 0) > 0 and (r.get("height") or 0) > 0,
              "%sx%s" % (r.get("width"), r.get("height")))
    # 只有 SQOI 头、后面没有有效数据 —— 按新顺序应判 bad_jpeg（解不开），
    # 而不是 too_small（那是"解开了但分辨率不够"）
    tiny = b"\xff\xd8" + b"0" * 100
    check("头不完整的字节流判 bad_jpeg", sn._judge(tiny).get("status") == "bad_jpeg",
          sn._judge(tiny).get("status"))
    # 反过来：能解开但分辨率很低 → too_small
    lowres = jpeg((120, 120, 120), size=(100, 80))
    print("     低分辨率图 %d 字节" % len(lowres))
    check("能解开但分辨率低判 too_small", sn._judge(lowres).get("status") == "too_small",
          sn._judge(lowres).get("status"))

    print("\n=== C. 保留策略（_prune）===")
    import tempfile
    tmp = tempfile.mkdtemp(prefix="snap_")
    store = sn.SnapshotStore(tmp)
    # 造 MAX_KEEP + 5 条：中间有一条成功，最后一条也成功
    total = sn.MAX_KEEP + 5
    for i in range(total):
        sid = "S-%03d" % i
        fname = sid + ".jpg"
        with open(os.path.join(store.dir, fname), "wb") as f:
            f.write(b"x")
        status = "ok" if i in (10, total - 1) else "timeout"
        store.add({"id": sid, "file": fname, "status": status,
                   "source": "device", "captured_at": "t%d" % i})
    files = [f for f in os.listdir(store.dir) if f.endswith(".jpg")]
    print("     写入 %d 条，磁盘上剩 %d 个文件" % (total, len(files)))
    check("文件数不超过 MAX_KEEP", len(files) <= sn.MAX_KEEP, len(files))
    check("最近一张成功图仍在", (("S-%03d.jpg" % (total - 1)) in files))
    recs = store.all(200)
    kept_ok = [r for r in recs if r.get("status") == "ok" and not r.get("pruned")]
    check("成功记录没有被全部清掉", len(kept_ok) >= 1, [r["id"] for r in kept_ok])

    print("\n=== D. last_ok 只认设备采集 ===")
    tmp2 = tempfile.mkdtemp(prefix="snap2_")
    s2 = sn.SnapshotStore(tmp2)
    s2.add({"id": "R1", "status": "ok", "source": "replay", "file": ""})
    check("只有回放时 last_ok 为空", s2.last_ok() is None)
    s2.add({"id": "D1", "status": "ok", "source": "device", "file": ""})
    check("有设备采集后 last_ok 指向它", (s2.last_ok() or {}).get("id") == "D1")

    print("\n结果：" + ("全部通过" if OK else "有失败项"))
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
