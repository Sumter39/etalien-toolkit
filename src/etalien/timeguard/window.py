# -*- coding: utf-8 -*-
"""隐藏的顶层窗口，用来接系统广播。

WM_POWERBROADCAST 只广播给顶层窗口，message-only 窗口收不到，所以这个窗口
虽然不显示，但必须是真正的顶层窗口。
"""
from .pauser import pause
from .state import log

WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016
WM_POWERBROADCAST = 0x0218
PBT_APMSUSPEND = 0x0004
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMRESUMEAUTOMATIC = 0x0012
ENDSESSION_LOGOFF = 0x80000000


def wndproc(hwnd, msg, wparam, lparam):
    import win32gui
    try:
        if msg == WM_QUERYENDSESSION:
            kind = "注销" if (lparam & ENDSESSION_LOGOFF) else "关机/重启"
            log("⚠ 收到系统结束会话通知（%s），抢时间发送暂停" % kind)
            pause("系统" + kind, fast=True, channel="消息")
            return 1                       # TRUE：同意继续关机
        if msg == WM_ENDSESSION:
            if wparam:
                log("⚠ 会话即将结束，兜底再报一次")
                pause("会话结束兜底", fast=True, channel="ENDSESSION")
            return 0
        if msg == WM_POWERBROADCAST:
            if wparam == PBT_APMSUSPEND:
                log("⚠ 系统即将睡眠/休眠，发送暂停")
                pause("睡眠/休眠", fast=True, channel="电源广播")
            elif wparam in (PBT_APMRESUMESUSPEND, PBT_APMRESUMEAUTOMATIC):
                log("· 系统已从睡眠中恢复")
            return 1
    except Exception as e:
        log("窗口消息处理异常：%s: %s" % (type(e).__name__, e))
    return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)


def make_window():
    """建一个隐藏的顶层窗口，专门用来接系统广播。"""
    import win32con
    import win32gui

    classname = "EtalienTimeGuardWnd"
    wc = win32gui.WNDCLASS()
    wc.hInstance = win32gui.GetModuleHandle(None)
    wc.lpszClassName = classname
    wc.lpfnWndProc = wndproc
    try:
        win32gui.RegisterClass(wc)
    except Exception:
        pass                              # 类已注册

    hwnd = win32gui.CreateWindowEx(
        0, classname, "EtalienTimeGuard",
        win32con.WS_POPUP,
        -200, -200, 1, 1,
        0, 0, wc.hInstance, None)
    if not hwnd:
        raise RuntimeError("创建隐藏窗口失败，无法接收系统关机/睡眠信号")
    return hwnd
