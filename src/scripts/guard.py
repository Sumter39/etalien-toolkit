#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""时长守护 —— 入口脚本。

开机自启项（EtalienTimeGuard）指向本文件，必须用基础解释器的 pythonw.exe 启动
（原因见 service._launch_cmdline）。基础解释器直连少了 venv 转发器那一环，
pywin32 的 .pth 不会被执行 —— 它负责把 win32/ 子目录和 DLL 目录注册进来，
所以这里必须用 site.addsitedir 手工挂上 venv 的包目录（sys.path.insert 不算数）。

用法：
    pythonw src/scripts/guard.py             # 常驻（由注册表 Run 键拉起，无窗口）
    python  src/scripts/guard.py --status    # 运行状态 + 剩余时长
    python  src/scripts/guard.py --pause-now # 立刻暂停一次
    python  src/scripts/guard.py --stop      # 停掉
"""
import json
import os
import site
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

try:
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as _f:
        _venv = json.load(_f).get("venv")
    if _venv:
        site.addsitedir(os.path.join(_venv, "Lib", "site-packages"))
except Exception:
    pass

from etalien.timeguard.service import main   # noqa: E402

if __name__ == "__main__":
    main()
