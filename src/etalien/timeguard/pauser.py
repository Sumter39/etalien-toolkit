# -*- coding: utf-8 -*-
"""暂停动作与并发闸门。

消息循环（关机/睡眠）和 poll_loop（客户端退出/锁屏/空闲）是两个线程，可能在同一
刻喊暂停。靠 _claim_pause 抢闸门 —— 只有一条真正发请求，其余让位；不加锁就会各发
一次，服务端虽然幂等，但白占关机窗口里宝贵的时间。

免重发窗口只在「同一次关机」内有效，过期自动失效 —— 否则它会活到进程重启，把之后
每一次暂停全部短路掉。
"""
import threading
import time

from .. import api
from .creds import resolve_cred
from .settings import CONFIG
from .state import _STATE, log, save_state

DONE_OK_TTL = 3.0          # 「刚发成功」的免重发窗口（秒），够覆盖同一次关机的两个消息

_PAUSE_LOCK = threading.Lock()
_INFLIGHT = {"done_ok_until": 0.0}


def _claim_pause():
    """抢「由我来发这次暂停」的资格。返回 True 表示该发请求，False 表示刚发成功过。"""
    with _PAUSE_LOCK:
        if time.time() < _INFLIGHT["done_ok_until"]:
            return False
        _INFLIGHT["done_ok_until"] = 0.0
        return True


def _release_pause(ok):
    with _PAUSE_LOCK:
        _INFLIGHT["done_ok_until"] = (time.time() + DONE_OK_TTL) if ok else 0.0


def reset_pause_gate():
    """立刻清掉免重发窗口，让下一次 pause() 必定发请求（测试用）。"""
    with _PAUSE_LOCK:
        _INFLIGHT["done_ok_until"] = 0.0


def _settle(result):
    """暂停收尾：把末次结果落进状态文件。"""
    _STATE["last_result"] = result
    save_state()


def pause(reason, fast=False, channel=None):
    """上报一次「暂停计时」。已经处于暂停态时服务端返回 500，属正常。

    fast=True 给「系统正在等我点头」的路径用（关机 / 注销 / 睡眠）：只发一次请求，
    超时收紧到 fast_timeout（2 秒），且撞上 401 不换凭据。非 fast 路径撞 401 会就地
    换一份凭据重试，不等 30 分钟的周期复检。

    channel 只用于日志归因。返回 True 表示服务端确已处于暂停态，None 表示让位
    （刚发成功过，不必再来），False 表示发了但没成。
    """
    body = api.field(1, 1)       # field1=1：暂停
    tag = channel or "direct"

    if not _claim_pause():
        # 返回 None 而不是 True：True 在别处表示「已确认暂停态」，
        # 让位只是「不必我再发」，不能让人误以为事情已经办完。
        return None

    t0 = time.time()

    def send(timeout):
        """打一次暂停接口，返回 (状态码, 原始响应, 失败归类)。

        两种「状态码为 None」必须分开：没有凭据文件、和网络/连接层异常。
        混用同一个名字会让 --status 把断网报成「没有凭据」。
        """
        try:
            st, raw = api.call(CONFIG["pause_path"], body, timeout=timeout)
            return st, raw, None
        except SystemExit as e:                 # 没有凭据文件
            return None, str(e), "no-token"
        except Exception as e:
            return None, "%s: %s" % (type(e).__name__, e), "net-error"

    timeout = CONFIG["fast_timeout"] if fast else CONFIG["http_timeout"]
    _STATE["last_event"] = reason
    try:
        st, raw, kind = send(timeout)
    except BaseException:
        _release_pause(False)
        raise
    if st is None:
        log("✗ 暂停失败 [%s]：%s" % (reason, raw))
        _release_pause(False)
        # kind 为 None 说明异常是在 call 内部被吞掉的（连接层问题），不是没凭据
        _settle(kind or "net-error")
        return False

    if st == 401:
        if fast:
            log("✗ 暂停失败 [%s]  HTTP 401 —— 系统等我结束会话，不换凭据直接放行" % reason)
            _release_pause(False)
            _settle("token-expired")
            return False
        log("· 凭据失效（HTTP 401），换一份可用凭据重试…")
        if resolve_cred(force=True)[0]:
            st, raw, kind = send(timeout)
            if st is None:
                log("✗ 暂停失败 [%s]：%s" % (reason, raw))
                _release_pause(False)
                _settle(kind)
                return False
        if st == 401:
            # 两份凭据全都过期 = 只能人工介入（跑一次 adwatch 让 App 续，或重新登录）
            log("✗✗ 暂停失败 [%s]  HTTP 401 凭据全部失效 —— 时长会继续被扣！"
                "跑一次 adwatch.py 让 App 续期，或重新登录客户端" % reason)
            _release_pause(False)
            _settle("token-expired")
            return False

    ms = int((time.time() - t0) * 1000)
    if st == 200:
        log("✓ 已暂停计时 [%s]  HTTP 200  %dms  ← %s" % (reason, ms, tag))
        _release_pause(True)
        _settle("paused")
        return True
    if st == 500 and b"same pause state" in raw:
        log("· 本来就是暂停状态，无需重复 [%s]  %dms  ← %s" % (reason, ms, tag))
        _release_pause(True)
        _settle("already-paused")
        return True

    log("✗ 暂停失败 [%s]  HTTP %s  %r  %dms" % (reason, st, raw[:120], ms))
    _release_pause(False)
    _settle("http-%s" % st)
    return False
