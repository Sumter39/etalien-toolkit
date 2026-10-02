# -*- coding: utf-8 -*-
"""守护进程的落盘路径、日志与运行状态。

状态文件只给 --status 看，不参与任何判定 —— 判活一律现算，见 service.cmd_status()。
"""
import json
import os
import threading
from datetime import datetime

from .. import OUT_DIR
from ..logs import make

LOG_FILE = os.path.join(OUT_DIR, "guard.log")
PID_FILE = os.path.join(OUT_DIR, "guard.pid")
STATE_FILE = os.path.join(OUT_DIR, "guard_state.json")
PEND_FILE = os.path.join(OUT_DIR, "pending_pause.json")   # 「欠一次暂停」的落盘标记
LOG_MAX = 2 * 1024 * 1024

log = make(lambda: LOG_FILE, max_bytes=LOG_MAX)

# poll_loop 跑在子线程里，主线程退出（消息循环结束）时靠它把轮询也叫停。
STOP = threading.Event()

_STATE = {"started": None, "last_event": None, "last_result": None, "cred_ok": None,
          "want_pause": False,     # 期望服务端是暂停态（一旦置位就不再翻回去）
          "retry_at": 0,           # 下一次补发的最早时刻（退避用）
          "retry_count": 0}


def save_state():
    try:
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(_STATE, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def mark_pending(reason):
    """落盘「欠一次暂停」。

    必须在发请求之前调：写本地文件是微秒级、必定成功，而请求可能卡到进程被杀
    （关机回调里系统只等几秒）。有这条记录，开机才能把漏掉的那次捡回来。

    重复调是幂等的 —— 睡眠恢复后进程还活着时，靠它配合 poll_loop 的重试收尾。
    """
    try:
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(PEND_FILE, "w", encoding="utf-8") as f:
            json.dump({"reason": reason,
                       "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]},
                      f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def clear_pending():
    """撤销欠账标记 —— 只在确认服务端已进入暂停态之后调。"""
    try:
        os.remove(PEND_FILE)
    except Exception:
        pass


def load_pending():
    """读欠账标记；没有、损坏或字段不全都返回 None。"""
    try:
        with open(PEND_FILE, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return None
    return d if isinstance(d, dict) and d.get("reason") else None
