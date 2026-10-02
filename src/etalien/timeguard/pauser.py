# -*- coding: utf-8 -*-
"""暂停动作与并发闸门。

消息循环（关机/睡眠）和 poll_loop（客户端退出/锁屏/空闲/补发）是两个线程，
可能在同一刻喊暂停。靠 _claim_pause 抢闸门 —— 只有一条真正发请求，其余让位；
不加锁就会各发一次，服务端虽然幂等，但白占关机窗口里宝贵的时间。
"""
import threading
import time

from .. import api
from .creds import resolve_cred
from .settings import CONFIG
from .state import _STATE, clear_pending, load_pending, log, mark_pending, save_state

PAUSE_RETRY_BASE = 5       # 补发第一次等 5 秒
PAUSE_RETRY_MAX = 300      # 补发间隔上限 5 分钟

_PAUSE_LOCK = threading.Lock()
_INFLIGHT = {"ch": None, "done_ok": False}


def _claim_pause(channel):
    """抢「由我来发这次暂停」的资格。返回 True 表示该发请求，False 表示别的触发源在发。

    done_ok 为真时直接短路 —— 已有触发源成功过，服务端已是暂停态，
    后面所有触发都只是「同一次关机里的重复发现」，不必再发。
    """
    with _PAUSE_LOCK:
        if _INFLIGHT["done_ok"]:
            return False
        if _INFLIGHT["ch"] is not None:
            return False
        _INFLIGHT["ch"] = channel
        return True


def _release_pause(ok):
    with _PAUSE_LOCK:
        _INFLIGHT["ch"] = None
        if ok:
            _INFLIGHT["done_ok"] = True


def reset_pause_gate():
    """新一轮「需要注意」的场景开始时重置闸门（非关机路径每次都要能重新发）。"""
    with _PAUSE_LOCK:
        _INFLIGHT["ch"] = None
        _INFLIGHT["done_ok"] = False


def _settle(ok, result):
    """暂停收尾：统一落状态，成功撤欠账、失败排重试。

    失败不能装作处理过 —— 否则一次网络抖动就让这个事件彻底过去，直到下次关机
    都不会再试。退避取指数增长，网络久久不通也不会把日志刷爆。
    """
    _STATE["last_result"] = result
    if ok:
        clear_pending()
        _STATE["retry_count"] = 0
        _STATE["retry_at"] = 0
    else:
        n = _STATE.get("retry_count", 0) + 1
        _STATE["retry_count"] = n
        _STATE["retry_at"] = time.time() + min(PAUSE_RETRY_BASE * (2 ** (n - 1)),
                                              PAUSE_RETRY_MAX)
    save_state()
    return ok


def need_repause():
    """该补发一次暂停吗：期望暂停、但上次没落实、且已过退避时间。

    关机那次漏发就是靠它接住的 —— 那个事件早已过去，唯一能让它「继续被惦记」
    的办法就是把这个判断留在轮询里。
    """
    return (_STATE.get("want_pause")
            and _STATE.get("last_result") not in ("paused", "already-paused")
            and time.time() >= _STATE.get("retry_at", 0))


def pause(reason, fast=False, channel=None):
    """上报一次「暂停计时」。已经是暂停状态时服务端返回 500，属正常。

    请求尽量短命：暂停晚到一秒，就多扣一秒。所以撞上 401 不等 30 分钟的
    周期复检，就地换一份凭据重试 —— 复检默认半小时一次，期间关机/休眠触发的
    暂停会次次扑空，时长照扣。

    fast=True 给「系统正在等我点头」的路径用（关机 / 注销 / 睡眠）。那条路上
    只发一次请求、且超时收紧到 fast_timeout（2 秒）：换凭据要先探活
    （HTTP 超时 5~10 秒，最坏还得启一次模拟器），而 Windows 判「未响应」的阈值
    就是 5 秒，拖过去会弹「此应用阻止关机」，比漏报一次更糟。

    失败不静默：want_pause 一律置位，失败的再排一次重试（见 _settle），
    由 poll_loop 接住往下补。fast=True 还会在发请求之前落盘欠账标记 ——
    请求可能卡到进程被杀，那时就只剩「开机补发」能救了。

    channel 标注这次是哪个触发源。返回 True 表示服务端确已处于暂停态，
    None 表示让位（别的触发源在发），False 表示发了但没成。
    """
    body = api.field(1, 1)       # field1=1：暂停
    tag = channel or "direct"

    if not _claim_pause(tag):
        # 返回 None 而不是 True：True 在别处表示「已确认暂停态」，
        # 让位只是「不必我再发」，不能让人误以为事情已经办完。
        return None

    t0 = time.time()
    _STATE["want_pause"] = True      # 期望暂停态；失败也留着，好让轮询继续补
    if fast:
        mark_pending(reason)         # 先落盘 —— 下面这次请求可能卡到进程被杀

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
        return _settle(False, kind or "net-error")

    if st == 401:
        if fast:
            log("✗ 暂停失败 [%s]  HTTP 401 —— 系统等我结束会话，不换凭据直接放行" % reason)
            _release_pause(False)
            return _settle(False, "token-expired")
        log("· 凭据失效（HTTP 401），换一份可用凭据重试…")
        if resolve_cred(force=True)[0]:
            st, raw, kind = send(timeout)
            if st is None:
                log("✗ 暂停失败 [%s]：%s" % (reason, raw))
                _release_pause(False)
                return _settle(False, kind)
        if st == 401:
            # 两份凭据全都过期 = 只能人工介入（跑一次 adwatch 让 App 续，或重新登录）
            log("✗✗ 暂停失败 [%s]  HTTP 401 凭据全部失效 —— 时长会继续被扣！"
                "跑一次 adwatch.py 让 App 续期，或重新登录客户端" % reason)
            _release_pause(False)
            return _settle(False, "token-expired")

    ms = int((time.time() - t0) * 1000)
    if st == 200:
        log("✓ 已暂停计时 [%s]  HTTP 200  %dms  ← %s" % (reason, ms, tag))
        _release_pause(True)
        return _settle(True, "paused")
    if st == 500 and b"same pause state" in raw:
        log("· 本来就是暂停状态，无需重复 [%s]  %dms  ← %s" % (reason, ms, tag))
        _release_pause(True)
        return _settle(True, "already-paused")

    log("✗ 暂停失败 [%s]  HTTP %s  %r  %dms" % (reason, st, raw[:120], ms))
    _release_pause(False)
    return _settle(False, "http-%s" % st)


def startup_check():
    """开机补账：上次关机 / 睡眠前没发出去的暂停，在这里补。

    关机回调里只剩几秒，请求可能卡到进程被杀 —— 而 mark_pending 已经在发请求
    之前把欠账落盘了，所以这里能把它捡回来。补发是幂等的：服务端若已是暂停态会回
    500 same pause state，同样算成功。

    先读一次服务端真实状态只是为了少打一次无谓的请求；读不到不影响补发 ——
    欠账是确定的事，不能因为「查不到」就算了。
    """
    pend = load_pending()
    if not pend:
        return
    log("⚠ 上次留下一笔未完成的暂停（%s @ %s）—— 开机补发" % (pend["reason"], pend["at"]))
    _, paused = api.pause_state(timeout=15)
    if paused is True:
        log("· 服务端当前已是暂停态，无需补发")
        clear_pending()
        return
    _STATE["want_pause"] = True
    reset_pause_gate()
    pause("开机补发（%s）" % pend["reason"], channel="开机补发")
