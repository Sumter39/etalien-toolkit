# -*- coding: utf-8 -*-
"""进程生命周期、子命令与主流程。"""
import argparse
import atexit
import json
import os
import socket
import sys
import threading
import time
from datetime import datetime

from .. import OUT_DIR, api
from .. import config as cfg
from ..procs import run
from . import poller, probe, window
from .creds import resolve_cred
from .pauser import pause
from .settings import CONFIG
from .state import LOG_FILE, PID_FILE, STATE_FILE, STOP, _STATE, log

LOCK_PORT = 47651          # 单实例锁（本地回环端口，不占权限）

RUN_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "EtalienTimeGuard"

RESULT_TEXT = {                  # 「末次结果」原值 → 人话
    "paused": "已暂停",
    "already-paused": "本来就是暂停态",
    "token-expired": "凭据过期或失效",
    "no-token": "没有凭据文件",
    "net-error": "网络/连接失败",
}


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


def cmd_status():
    running = "运行中" if acquire_lock() is None else "未运行"
    print("守护进程：%s" % running)
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                d = json.load(f)
            print("  启动于   :", d.get("started"))
            print("  末次触发 :", d.get("last_event") or "（还没触发过）")
            code = d.get("last_result")
            print("  末次结果 :", RESULT_TEXT.get(code, code) if code else "-")
        except Exception as e:
            print("  状态文件读取失败:", e)

    # 环境类字段一律现算，不信快照文件 —— 文件是运行中进程写的，
    # 而任何临时进程（比如测试）覆盖它之后，守护进程只在「状态变化」时才重写，
    # 于是文件可能长期停留在错误值上，把 --status 变成假象。
    print("  客户端   :", "在运行" if probe.client_running() else "未运行")
    print("  屏幕     :", "已锁" if probe.is_locked() else "未锁")
    print("  当前空闲 : %.0f 秒" % probe.idle_seconds())

    ok, why = resolve_cred(force=True)
    if ok:
        print("  凭据     : 可用 " + why)
    elif ok is False:
        print("  凭据     : 不可用 " + why)
    else:
        print("  凭据     : 未知 " + why)
    if ok:
        st, raw = api.call("/v2/account/remain/duration", timeout=15)
        if st == 200:
            fs = {f: v for f, k, v in api.decode(raw) if k == "varint"}
            print("  剩余时长 :", api.fmt_dur(fs.get(1, 0)))
        else:
            print("  剩余时长 : 查询失败 HTTP %s" % st)

    print("  日志     :", LOG_FILE)


def cmd_pause_now():
    ok = pause("手动触发")
    sys.exit(0 if ok else 1)


def _launch_cmdline():
    r"""Run 键要写的命令行。

    必须指向基础解释器的 pythonw.exe，脚本路径取 sys.argv[0]（当初被执行的那个
    入口），不是本模块的路径 —— 把 Run 键指向包内文件，解释器会拿它当普通模块跑，
    __main__ 逻辑不会触发。
    """
    pyw = os.path.join(sys.base_prefix, "pythonw.exe")
    return '"%s" "%s"' % (pyw, os.path.abspath(sys.argv[0]))


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

    print("已安装开机自启（注册表 Run 项）：")
    print("  HKCU\\%s\\%s" % (RUN_KEY, RUN_NAME))
    print("  启动命令:", cmdline)
    print("  空闲阈值:", CONFIG["idle_seconds"] // 60, "分钟")
    print()
    print("下次登录时自动生效。想立刻用，直接跑 python src/scripts/guard.py 即可。")


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


def cmd_stop():
    if not os.path.exists(PID_FILE):
        print("没有 PID 文件 —— 守护进程没在跑")
        return
    try:
        pid = int(open(PID_FILE, encoding="utf-8").read().strip())
    except Exception:
        print("PID 文件损坏")
        return
    r = run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
    out = (r.stdout or b"").decode("gbk", "ignore").strip()
    print(out or "已发送终止信号")
    time.sleep(1)
    try:
        os.remove(PID_FILE)
    except Exception:
        pass


def main():
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
    # 入口脚本靠这一项挂 pywin32（见 src/scripts/guard.py），缺了窗口消息就收不到。
    cfg.require("venv", "venv 根目录；脚本靠它挂载 pywin32 等第三方包")

    lock = acquire_lock()
    if lock is None:
        print("已有守护进程在运行，退出")
        return

    os.makedirs(OUT_DIR, exist_ok=True)
    _STATE["started"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    log("=" * 20 + " 守护进程启动 " + "=" * 20)
    log("监测项：关机/重启、睡眠/休眠、客户端退出、锁屏、键鼠空闲>%d分钟"
        % (CONFIG["idle_seconds"] // 60))

    # 先把 DNS 解析好缓存起来，关机时网络组件已被拆，那时不必再去碰解析。
    # 开机自启时网络常常还没就绪，这里失败很正常，交给轮询重试。
    if not api.prewarm():
        log("⚠ DNS 预热失败（开机自启时网络多半还没就绪）—— 轮询里会重试")

    # 凭据自检 —— 失效必须让人看见，不能默默装死
    ok, why = resolve_cred(force=True)
    _STATE["cred_ok"] = ok
    if ok:
        log("凭据可用：%s" % why)
        st, raw = api.call("/v2/account/remain/duration", timeout=15)
        if st == 200:
            fs = {f: v for f, k, v in api.decode(raw) if k == "varint"}
            log("当前剩余时长 %s" % api.fmt_dur(fs.get(1, 0)))
    elif ok is False:
        log("⚠ %s —— 暂停功能会失效" % why)
        log("  恢复办法：跑一次 tools/etapi.py scan（客户端在运行时），"
            "或跑一次 adwatch（它会顺手存下模拟器端 App 的 token）")
    else:
        log("· %s —— 稍后自动重试（不报警、也不去续期）" % why)

    write_pid()
    atexit.register(clean_pid)

    threading.Thread(target=poller.poll_loop, daemon=True).start()

    try:
        hwnd = window.make_window()
    except Exception as e:
        log("✗ %s" % e)
        STOP.set()
        sys.exit(1)

    log("隐藏窗口已建立（hwnd=%s），进入消息循环，等待系统信号" % hwnd)
    import win32gui
    try:
        win32gui.PumpMessages()
    except KeyboardInterrupt:
        pass
    finally:
        STOP.set()
        log("守护进程退出")
        clean_pid()
