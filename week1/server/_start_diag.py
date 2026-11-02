#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""定位 server.py 启动卡在哪一行

现象：进程在跑、CPU 几乎为 0、8000 端口不监听、日志一个字都没有。
这说明卡在某个阻塞调用上（网络/DNS/文件锁），而不是在算数据。

做法：用 faulthandler 在 N 秒后把当前所有线程的调用栈全部打出来 ——
哪个函数压在最上面，就是它卡住了。这比一句句加 print 快得多。
"""
from __future__ import annotations

import faulthandler
import os
import runpy
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)

wait = float(sys.argv[1]) if len(sys.argv) > 1 else 8.0
sys.argv = ["server.py", "--port", "8000", "--host", "0.0.0.0"]

print(f"[diag] 启动 server.py，{wait:.0f} 秒后 dump 调用栈", flush=True)
faulthandler.dump_traceback_later(wait, exit=True, file=sys.stdout)
runpy.run_path(os.path.join(HERE, "server.py"), run_name="__main__")
