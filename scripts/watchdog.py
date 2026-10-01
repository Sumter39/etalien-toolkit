#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""外星仔加速器 · 时长守护进程

在四种「你已经不需要加速，但服务端还在扣时长」的情况下，
自动向服务端上报一次暂停（POST /v2/account/update/pause/state  field1=1）：

  ① 关机 / 重启 / 注销   接系统 WM_QUERYENDSESSION，抢在被强杀之前发出去
  ② 睡眠 / 休眠 / 合盖   接 WM_POWERBROADCAST(PBT_APMSUSPEND)
  ③ 客户端进程退出       轮询 etalien.exe，「在 → 不在」的那一刻触发
  ④ 锁屏 / 键鼠空闲超时  人已经离开电脑

为什么不需要管理员权限：
    暂停是纯服务端状态 —— 实测把客户端进程 taskkill 强杀之后，接口照样调得通。
    本进程只发一个 HTTPS 请求，不碰客户端进程，所以不触发 UIPI 那条线。

为什么要建一个隐藏窗口：
    WM_POWERBROADCAST 只广播给顶层窗口，message-only 窗口收不到，
    所以必须建一个真正的顶层窗口，但保持隐藏（不 Show），不进任务栏也不进 Alt+Tab。

用法：
    pythonw watchdog.py             # 常驻（由注册表 Run 键拉起，无窗口）
    python  watchdog.py --status    # 看运行状态 + 剩余时长
    python  watchdog.py --pause-now # 立刻暂停一次
    python  watchdog.py --stop      # 停掉
