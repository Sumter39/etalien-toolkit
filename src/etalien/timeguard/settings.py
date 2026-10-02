# -*- coding: utf-8 -*-
"""守护进程的运行参数。

只放调优类参数；跟机器绑定的（客户端进程名由命令行覆盖）不在这里写死。
"""
from ..config import get

CONFIG = {
    "process": "etalien.exe",       # 要盯的客户端进程
    "idle_seconds": get("idle_minutes") * 60,   # 键鼠空闲多久算「人走了」
    "poll_interval": 5,             # 轮询间隔（秒）
    "http_timeout": 5,              # 暂停请求超时
    "fast_timeout": 2,              # 关机/注销/睡眠路径专用：Windows 判「未响应」是 5 秒，
                                    # 用 5 秒超时正好贴边，会弹「此应用阻止关机」
    "pause_path": "/v2/account/update/pause/state",
    "watch_process": True,          # 盯客户端进程退出
    "watch_lock": True,             # 盯锁屏
    "watch_idle": True,             # 盯键鼠空闲
}
