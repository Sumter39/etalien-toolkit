# -*- coding: utf-8 -*-
"""落盘日志。

pythonw.exe 没有控制台，print 会丢，运行日志必须落盘 —— 这是事后排查的唯一线索。
格式统一成 [YYYY-MM-DD HH:MM:SS] 消息，超过上限就轮转成 .1。

每次写都重新开文件（不用常驻句柄）：进程被强杀时不丢缓冲区里的内容，而关机路径
上被杀是常态。
"""
import os
import sys
import time

MAX_BYTES = 2 * 1024 * 1024


def make(path, max_bytes=MAX_BYTES):
    """造一个 log(msg) 函数。

    path 可以传字符串，也可以传不带参数的可调用对象 —— 传后者是为了让调用方
    那侧的路径变量能被替换（测试会把日志重定向到临时目录）。
    """
    def log(msg):
        line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
        try:
            if sys.stdout is not None:
                print(line, flush=True)
        except Exception:
            pass
        try:
            target = path() if callable(path) else path
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if os.path.exists(target) and os.path.getsize(target) > max_bytes:
                bak = target + ".1"
                if os.path.exists(bak):
                    os.remove(bak)
                os.replace(target, bak)
            with open(target, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    return log
