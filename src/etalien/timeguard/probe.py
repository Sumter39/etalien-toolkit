# -*- coding: utf-8 -*-
"""环境探测：进程表、锁屏、键鼠空闲、全屏。

两条保守规则：
    前台有铺满整屏的窗口时不判空闲（手柄玩游戏不产生键鼠输入，只看空闲秒数不准）。
    进程查询失败时按「客户端还在运行」处理，避免误暂停。
"""
import ctypes
import ctypes.wintypes as wt

from .settings import CONFIG

TH32CS_SNAPPROCESS = 0x00000002


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wt.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
_k32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
_k32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
_k32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
_k32.CloseHandle.argtypes = [ctypes.c_void_p]


def process_running(name):
    """按 exe 名查进程。返回 True/False；查询本身失败时返回 None。"""
    snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == ctypes.c_void_p(-1).value:
        return None
    try:
        target = name.lower()
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = _k32.Process32FirstW(snap, ctypes.byref(pe))
        while ok:
            if pe.szExeFile.lower() == target:
                return True
            ok = _k32.Process32NextW(snap, ctypes.byref(pe))
        return False
    except Exception:
        return None
    finally:
        _k32.CloseHandle(snap)


def client_running():
    """客户端是否在运行。查询失败时保守当作「在运行」，避免误暂停。"""
    r = process_running(CONFIG["process"])
    return True if r is None else r


def is_locked():
    """锁屏判定：锁屏界面由 LogonUI.exe 承担，它一出现就是锁了。"""
    return process_running("LogonUI.exe") is True


def idle_seconds():
    """距离最后一次键鼠输入过了多少秒。"""
    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
        return 0.0
    tick = ctypes.windll.kernel32.GetTickCount()
    return ((tick - lii.dwTime) & 0xFFFFFFFF) / 1000.0


def foreground_fullscreen():
    """前台是否有铺满整屏的窗口。

    用来豁免空闲判定 —— 用手柄打游戏不产生键鼠输入，光看空闲秒数会被误杀。
    最大化的普通窗口高度会矮一截（让出任务栏），不会被误判成全屏。
    """
    try:
        u = ctypes.windll.user32
        h = u.GetForegroundWindow()
        if not h:
            return False
        r = wt.RECT()
        if not u.GetWindowRect(h, ctypes.byref(r)):
            return False
        sw, sh = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
        return (r.right - r.left) >= sw and (r.bottom - r.top) >= sh
    except Exception:
        return False