"""

import argparse
import atexit
import ctypes
import ctypes.wintypes as wt
import json
import os
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import etapi as E      # noqa: E402
import etconfig as CFG  # noqa: E402

ROOT = E.ROOT
OUT = E.OUT_DIR
LOG_FILE = os.path.join(OUT, "watchdog.log")
PID_FILE = os.path.join(OUT, "watchdog.pid")
STATE_FILE = os.path.join(OUT, "watchdog_state.json")
LOCK_PORT = 47651          # 单实例锁（本地回环端口，不占权限）
LOG_MAX = 2 * 1024 * 1024  # 日志到 2MB 轮转一次

CONFIG = {
    "process": "etalien.exe",       # 要盯的客户端进程
    "idle_seconds": CFG.get("idle_minutes") * 60,   # 键鼠空闲多久算「人走了」
    "poll_interval": 5,             # 轮询间隔（秒）
    "http_timeout": 5,              # 暂停请求超时（关机窗口只有几秒，别设太长）
    "pause_path": "/v2/account/update/pause/state",
    "watch_process": True,          # 盯客户端进程退出
    "watch_lock": True,             # 盯锁屏
    "watch_idle": True,             # 盯键鼠空闲
}

RUNNING = True
_STATE = {"client": None, "locked": False, "idle": False,
          "started": None, "last_event": None, "last_result": None}

# ---------------------------------------------------------------- 系统常量

WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016
WM_POWERBROADCAST = 0x0218
PBT_APMSUSPEND = 0x0004
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMRESUMEAUTOMATIC = 0x0012
ENDSESSION_LOGOFF = 0x80000000


# ---------------------------------------------------------------- 日志


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]


def _rotate():
    try:
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > LOG_MAX:
            bak = LOG_FILE + ".1"
            if os.path.exists(bak):
                os.remove(bak)
            os.replace(LOG_FILE, bak)
    except Exception:
        pass


def log(msg):
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        if sys.stdout is not None:
            print(line, flush=True)
    except Exception:
        pass
    try:
        os.makedirs(OUT, exist_ok=True)
        _rotate()
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def save_state():
    try:
        os.makedirs(OUT, exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(_STATE, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ---------------------------------------------------------------- 核心动作


def pause(reason):
    """上报一次「暂停计时」。已经是暂停状态时服务端返回 500，属正常。"""
    body = E.pb_v(1, 1) if hasattr(E, "pb_v") else b"\x08\x01"
    t0 = time.time()
    try:
        st, raw = E.call(CONFIG["pause_path"], body, timeout=CONFIG["http_timeout"])
    except SystemExit as e:                     # 没有 token
        log("✗ 暂停失败 [%s]：%s" % (reason, e))
        _STATE["last_event"] = reason
        _STATE["last_result"] = "no-token"
        save_state()
        return False
    except Exception as e:
        log("✗ 暂停失败 [%s]：%s: %s" % (reason, type(e).__name__, e))
        _STATE["last_event"] = reason
        _STATE["last_result"] = "error"
        save_state()
        return False

    ms = int((time.time() - t0) * 1000)
    _STATE["last_event"] = reason

    if st == 200:
        log("✓ 已暂停计时 [%s]  HTTP 200  %dms" % (reason, ms))
        _STATE["last_result"] = "paused"
        save_state()
        return True
    if st == 500 and b"same pause state" in raw:
        log("· 本来就是暂停状态，无需重复 [%s]  %dms" % (reason, ms))
        _STATE["last_result"] = "already-paused"
        save_state()
        return True

    log("✗ 暂停失败 [%s]  HTTP %s  %r  %dms" % (reason, st, raw[:120], ms))
    _STATE["last_result"] = "http-%s" % st
    save_state()
    return False


# ---------------------------------------------------------------- 环境探测

TH32CS_SNAPPROCESS = 0x00000002


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
    """etalien.exe 是否在运行。查询失败时保守当作「在运行」，避免误暂停。"""
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


# ---------------------------------------------------------------- 轮询线程


def poll_loop():
    st = {"client": client_running(), "locked": is_locked(), "idle": False}
    _STATE["client"] = st["client"]
    _STATE["locked"] = st["locked"]
    save_state()
    log("初始状态：客户端%s，屏幕%s" % ("在运行" if st["client"] else "未运行",
                                        "已锁" if st["locked"] else "未锁"))

    while RUNNING:
        try:
            # ① 客户端进程退出
            if CONFIG["watch_process"]:
                cur = client_running()
                if st["client"] and not cur:
                    log("→ 客户端进程已退出")
                    pause("客户端退出")
                elif not st["client"] and cur:
                    log("→ 客户端已重新启动，继续监测")
                if cur != st["client"]:
                    st["client"] = cur
                    _STATE["client"] = cur
                    save_state()

            # ② 锁屏
            cur_lock = is_locked()
            if CONFIG["watch_lock"] and cur_lock != st["locked"]:
                if cur_lock:
                    log("→ 屏幕已锁定")
                    pause("锁屏")
                else:
                    log("→ 屏幕已解锁")
                st["locked"] = cur_lock
                _STATE["locked"] = cur_lock
                save_state()
            elif not CONFIG["watch_lock"]:
                st["locked"] = cur_lock

            # ③ 键鼠空闲（锁屏时不重复判；全屏应用视为「在用电脑」，豁免）
            if CONFIG["watch_idle"] and not cur_lock:
                if foreground_fullscreen():
                    if st["idle"]:
                        log("· 前台是全屏应用，空闲计时重置")
                        st["idle"] = False
                else:
                    idle = idle_seconds()
                    if idle >= CONFIG["idle_seconds"] and not st["idle"]:
                        log("→ 键鼠已空闲 %.0f 分钟" % (idle / 60))
                        pause("空闲%.0f分钟" % (idle / 60))
                        st["idle"] = True
                    elif idle < 60 and st["idle"]:
                        log("→ 检测到操作活动，空闲计时归零")
                        st["idle"] = False
            elif cur_lock:
                st["idle"] = False

        except Exception as e:
            log("轮询异常：%s: %s" % (type(e).__name__, e))

        time.sleep(CONFIG["poll_interval"])


# ---------------------------------------------------------------- 窗口过程


def wndproc(hwnd, msg, wparam, lparam):
    import win32gui
    try:
        if msg == WM_QUERYENDSESSION:
            kind = "注销" if (lparam & ENDSESSION_LOGOFF) else "关机/重启"
            log("⚠ 收到系统结束会话通知（%s），抢时间发送暂停" % kind)
            pause("系统" + kind)
            return 1                       # TRUE：同意继续关机
        if msg == WM_ENDSESSION:
            if wparam:
                log("⚠ 会话即将结束，兜底再报一次")
                pause("会话结束兜底")
            return 0
        if msg == WM_POWERBROADCAST:
            if wparam == PBT_APMSUSPEND:
                log("⚠ 系统即将睡眠/休眠，发送暂停")
                pause("睡眠/休眠")
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


# ---------------------------------------------------------------- 单实例


def acquire_lock():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
        s.listen(1)
        return s
    except OSError:
        return None


def write_pid():
    try:
        with open(PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass


def clean_pid():
    try:
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
    except Exception:
        pass


# ---------------------------------------------------------------- 子命令


def cmd_status():
    running = "运行中" if acquire_lock() is None else "未运行"
    print("守护进程：%s" % running)
    if os.path.exists(STATE_FILE):
        try:
            d = json.load(open(STATE_FILE, encoding="utf-8"))
            print("  启动于   :", d.get("started"))
            print("  客户端   :", "在运行" if d.get("client") else "未运行")
            print("  屏幕     :", "已锁" if d.get("locked") else "未锁")
            print("  末次触发 :", d.get("last_event") or "（还没触发过）")
            print("  末次结果 :", d.get("last_result") or "-")
        except Exception as e:
            print("  状态文件读取失败:", e)

    print("  当前空闲 : %.0f 秒" % idle_seconds())
    print("  实时锁屏 :", "是" if is_locked() else "否")
    print("  客户端   :", "在运行" if client_running() else "未运行")

    st, raw = E.call("/v2/account/remain/duration", timeout=15)
    if st == 200:
        fs = {f: v for f, k, v in E.pb_decode(raw) if k == "varint"}
        print("  剩余时长 :", E.fmt_dur(fs.get(1, 0)))
    else:
        print("  剩余时长 : 查询失败 HTTP %s" % st)

    print("  日志     :", LOG_FILE)


def cmd_pause_now():
    ok = pause("手动触发")
    sys.exit(0 if ok else 1)


RUN_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "EtalienTimeGuard"


def _launch_cmdline():
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pyw):
        pyw = sys.executable
    return '"%s" "%s"' % (pyw, os.path.abspath(__file__))


def cmd_install():
    """写进 HKCU 的 Run 键 → 登录自动启动。

    不用计划任务（要管理员），也不用启动文件夹的 .cmd（多一层 shell，
    在受限环境下反而容易出岔子）。Run 键直接调 pythonw，链路最短。
    """
    import winreg

    cmdline = _launch_cmdline()
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE)
    winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, cmdline)
    winreg.CloseKey(key)

    # 清掉早期版本放在启动文件夹里的 .cmd，避免重复启动
    startup = os.path.join(os.environ.get("APPDATA", ""),
                           "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
    stale = os.path.join(startup, "etalien_watchdog.cmd")
    if os.path.exists(stale):
        os.remove(stale)
        print("已清理旧启动项:", stale)

    print("已安装开机自启（注册表 Run 项）：")
    print("  HKCU\\%s\\%s" % (RUN_KEY, RUN_NAME))
    print("  启动命令:", cmdline)
    print("  空闲阈值:", CONFIG["idle_seconds"] // 60, "分钟")
    print()
    print("下次登录时自动生效。想立刻用，直接跑 `python watchdog.py` 即可。")


def cmd_uninstall():
    import winreg

    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE)
        winreg.DeleteValue(key, RUN_NAME)
        winreg.CloseKey(key)
        print("已移除注册表自启项:", RUN_NAME)
    except FileNotFoundError:
        print("注册表里没有该项，无需移除")
    except Exception as e:
        print("移除失败:", e)

    startup = os.path.join(os.environ.get("APPDATA", ""),
                           "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
    stale = os.path.join(startup, "etalien_watchdog.cmd")
    if os.path.exists(stale):
        os.remove(stale)
        print("已清理启动文件夹:", stale)


def cmd_stop():
    if not os.path.exists(PID_FILE):
        print("没有 PID 文件 —— 守护进程没在跑")
        return
    try:
        pid = int(open(PID_FILE, encoding="utf-8").read().strip())
    except Exception:
        print("PID 文件损坏")
        return
    r = subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
    out = (r.stdout or b"").decode("gbk", "ignore").strip()
    print(out or "已发送终止信号")
    time.sleep(1)
    try:
        os.remove(PID_FILE)
    except Exception:
        pass


# ---------------------------------------------------------------- 主流程


def main():
    global RUNNING
    ap = argparse.ArgumentParser(description="外星仔加速器 · 时长守护进程")
    ap.add_argument("--stop", action="store_true", help="停止守护进程")
    ap.add_argument("--status", action="store_true", help="查看运行状态")
    ap.add_argument("--pause-now", action="store_true", help="立刻暂停计时")
    ap.add_argument("--install", action="store_true", help="安装开机自启（写入注册表 Run 键）")
    ap.add_argument("--uninstall", action="store_true", help="移除开机自启")
    ap.add_argument("--idle-min", type=int, default=CONFIG["idle_seconds"] // 60,
                    help="键鼠空闲多少分钟后自动暂停（默认 15）")
    ap.add_argument("--process", default=CONFIG["process"],
                    help="要盯的客户端进程名（默认 etalien.exe）")
    a = ap.parse_args()

    if a.stop:
        return cmd_stop()
    if a.status:
        return cmd_status()
    if a.pause_now:
        return cmd_pause_now()
    if a.install:
        return cmd_install()
    if a.uninstall:
        return cmd_uninstall()

    CONFIG["idle_seconds"] = max(1, a.idle_min) * 60
    CONFIG["process"] = a.process

    lock = acquire_lock()
    if lock is None:
        print("已有守护进程在运行，退出")
        return

    os.makedirs(OUT, exist_ok=True)
    _STATE["started"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    log("=" * 20 + " 守护进程启动 " + "=" * 20)
    log("监测项：关机/重启、睡眠/休眠、客户端退出、锁屏、键鼠空闲>%d分钟"
        % (CONFIG["idle_seconds"] // 60))

    # token 自检 —— 失效必须让人看见，不能默默装死
    st, raw = E.call("/v2/account/remain/duration", timeout=15)
    if st == 200:
        fs = {f: v for f, k, v in E.pb_decode(raw) if k == "varint"}
        log("凭据有效，当前剩余时长 %s" % E.fmt_dur(fs.get(1, 0)))
    else:
        log("⚠ 凭据自检失败 HTTP %s %r —— 暂停功能会失效，需要重新抓 token"
            % (st, raw[:120]))

    write_pid()
    atexit.register(clean_pid)

    t = threading.Thread(target=poll_loop, daemon=True)
    t.start()

    try:
        hwnd = make_window()
    except Exception as e:
        log("✗ %s" % e)
        RUNNING = False
        sys.exit(1)

    log("隐藏窗口已建立（hwnd=%s），进入消息循环，等待系统信号" % hwnd)
    import win32gui
    try:
        win32gui.PumpMessages()
    except KeyboardInterrupt:
        pass
    finally:
        RUNNING = False
        log("守护进程退出")
        clean_pid()


if __name__ == "__main__":
    main()
