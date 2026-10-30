#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""第7周 · 快照面板的界面逻辑（不启浏览器，跑的是页面里的真实代码）

背景：任务卡要求「实现采集中、采集成功、镜头遮挡/画面不可用和采集失败反馈」，
其中最容易做错的一条是——失败时不要把上一张图留在原地（用户会以为那是刚拍的）。

开真浏览器需要额外授权，所以这里换一种验证方式：
**把 index.html 里那段快照代码原样抽出来**，在 Node 里配一套假 DOM + 假 fetch，
直接把四种响应喂进去，断言界面上真正被写进去的文字与图片开关。

它证明什么：四种状态的判定分支、文案、图片显示/隐藏都按预期。
它不证明什么：CSS 布局、真实浏览器的事件绑定、图片真的能解码出来。
这两件事分开说，不含糊。

用法：python tools/test_week7_ui_logic.py
"""

import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(ROOT, "web", "index.html")
START = "/* ---------- 第7周：按需环境快照"
END = "/* ---------- 摄像头取流方式"


def find_node():
    for c in (r"C:/Users/user/.workbuddy/binaries/node/versions/22.22.2-6/node.exe", "node"):
        try:
            subprocess.run([c, "--version"], capture_output=True, check=True)
            return c
        except Exception:
            continue
    raise SystemExit("找不到 node")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    src = open(HTML, encoding="utf-8").read()
    i = src.index(START)
    j = src.index(END, i)
    block = src[i:j]
    print("  从 index.html 抽出快照代码 %d 行" % len(block.splitlines()))

    harness = r"""
// ---------------- 假 DOM ----------------
function makeEl(id) {
  return {
    id: id, textContent: '', innerHTML: '', className: '', hidden: false,
    src: '', disabled: false, _h: {},
    addEventListener(t, f) { this._h[t] = f; },
    removeAttribute(a) { if (a === 'src') this.src = ''; },
    querySelector() { return null; }
  };
}
const els = {};
global.document = { getElementById: (id) => (els[id] || (els[id] = makeEl(id))) };
global.window = {};
global.performance = { now: () => Date.now() };

// ---------------- 假 fetch ----------------
let RESP = {};   // 直接就是响应体，别包一层
let LIST_RESP = { snapshots: [] };
let STATE_AT_FETCH = '';   // 抓"请求发起那一刻"的状态，用来验证「采集中」这个中间态
global.fetch = async function (url) {
  if (String(url).indexOf('/api/snapshot/list') >= 0) {
    return { json: async () => LIST_RESP };
  }
  // 抓住发起瞬间的状态：此刻界面应当是「采集中…」，而不是还停在上一次的结果上
  STATE_AT_FETCH = (els['snapState'] || {}).textContent || '';
  return { json: async () => RESP };
};

// ---------------- 被测代码 ----------------
""" + block + r"""
globalThis.__take = takeSnapshot;
globalThis.__els = () => ({
  state: els['snapState'], img: els['snapImg'], ph: els['snapPlaceholder'],
  meta: els['snapMeta'], list: els['snapList']
});

// ---------------- 用例 ----------------
let ok = true;
function check(name, cond, detail) {
  console.log((cond ? '  [PASS] ' : '  [FAIL] ') + name + (detail ? '  ' + detail : ''));
  ok = ok && !!cond;
}
// 注意：被测代码在加载时就把元素引用抓走了（snapState = getElementById(...)），
// 所以这里**必须原地重置**，不能换新对象 —— 换了新对象，断言看的是另一批元素，
// 全都会"失败"，但那是测试自己的错（第一次就踩了）。
function fresh() {
  ['snapState', 'snapImg', 'snapPlaceholder', 'snapMeta', 'snapList'].forEach(function (k) {
    const e = els[k] || (els[k] = makeEl(k));
    e.textContent = ''; e.innerHTML = ''; e.className = '';
    e.src = ''; e.disabled = false;
    e.hidden = (k === 'snapImg');
  });
}

