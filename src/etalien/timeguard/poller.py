# -*- coding: utf-8 -*-
"""轮询线程：客户端退出、锁屏、键鼠空闲、凭据复检、DNS 补预热。

这几条和窗口消息走的是两条路：窗口消息管系统级事件（关机/睡眠），
轮询管用户态事件（退出客户端/锁屏/闲置），两者互不替代。
"""
import time

from .. import api
from . import probe
from .creds import renew_cred, resolve_cred
from .pauser import pause
from .settings import CONFIG
from .state import STOP, _STATE, log, save_state


def poll_loop():
    st = {"client": probe.client_running(), "locked": probe.is_locked(), "idle": False}
    save_state()
    log("初始状态：客户端%s，屏幕%s" % ("在运行" if st["client"] else "未运行",
                                        "已锁" if st["locked"] else "未锁"))

    while not STOP.is_set():
        try:
            # ① 客户端进程退出
            if CONFIG["watch_process"]:
                cur = probe.client_running()
                if st["client"] and not cur:
                    log("→ 客户端进程已退出")
                    pause("客户端退出", channel="轮询/客户端退出")
                elif not st["client"] and cur:
                    log("→ 客户端已重新启动，继续监测")
                if cur != st["client"]:
                    st["client"] = cur
                    save_state()

            # ② 锁屏
            cur_lock = probe.is_locked()
            if CONFIG["watch_lock"] and cur_lock != st["locked"]:
                if cur_lock:
                    log("→ 屏幕已锁定")
                    pause("锁屏", channel="轮询/锁屏")
                else:
                    log("→ 屏幕已解锁")
                st["locked"] = cur_lock
                save_state()
            elif not CONFIG["watch_lock"]:
                st["locked"] = cur_lock

            # ③ 键鼠空闲（锁屏时不重复判；全屏应用视为「在用电脑」，豁免）
            if CONFIG["watch_idle"] and not cur_lock:
                if probe.foreground_fullscreen():
                    if st["idle"]:
                        log("· 前台是全屏应用，空闲计时重置")
                        st["idle"] = False
                else:
                    idle = probe.idle_seconds()
                    if idle >= CONFIG["idle_seconds"] and not st["idle"]:
                        log("→ 键鼠已空闲 %.0f 分钟" % (idle / 60))
                        pause("空闲%.0f分钟" % (idle / 60), channel="轮询/空闲")
                        st["idle"] = True
                    elif idle < 60 and st["idle"]:
                        log("→ 检测到操作活动，空闲计时归零")
                        st["idle"] = False
            elif cur_lock:
                st["idle"] = False

            # ④ 凭据复检（默认 30 分钟一次）—— 只在状态变化时吱声，避免刷日志
            ok, why = resolve_cred()
            if ok is False:
                # 手里两份都废了 → 借 App 的手续一份。必须赶在复检时做，不能等关机
                # 那一刻：那时只剩几秒，启模拟器根本来不及，只能白扣一次时长。
                # 只在明确 401 时做：网络不通（ok=None）不代表凭据废了。
                if renew_cred()[0]:
                    ok, why = resolve_cred(force=True)
            if ok != _STATE.get("cred_ok"):
                _STATE["cred_ok"] = ok
                save_state()
                if ok:
                    mark = "✓ 凭据可用："
                elif ok is False:
                    mark = "⚠ 凭据不可用："
                else:
                    mark = "· 凭据状态未知："
                log(mark + why)

            # ⑤ DNS 补预热：开机自启时网络还没就绪，启动那次必然失败；缓存不热，
            #    关机时就只能去撞一个不可取消的 getaddrinfo，暂停大概率发不出去。
            if not api.dns_ready():
                if api.prewarm():
                    log("✓ DNS 预热补上了")

        except Exception as e:
            log("轮询异常：%s: %s" % (type(e).__name__, e))

        time.sleep(CONFIG["poll_interval"])
