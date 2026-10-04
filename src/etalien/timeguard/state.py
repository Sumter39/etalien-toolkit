# -*- coding: utf-8 -*-
"""守护进程的落盘路径、日志与运行状态。

状态文件只给 --status 看，不参与任何判定 —— 判活一律现算，见 service.cmd_status()。
"""
import json
import os
import threading

from .. import OUT_DIR
from ..logs import make

LOG_FILE = os.path.join(OUT_DIR, "guard.log")
PID_FILE = os.path.join(OUT_DIR, "guard.pid")
STATE_FILE = os.path.join(OUT_DIR, "guard_state.json")
LOG_MAX = 2 * 1024 * 1024

log = make(lambda: LOG_FILE, max_bytes=LOG_MAX)

# poll_loop 跑在子线程里，主线程退出（消息循环结束）时靠它把轮询也叫停。
STOP = threading.Event()

_STATE = {"started": None, "last_event": None, "last_result": None, "cred_ok": None}


def save_state():
    try:
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(_STATE, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