(async function () {
  // 1) 采集成功
  fresh();
  RESP = {
    id: 'SNAP-A', status: 'ok', is_this_attempt: true, source: 'device',
    width: 320, height: 240, captured_at: '2026-10-30 09:00:00',
    latency_ms: 812, bytes: 20123 };
  await globalThis.__take();
  let e = globalThis.__els();
  check('采集中：请求发起时界面已是「采集中…」', STATE_AT_FETCH === '采集中…', STATE_AT_FETCH);
  check('成功：状态写「采集成功」', e.state.textContent === '采集成功', e.state.textContent);
  check('成功：图片显示且指向本次 id', !e.img.hidden && e.img.src.indexOf('SNAP-A') >= 0, e.img.src);
  check('成功：写了拍摄时刻', e.meta.innerHTML.indexOf('2026-10-30 09:00:00') >= 0);
  check('成功：写了尺寸', e.meta.innerHTML.indexOf('320x240') >= 0);

  // 2) 画面不可用（遮挡）—— 仍应显示这张"不能用的图"
  fresh();
  RESP = {
    id: 'SNAP-B', status: 'too_dark', is_this_attempt: false, source: 'device',
    reason: '平均亮度 3.2 < 12，镜头可能被遮挡或全黑' };
  await globalThis.__take();
  e = globalThis.__els();
  check('遮挡：状态写「画面不可用」', e.state.textContent === '画面不可用', e.state.textContent);
  check('遮挡：显示这张不可用的图（不是留空的）', !e.img.hidden && e.img.src.indexOf('SNAP-B') >= 0);
  check('遮挡：给出亮度等具体原因', e.meta.innerHTML.indexOf('平均亮度') >= 0);

  // 3) 采集失败，但有上次成功 —— 必须标"不是本次"
  fresh();
  RESP = {
    id: 'SNAP-C', status: 'timeout', is_this_attempt: false, source: 'device',
    reply: '板子没在规定时间内回画面，**这次没拍到**。',
    last_ok: { id: 'SNAP-OLD', captured_at: '2026-10-30 08:54:47',
               age_seconds: 320, is_this_attempt: false } };
  await globalThis.__take();
  e = globalThis.__els();
  check('失败：状态写「采集失败」', e.state.textContent === '采集失败', e.state.textContent);
  check('失败：显示的是上次成功那张', e.img.src.indexOf('SNAP-OLD') >= 0, e.img.src);
  check('失败：明确写「不是本次采集的结果」', e.meta.innerHTML.indexOf('不是本次采集的结果') >= 0);
  check('失败：带上旧图的拍摄时间', e.meta.innerHTML.indexOf('08:54:47') >= 0);

  // 4) 采集失败，且从来没有成功过 —— 画面区必须清空
  fresh();
  RESP = {
    id: 'SNAP-D', status: 'unreachable', is_this_attempt: false, source: 'device',
    reply: '连不上板子，**这次没拍到**。' };
  await globalThis.__take();
  e = globalThis.__els();
  check('无历史：状态写「采集失败」', e.state.textContent === '采集失败');
  check('无历史：图片被隐藏（不留旧图）', e.img.hidden === true);
  check('无历史：占位文字说明没有画面', e.ph.textContent.indexOf('没有拿到画面') >= 0, e.ph.textContent);

  // 5) 记录列表：回放与成功要能分开
  fresh();
  LIST_RESP = { snapshots: [
    { id: 'S1', status: 'ok', source: 'device', captured_at: '2026-10-30 09:00:00', width: 320, height: 240 },
    { id: 'S2', status: 'ok', source: 'replay', captured_at: '2026-10-30 08:54:47', width: 320, height: 240 },
    { id: 'S3', status: 'too_dark', source: 'device', captured_at: '2026-10-30 08:50:00' }
  ] };
  await loadSnapList();
  e = globalThis.__els();
  check('列表：成功与回放区分显示',
        e.list.innerHTML.indexOf('成功') >= 0 && e.list.innerHTML.indexOf('回放') >= 0);
  check('列表：标出「历史图像」', e.list.innerHTML.indexOf('历史图像') >= 0);
  check('列表：不可用的状态也在列表里', e.list.innerHTML.indexOf('too_dark') >= 0);

  console.log('\n结果：' + (ok ? '全部通过' : '有失败项'));
  process.exit(ok ? 0 : 1);
})();
"""

    tmp = tempfile.mkdtemp(prefix="w7ui_")
    js_path = os.path.join(tmp, "ui_test.js")
    with open(js_path, "w", encoding="utf-8") as f:
        f.write(harness)

    node = find_node()
    p = subprocess.run([node, js_path], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    print(p.stdout.rstrip())
    if p.returncode != 0:
        print(p.stderr[-1200:])
        print("\n结果：有失败项")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
