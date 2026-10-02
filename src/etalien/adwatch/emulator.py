# -*- coding: utf-8 -*-
"""MuMu 模拟器的进程识别与启停。

判死活只看进程名，不看 MuMuManager 的 info 字段 —— 实测 is_main 恒为 false，
主程序在不在跑都是 false，没法用。

关模拟器必须三步走（control shutdown → main close → 提权 taskkill），
少一步主程序都会挂在后台/托盘里。主程序关掉后下次得重新拉起，
start() 里的 ensure_main() 会负责，两边是配套的。
"""
import ctypes
import json
import os
import time

from ..config import mumu_manager, vmindex
from ..procs import run
from .config import CONFIG, log

# 这些 exe 不参与清理：
#   MuMuManager / mumu-cli   当前正在执行的 CLI 工具，不能自杀
#   crashpad_handler         崩溃处理器，名字太通用，别的应用也在用
#   各种 Install / Uninstall 一次性安装卸载程序
SKIP = {
    "mumumanager.exe", "mumu-cli.exe", "crashpad_handler.exe",
    "mumudeviceengineinstaller.exe", "mumunxupdater.exe", "uninstall.exe",
}
SKIP_HINTS = ("install", "uninstall", "setup")

# 只有这几个退了，才算「模拟器真的关掉了」：主程序 + 实例（窗口 / 虚拟机）。
CORE = {"mumunxmain.exe", "mumunxservice.exe", "mumuplayer.exe",
        "mumunxdevice.exe", "mumunxheadless.exe"}


def info():
    """查询 MuMu 实例状态；失败返回 {}。"""
    try:
        r = run([mumu_manager(), "info", "-v", vmindex()],
                capture_output=True, timeout=30)
        return json.loads((r.stdout or b"").decode("utf-8", "ignore"))
    except Exception:
        return {}


def started():
    """Android 实例是否已启动。"""
    return bool(info().get("is_android_started"))


def proc_names():
    """MuMu 运行时可能用到的所有进程名。

    直接扫安装目录下的 exe 文件名，而不是硬编码一张进程表 —— 换 MuMu 版本、
    换安装路径都不用改代码（旧版是 MuMuPlayer.exe，新版是 MuMuNxMain.exe）。
    注意必须递归：实例进程藏在 nx_device/<版本>/shell/ 里
    （MuMuNxDevice.exe / MuMuNxHeadless.exe / NemuShell.exe …），只扫顶层会漏。
    """
    base = os.path.dirname(os.path.dirname(mumu_manager()))     # …\MuMuPlayer
    names = set()
    for top in ("nx_main", "nx_device"):
        for _root, _dirs, files in os.walk(os.path.join(base, top)):
            for f in files:
                low = f.lower()
                if not low.endswith(".exe") or low in SKIP:
                    continue
                if any(h in low for h in SKIP_HINTS):
                    continue
                names.add(f)
    return names


def procs():
    """当前还在跑的 MuMu 进程名（排序；取不到进程表时返回 []）。"""
    try:
        out = run(["tasklist", "/FO", "CSV", "/NH"],
                  capture_output=True, timeout=30).stdout
        running = {ln.split('","')[0].strip('"')
                   for ln in out.decode("gbk", "ignore").splitlines() if ln.strip()}
    except Exception:
        return []
    return sorted(proc_names() & running)


def core_alive():
    """模拟器核心进程（主程序 / 实例）是否还在跑。"""
    return any(p.lower() in CORE for p in procs())


def main_ready():
    """MuMu 主程序（多开器）和它的服务是否都已就绪。"""
    ps = {p.lower() for p in procs()}
    return "mumunxmain.exe" in ps and "mumunxservice.exe" in ps


def ensure_main(timeout=90):
    """确保 MuMu 主程序在跑 —— 不在的话 control launch 必然失败。

    实测：主程序缺失时 MuMuManager control launch 报
    -503 mainnx connect failed，等多久都没用；而主程序必须以管理员权限
    运行，普通权限起来后 IPC 建不起来，一样连不上。用 runas 拉起后 5 秒就绪，
    实例 17 秒起好。（本机 UAC 关闭，这一步静默、不弹框。）
    """
    if main_ready():
        return True

    exe = os.path.join(os.path.dirname(mumu_manager()), "MuMuNxMain.exe")
    if not os.path.exists(exe):
        log("  ! 找不到 MuMu 主程序 %s" % exe)
        return False

    log("MuMu 主程序未运行，正在拉起…")
    try:
        ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, None,
                                            os.path.dirname(exe), 1)
    except Exception as e:
        log("  ! 拉起主程序失败: %s" % e)
        return False

    t0 = time.time()
    while time.time() - t0 < timeout:
        if main_ready():
            log("MuMu 主程序已就绪（%.0fs）" % (time.time() - t0))
            return True
        time.sleep(3)
    log("⚠ MuMu 主程序 %ds 内未就绪，模拟器可能起不来" % timeout)
    return False


