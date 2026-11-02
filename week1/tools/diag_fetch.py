#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断页面的 TypeError: Failed to fetch

思路：在页面脚本执行之前注入钩子，把每一次 fetch / XHR 的结果都记下来，
包括被浏览器拦掉、根本没到服务器的那种。这样能分清是
  (a) 服务器没通 / 断连
  (b) 浏览器代理拦了
  (c) 某个接口报错
而不是只看一句 Failed to fetch 瞎猜。

用法：
    python tools/diag_fetch.py                  # 走系统代理（模拟用户浏览器）
    python tools/diag_fetch.py --no-proxy       # 直连
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from browser_test import WS, find_browser, find_page_target, js, wait_devtools  # noqa: E402

INJECT = r"""
(function(){
  if (window.__diag) return;
  var d = window.__diag = {errs: [], reqs: [], ok: 0};
  window.addEventListener('error', function(e){
    d.errs.push({t: 'error', msg: String(e.message || ''), where: String(e.filename || '') + ':' + (e.lineno || '')});
  }, true);
  window.addEventListener('unhandledrejection', function(e){
    var r = e.reason;
    d.errs.push({t: 'reject', msg: String((r && r.message) || r)});
  });
  var of = window.fetch;
  window.fetch = function(input, init){
    var url = (typeof input === 'string') ? input : ((input && input.url) || '');
    var t0 = performance.now();
    return of.apply(this, arguments).then(function(r){
      d.ok++;
      if (!r.ok) d.reqs.push({url: url, status: r.status, ms: Math.round(performance.now() - t0)});
      return r;
    }).catch(function(err){
      d.reqs.push({url: url, status: 'FETCH_FAIL',
                   err: String((err && err.name) || '') + ': ' + String((err && err.message) || err),
                   ms: Math.round(performance.now() - t0)});
      throw err;
    });
  };
  var oo = XMLHttpRequest.prototype.open, osend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(m, u){ this.__u = u; return oo.apply(this, arguments); };
  XMLHttpRequest.prototype.send = function(){
    var self = this, u = this.__u;
    this.addEventListener('error', function(){ d.reqs.push({url: u, status: 'XHR_FAIL'}); });
    this.addEventListener('load', function(){ if (self.status >= 400) d.reqs.push({url: u, status: self.status}); });
    return osend.apply(this, arguments);
  };
})();
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000/")
    ap.add_argument("--port", type=int, default=9333)
    ap.add_argument("--wait", type=float, default=10.0)
    ap.add_argument("--no-proxy", action="store_true")
    args = ap.parse_args()

    browser = find_browser()
    profile = tempfile.mkdtemp(prefix="diag_")
    cmd = [browser, "--headless=new", f"--remote-debugging-port={args.port}",
           f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
           "--disable-gpu", "--window-size=1280,1000", "about:blank"]
    if args.no_proxy:
        cmd.insert(1, "--no-proxy-server")
    print(f"浏览器: {browser}")
    print(f"页面  : {args.url}")
    print(f"代理  : {'BYPASS (--no-proxy-server)' if args.no_proxy else '跟随系统设置'}")

    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ver = wait_devtools(args.port)
        print(f"内核  : {ver.get('Browser')}\n")
        tgt = None
        for _ in range(40):
            tgt = find_page_target(args.port, "about:blank")
            if tgt:
                break
            time.sleep(0.3)
        if not tgt:
            raise SystemExit("没找到标签页")

        ws = WS(tgt["webSocketDebuggerUrl"])
        ws.call("Page.enable")
        ws.call("Runtime.enable")
        ws.call("Page.addScriptToEvaluateOnNewDocument", {"source": INJECT})
        ws.call("Page.navigate", {"url": args.url})
        js(ws, "new Promise(r=>setTimeout(()=>r(1), %d))" % int(args.wait * 1000), timeout=args.wait + 20)

        d = js(ws, "JSON.stringify(window.__diag || {errs:[],reqs:[],ok:0})")
        d = json.loads(d)
        state = js(ws, """(()=>{
          const t=(id)=>{const e=document.getElementById(id);return e?e.textContent.trim():null};
          return {title: document.title, pill: t('pill'), dev: t('dev'), mtime: t('mtime')};
        })()""")

        print(f"页面标题: {state['title']}")
        print(f"状态标签: {state['pill']}   设备: {state['dev']}   末次时间: {state['mtime']}")
        print(f"成功请求: {d['ok']} 条")
        print(f"\n失败请求 {len(d['reqs'])} 条：")
        for r in d["reqs"]:
            print(f"  - {r.get('status')} {r.get('url')} {r.get('err','') or ''} ({r.get('ms','?')}ms)")
        print(f"\nJS 异常 {len(d['errs'])} 条：")
        for e in d["errs"]:
            print(f"  - [{e['t']}] {e['msg']} {e.get('where','')}")
        if not d["reqs"] and not d["errs"]:
            print("\n>>> 这轮没有失败请求、也没有异常。")
        ws.close()
    finally:
        proc.terminate()


if __name__ == "__main__":
    main()
