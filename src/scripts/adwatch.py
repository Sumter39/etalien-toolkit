#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""看广告领时长 —— 入口脚本。

开机自启项（EtalienDailyCheckin）指向本文件，用 venv 里的 pythonw.exe 启动 ——
venv 会自己把 site-packages 挂上（uiautomator2 在那），所以这里不需要手工引导。

用法：
    python src/scripts/adwatch.py                # 跑满今天额度
    python src/scripts/adwatch.py --rounds 3     # 只跑 3 轮
    python src/scripts/adwatch.py --dry-run      # 只探测界面，不点击
    python src/scripts/adwatch.py --state        # 打印每日状态
    python src/scripts/adwatch.py --install      # 装 / 卸开机自启（--uninstall 反操作）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from etalien.adwatch.config import log     # noqa: E402
from etalien.adwatch.runner import main    # noqa: E402

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("已被手动中断")