def start():
    """用 MuMu 官方 MuMuManager 拉起实例，并等 Android 启动完成。"""
    mgr = mumu_manager()
    if not os.path.exists(mgr):
        log("✗ 找不到 %s —— 确认 config.json 里的 mumu_manager 填对了" % mgr)
        raise SystemExit(1)

    # 上次跑完把主程序关了的话，这里得先把它拉回来，否则 launch 一定失败
    ensure_main()

    log("模拟器未启动，正在拉起…")
    run([mgr, "control", "-v", vmindex(), "launch"],
        capture_output=True, timeout=180)
    t0 = time.time()
    while time.time() - t0 < CONFIG["mumu_boot_timeout"]:
        if started():
            log("模拟器已启动（%.0fs）" % (time.time() - t0))
            return True
        time.sleep(5)
    log("✗ 模拟器启动超时（%ds）" % CONFIG["mumu_boot_timeout"])
    return False


def shutdown():
    """刷完后彻底关掉 MuMu（用户明确要求：脚本退出即完全关闭）。

    只调 control shutdown 关不干净 —— 那只关 Android 实例（窗口 + VM 进程），
    MuMu 主程序（多开器 MuMuNxMain.exe）和它的服务还挂在后台/托盘里。所以：

        1. control shutdown   优雅关实例（VM 正常落盘）
        2. main close         请主程序自己退出
        3. 提权 taskkill      赖着不走时强杀（/T 连子进程）
        4. taskkill 兜底      清掉其余普通权限能清的残留
    """
    mgr = mumu_manager()
    log("正在关闭模拟器…")

    # 1) 优雅关实例（让 VM 正常落盘）
    try:
        run([mgr, "control", "-v", vmindex(), "shutdown"],
            capture_output=True, timeout=120)
    except Exception as e:
        log("  ! shutdown 异常: %s" % e)
    time.sleep(4)

    # 2) 请主程序自己退出
    try:
        run([mgr, "main", "close"], capture_output=True, timeout=60)
    except Exception as e:
        log("  ! main close 异常: %s" % e)
    time.sleep(5)

    # 3) 主程序还赖着 → 提权强杀。/T 连 MuMuNxService 等子进程一起清；
    #    主程序是管理员权限跑的，普通权限的 taskkill 对它只会 Access denied。
    if core_alive():
        tk = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                          "System32", "taskkill.exe")
        for img in ("MuMuNxMain.exe", "MuMuNxService.exe"):
            try:
                ctypes.windll.shell32.ShellExecuteW(None, "runas", tk,
                                                    "/F /T /IM %s" % img, None, 0)
            except Exception:
                pass
        for _ in range(10):
            time.sleep(2)
            if not core_alive():
                break

    # 4) 顺带清掉普通权限就能清掉的其余 MuMu 进程
    for _ in range(4):
        rest = [p for p in procs() if p.lower() not in CORE]
        if not rest:
            break
        killed = 0
        for p in rest:
            try:
                r = run(["taskkill", "/F", "/IM", p],
                        capture_output=True, timeout=30)
                killed += (r.returncode == 0)
            except Exception:
                pass
        if not killed:          # 全是「拒绝访问」，再试多少次都一样
            break
        time.sleep(2)

    left = procs()
    core = [p for p in left if p.lower() in CORE]
    if core:
        log("  ⚠ 模拟器核心进程未退出：%s" % ", ".join(core))
    else:
        log("  ✓ 模拟器已完全关闭")
    rest = [p for p in left if p.lower() not in CORE]
    if rest:
        # MuMu 安装时注册的常驻组件（远程控制 / 健康上报 / adb），开机就在，
        # 跑在更高权限的会话里，普通权限杀不掉 —— 跟「模拟器还在跑」是两回事。
        log("    （以下常驻服务仍在，非本脚本启动：%s）" % ", ".join(rest))
