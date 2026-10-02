#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
外星仔加速器 · 每日「看广告领时长」自动签到

点「看广告 领时长」→ 等广告 Activity 起来 → 停留 15 秒 → 回主界面。
发奖由服务端按广告播放时长判定，所以不需要识别广告内容，也不点任何领奖按钮。

判定**全部走接口，不读界面**（界面会滚动、会漏渲染，而"读不到"最容易被误当成
"已完成"）：
    单轮是否成功    /v2/account/pc/ad/config 的 watchCnt 增加（时长净增作旁证）
    今日是否刷满    三档 watchCnt >= 该档条目数
接口读不到时**不做任何推断**，直接停 —— 宁可少跑一轮，也不误判收工。

环境前提
    1. 已装 MuMu 模拟器；`config.json` 里填好 MuMuManager.exe / adb.exe 路径与
       OAID（模板见 config.example.json）
    2. 模拟器里已装外星仔并登录
    3. 已用 KernelSU(ksud) 把机型属性改成 OnePlus 并写入 persist.oaid，
       否则广告 SDK 报 "oaid sdk not find"，永远卡在「加载中」
       （`ensure_ready()` 每次运行自动重刷）

用法
    python scripts/checkin.py                 # 跑满今天额度
    python scripts/checkin.py --rounds 3      # 只跑 3 轮
    python scripts/checkin.py --dry-run       # 只探测界面，不点击
    python scripts/checkin.py --state         # 打印每日状态
    python scripts/checkin.py --install       # 装 / 卸开机自启（--uninstall 反操作）

每日状态 output/state.json
    date       最后一次跑是哪天     —— 不是今天就必跑
    all_done   那天是否已全部看完   —— 是今天但没看完，继续补跑
    attempts   当天已尝试次数       —— 上限 6 次，防反复启动模拟器
    progress   上次进度快照         —— 排查用

自启触发多次是安全的：刷满了跳过，没刷满接着补。
"""
import argparse
import ctypes
import json
import os
import random
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import etapi as E
import etconfig as CFG
from probe import find_adb


# ----------------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------------
# 分两类，别混：
#   ① 应用自身的常量（包名、按钮文案、档位名称…）—— 写死在代码里，与机器无关
#   ② 跟本机绑定的值（模拟器路径、adb 路径、OAID…）—— 一律放 config.json，
#      见 scripts/etconfig.py。config.json 已被 .gitignore 忽略，不会外泄。
# ----------------------------------------------------------------------------
_ROOT = CFG.ROOT

CONFIG = {
    "package": "com.etalien.booster",
    "main_activity": "com.etalien.booster/com.etalien.booster.ui.MainActivity",

    "fake_brand": "OnePlus",          # OnePlus → 广告SDK走OPPO路径 → 直接读 persist.oaid
    "brand_props": [
        "ro.product.manufacturer", "ro.product.brand",
        "ro.product.vendor.manufacturer", "ro.product.system.manufacturer",
        "ro.product.odm.manufacturer", "ro.product.vendor.brand",
        "ro.product.system.brand", "ro.product.odm.brand",
    ],

    # 「看广告 领时长」按钮文案（含广告加载失败后的重试态）
    "watch_button": ["看广告 领时长", "看广告领时长", "点击重试"],

    # 【核心参数】广告页停留秒数。实测 15s 即可发奖，故基准 15s + 0~4s 抖动。
    # 这是广告端的行为，跟本机无关，所以写死在代码里而不是放 config.json。
    "ad_stay": 15,
    "ad_stay_jitter": 4,

    # 等广告 Activity 出现的最长秒数
    "ad_appear_timeout": 40,

    # 模拟器开机 / adb 就绪的上限（调优参数，config.json 可覆盖）
    "mumu_boot_timeout": CFG.get("mumu_boot_timeout"),

    # 弹窗关键词
    "popup": ["我知道了", "跳过", "关闭", "以后再说", "暂不更新"],

    # 未登录标志
    "login_flags": ["注册登录", "请输入手机号", "登录后开启加速"],

    # 轮次控制
    # 额度分三档：阶段一 9 次×20分 → 阶段二 3 次×30分 → 加油包 9 次×10分，
    # 单日 21 轮刷满 ≈ 6 小时可暂停时长。这里给足上限，实际由「接口显示刷满」
    # 或「连续失败」提前终止。
    "max_rounds": 25,
    "round_gap": (6, 14),        # 轮次间随机间隔（模拟真人节奏）
    "retry_per_round": 2,        # 单轮内失败重试次数
    "max_consecutive_fail": 3,   # 连续失败多少轮后停止

    # 每日状态：date(最后跑哪天) + all_done(那天是否全部看完) + attempts + progress
    "state_file": os.path.join(_ROOT, "output", "state.json"),
    # 同一天最多尝试几次。额度刷不满（广告无填充等）时避免反复拉起模拟器；
    # 正常情况 1~2 次就收工，--force 可无视上限强制再跑。
    "max_attempts_per_day": 6,
    # 运行日志（计划任务用 pythonw.exe 无控制台，必须落盘）
    "log_file": os.path.join(_ROOT, "output", "checkin.log"),
}


_LOG_FP = None


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)          # pythonw.exe 下无控制台，会抛错
    except Exception:
        pass
    global _LOG_FP
    try:
        if _LOG_FP is None:
            path = CONFIG["log_file"]
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if os.path.exists(path) and os.path.getsize(path) > 1_000_000:
                os.replace(path, path + ".1")          # 超 1MB 轮转
            _LOG_FP = open(path, "a", encoding="utf-8")
            _LOG_FP.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 =====\n")
        _LOG_FP.write(line + "\n")
        _LOG_FP.flush()
    except Exception:
        pass


# ----------------------------------------------------------------------------
# 环境自愈（幂等：模拟器没开就开、属性被还原就重刷）
# ----------------------------------------------------------------------------
def _mumu_manager():
    """MuMuManager.exe 的路径。

    刻意做成函数而不是模块级常量 —— 这样 `--state`、`--install` 这类
    不碰模拟器的子命令，在 config.json 还没填好时也能正常用。
    """
    return CFG.require("mumu_manager", "MuMuManager.exe 的完整路径")


def _serial():
    """模拟器 adb 地址。必填，不给默认值 —— 每台机器都可能不一样。"""
    return CFG.require("serial", "模拟器 adb 地址，如 127.0.0.1:16384")


def _vmindex():
    """MuMu 实例编号（多开器里的序号）。必填，不给默认值。"""
    return CFG.require("mumu_vmindex", "MuMu 实例编号，多开器里能看到，通常填 0")


def _mumu_info():
    """查询 MuMu 实例状态；失败返回 {}。"""
    try:
        r = subprocess.run([_mumu_manager(), "info", "-v", _vmindex()],
                           capture_output=True, timeout=30)
        return json.loads((r.stdout or b"").decode("utf-8", "ignore"))
    except Exception:
        return {}


def emulator_started():
    return bool(_mumu_info().get("is_android_started"))


# 这些 exe 不参与清理：
#   MuMuManager / mumu-cli   当前正在执行的 CLI 工具，不能自杀
#   crashpad_handler         崩溃处理器，名字太通用，别的应用也在用
#   各种 Install / Uninstall 一次性安装卸载程序
_MUMU_SKIP = {
    "mumumanager.exe", "mumu-cli.exe", "crashpad_handler.exe",
    "mumudeviceengineinstaller.exe", "mumunxupdater.exe", "uninstall.exe",
}
_MUMU_SKIP_HINTS = ("install", "uninstall", "setup")

# 只有这几个退了，才算「模拟器真的关掉了」：主程序 + 实例（窗口 / 虚拟机）。
_MUMU_CORE = {"mumunxmain.exe", "mumunxservice.exe", "mumuplayer.exe",
              "mumunxdevice.exe", "mumunxheadless.exe"}


def _mumu_proc_names():
    """MuMu 运行时可能用到的所有进程名。

    直接扫安装目录下的 exe 文件名，而不是硬编码一张进程表 —— 换 MuMu 版本、
    换安装路径都不用改代码（旧版是 MuMuPlayer.exe，新版是 MuMuNxMain.exe）。
    注意必须**递归**：实例进程藏在 nx_device/<版本>/shell/ 里
    （MuMuNxDevice.exe / MuMuNxHeadless.exe / NemuShell.exe …），只扫顶层会漏。
    """
    base = os.path.dirname(os.path.dirname(_mumu_manager()))     # …\MuMuPlayer
    names = set()
    for top in ("nx_main", "nx_device"):
        for root, _dirs, files in os.walk(os.path.join(base, top)):
            for f in files:
                low = f.lower()
                if not low.endswith(".exe") or low in _MUMU_SKIP:
                    continue
                if any(h in low for h in _MUMU_SKIP_HINTS):
                    continue
                names.add(f)
    return names


def _mumu_procs():
    """当前还在跑的 MuMu 进程名（排序；取不到进程表时返回 []）。"""
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                             capture_output=True, timeout=30).stdout
        running = {ln.split('","')[0].strip('"')
                   for ln in out.decode("gbk", "ignore").splitlines() if ln.strip()}
    except Exception:
        return []
    return sorted(_mumu_proc_names() & running)


def _core_alive():
    """模拟器核心进程（主程序 / 实例）是否还在跑。"""
    return any(p.lower() in _MUMU_CORE for p in _mumu_procs())


def main_app_ready():
    """MuMu 主程序（多开器）和它的服务是否都已就绪。

    不看 info 里的 is_main —— 实测它一直返回 false，主程序在不在跑都是 false，
    根本没法用。直接认进程名。
    """
    ps = {p.lower() for p in _mumu_procs()}
    return "mumunxmain.exe" in ps and "mumunxservice.exe" in ps


def ensure_main_app(timeout=90):
    """确保 MuMu 主程序在跑 —— 不在的话 `control launch` 必然失败。

    实测：主程序缺失时 `MuMuManager control launch` 报
    `-503 mainnx connect failed`，等多久都没用；而主程序**必须以管理员权限**
    运行，普通权限起来后 IPC 建不起来，一样连不上。用 runas 拉起后 5 秒就绪，
    实例 17 秒起好。（本机 UAC 关闭，这一步静默、不弹框。）
    """
    if main_app_ready():
        return True

    exe = os.path.join(os.path.dirname(_mumu_manager()), "MuMuNxMain.exe")
    if not os.path.exists(exe):
        log(f"  ! 找不到 MuMu 主程序 {exe}")
        return False

    log("MuMu 主程序未运行，正在拉起…")
    try:
        ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, None,
                                            os.path.dirname(exe), 1)
    except Exception as e:
        log(f"  ! 拉起主程序失败: {e}")
        return False

    t0 = time.time()
    while time.time() - t0 < timeout:
        if main_app_ready():
            log(f"MuMu 主程序已就绪（{time.time() - t0:.0f}s）")
            return True
        time.sleep(3)
    log(f"⚠ MuMu 主程序 {timeout}s 内未就绪，模拟器可能起不来")
    return False


def start_emulator():
    """用 MuMu 官方 MuMuManager 拉起实例，并等 Android 启动完成。"""
    mgr = _mumu_manager()
    if not os.path.exists(mgr):
        log(f"✗ 找不到 {mgr} —— 确认 config.json 里的 mumu_manager 填对了")
        sys.exit(1)

    # 上次跑完把主程序关了的话，这里得先把它拉回来，否则 launch 一定失败
    ensure_main_app()

    log("模拟器未启动，正在拉起…")
    subprocess.run([mgr, "control", "-v", _vmindex(), "launch"],
                   capture_output=True, timeout=180)
    t0 = time.time()
    while time.time() - t0 < CONFIG["mumu_boot_timeout"]:
        if emulator_started():
            log(f"模拟器已启动（{time.time() - t0:.0f}s）")
            return True
        time.sleep(5)
    log(f"✗ 模拟器启动超时（{CONFIG['mumu_boot_timeout']}s）")
    return False


def shutdown_emulator():
    """刷完后**彻底**关掉 MuMu（用户明确要求：脚本退出即完全关闭）。

    只调 `control shutdown` 关不干净 —— 那只关 Android 实例（窗口 + VM 进程），
    MuMu 主程序（多开器 MuMuNxMain.exe）和它的服务还挂在后台/托盘里。所以：

        1. control shutdown   优雅关实例（VM 正常落盘）
        2. main close         请主程序自己退出
        3. 提权 taskkill      赖着不走时强杀（/T 连子进程）
        4. taskkill 兜底      清掉其余普通权限能清的残留

    主程序关掉后下次得重新拉起 —— `start_emulator()` 里的 `ensure_main_app()`
    会负责，两边是配套的。
    """
    mgr = _mumu_manager()
    log("正在关闭模拟器…")

    # 1) 优雅关实例（让 VM 正常落盘）
    try:
        subprocess.run([mgr, "control", "-v", _vmindex(), "shutdown"],
                       capture_output=True, timeout=120)
    except Exception as e:
        log(f"  ! shutdown 异常: {e}")
    time.sleep(4)

    # 2) 请主程序自己退出
    try:
        subprocess.run([mgr, "main", "close"], capture_output=True, timeout=60)
    except Exception as e:
        log(f"  ! main close 异常: {e}")
    time.sleep(5)

    # 3) 主程序还赖着 → 提权强杀。/T 连 MuMuNxService 等子进程一起清；
    #    主程序是管理员权限跑的，普通权限的 taskkill 对它只会 Access denied。
    if _core_alive():
        tk = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                          "System32", "taskkill.exe")
        for img in ("MuMuNxMain.exe", "MuMuNxService.exe"):
            try:
                ctypes.windll.shell32.ShellExecuteW(None, "runas", tk,
                                                    f"/F /T /IM {img}", None, 0)
            except Exception:
                pass
        for _ in range(10):
            time.sleep(2)
            if not _core_alive():
                break

    # 4) 顺带清掉普通权限就能清掉的其余 MuMu 进程
    for _ in range(4):
        rest = [p for p in _mumu_procs() if p.lower() not in _MUMU_CORE]
        if not rest:
            break
        killed = 0
        for p in rest:
            try:
                r = subprocess.run(["taskkill", "/F", "/IM", p],
                                   capture_output=True, timeout=30)
                killed += (r.returncode == 0)
            except Exception:
                pass
        if not killed:          # 全是「拒绝访问」，再试多少次都一样
            break
        time.sleep(2)

    left = _mumu_procs()
    core = [p for p in left if p.lower() in _MUMU_CORE]
    if core:
        log(f"  ⚠ 模拟器核心进程未退出：{', '.join(core)}")
    else:
        log("  ✓ 模拟器已完全关闭")
    rest = [p for p in left if p.lower() not in _MUMU_CORE]
    if rest:
        # MuMu 安装时注册的常驻组件（远程控制 / 健康上报 / adb），开机就在，
        # 跑在更高权限的会话里，普通权限杀不掉 —— 跟「模拟器还在跑」是两回事。
        log(f"    （以下常驻服务仍在，非本脚本启动：{', '.join(rest)}）")


def adb_sh(adb, cmd, timeout=30):
    """在模拟器里执行 shell 命令，返回 stdout 文本。

    走 adb 而不是 `MuMuManager.exe sh`：后者超时杀不掉子进程，会把调用它的
    Python 一起拖死（实测卡住 10 分钟以上）。命令本身以什么身份执行由调用方
    保证 —— 要写系统属性就先调 `ensure_root()`。
    """
    try:
        r = subprocess.run([adb, "-s", _serial(), "shell", cmd],
                           capture_output=True, timeout=timeout)
        return (r.stdout or b"").decode("utf-8", "ignore").strip()
    except Exception:
        return ""


def ensure_root(adb):
    """让 adb shell 拿到 root 身份 —— 注入 `ro.product.*` 需要它。

    MuMu 的 adbd 支持 `adb root`（回 `restarting adbd as root`），重启后 `id`
    就是 uid=0。**不要**改用 `MuMuManager.exe sh`：那条通道虽然天生是 root，
    但请求超时杀不掉子进程，会把调用方一起拖死（实测卡 10 分钟以上）。

    adbd 重启会断开连接，所以必须在「adb 已就绪」之后调。
    """
    if adb_sh(adb, "id").startswith("uid=0"):
        return True
    try:
        subprocess.run([adb, "-s", _serial(), "root"],
                       capture_output=True, timeout=30)
        subprocess.run([adb, "-s", _serial(), "wait-for-device"],
                       capture_output=True, timeout=60)
    except Exception:
        pass
    for _ in range(8):
        if adb_sh(adb, "id").startswith("uid=0"):
            log("adb 已切到 root")
            return True
        time.sleep(3)
    return False


def ensure_ready(adb):
    """确保：模拟器在跑 → adb 可连 → OAID/品牌属性已注入。"""
    # 1) 模拟器实例
    if not emulator_started() and not start_emulator():
        sys.exit(1)

    # 2) 等 adb 可连
    log("等待模拟器 adb 就绪…")
    t0 = time.time()
    while time.time() - t0 < CONFIG["mumu_boot_timeout"]:
        subprocess.run([adb, "connect", _serial()], capture_output=True, timeout=20)
        out = subprocess.run([adb, "devices"], capture_output=True, timeout=20).stdout.decode("utf-8", "ignore")
        if _serial() in out:
            log(f"adb 已就绪（{time.time() - t0:.0f}s）")
            break
        time.sleep(5)
    else:
        log(f"✗ 等不到模拟器 adb 就绪（{CONFIG['mumu_boot_timeout']}s）")
        sys.exit(1)

    # 3) 注入 OAID 与品牌属性（MuMu 每次重启都会还原 ro.*），带重试验证。
    #    冷启动时系统可能还没就绪，命令会返回空 → 靠重试兜住。
    if not ensure_root(adb):
        log("⚠ 拿不到 adb root，品牌属性刷不进去 —— 广告多半无填充")
    oaid_value = CFG.require("oaid", "注入模拟器的假 OAID，任意 UUID 即可")
    brand = oaid = ""
    for i in range(8):
        adb_sh(adb, f"setprop persist.oaid {oaid_value}")
        for k in CONFIG["brand_props"]:
            adb_sh(adb, f"/data/adb/ksud resetprop {k} {CONFIG['fake_brand']}")
        brand = adb_sh(adb, "getprop ro.product.manufacturer")
        oaid = adb_sh(adb, "getprop persist.oaid")
        if CONFIG["fake_brand"].lower() in brand.lower() and oaid_value in oaid:
            log(f"OAID 环境已就绪（brand={brand}, oaid={oaid[:8]}…）")
            return
        log(f"  属性未生效（brand={brand!r}），重试 {i + 1}/8…")
        time.sleep(4)
    log(f"⚠ OAID 环境异常（brand={brand!r}, oaid={oaid[:12]!r}）—— 广告可能无填充")


def connect():
    adb = find_adb()
    if not adb:
        log("找不到 adb.exe —— 确认模拟器已装，或直接把路径填进 config.json 的 adb_path")
        sys.exit(1)
    os.environ["ADBUTILS_ADB_PATH"] = adb
    os.environ["PATH"] = os.path.dirname(adb) + os.pathsep + os.environ.get("PATH", "")
    log(f"adb: {adb}")

    ensure_ready(adb)

    import uiautomator2 as u2
    try:
        d = u2.connect(_serial())
    except Exception:
        d = u2.connect()
    info = d.info
    w, h = info.get("displayWidth") or 0, info.get("displayHeight") or 0
    log(f"已连接 {d.serial}  {w}x{h}")
    if w > h:
        log("⚠ 当前是横屏。App 为竖屏设计，建议固定为竖屏：")
        log(f'  MuMuManager.exe setting -v {_vmindex()} -k resolution_mode -val phone.1')
    return d, adb


# ----------------------------------------------------------------------------
# 进度读取（全部走接口，不读界面）
# ----------------------------------------------------------------------------
def read_progress(adb, serial, tries=2):
    """读一次进度；失败会重读 App token 再试，仍失败返回最后一次的结果。

    每次都重新从模拟器取 token —— App 自己会调 refresh/token 续期，
    现读的就是最新的一份。
    """
    r = None
    for i in range(tries):
        r = E.read_progress(adb, serial)
        if r["ok"]:
            return r
        log(f"  ! 接口读取失败：{r['reason']}")
        if i + 1 < tries:
            time.sleep(4)
    return r


def fmt_progress(p):
    """打一行，例如：时长 6时8分  [阶段一 9/9  阶段二 0/3  加油包 0/9]"""
    sec = p.get("seconds")
    bal = E.fmt_dur(sec) if sec is not None else "?"
    parts = [f"{s['title']} {s['done']}/{s['total']}" for s in (p.get("stages") or [])]
    return "时长 " + bal + ("  [" + "  ".join(parts) + "]" if parts else "")


def progressed(before, after):
    """本轮是否真的进账；返回描述字符串，没进账返回 None。

    主判据是 watchCnt 增加 —— 它是纯计数，不像可暂停时长那样会被加速扣减
    稀释掉。时长净增只当旁证。
    """
    old = {s["title"]: s for s in (before.get("stages") or [])}
    for s in (after.get("stages") or []):
        o = old.get(s["title"])
        if o and s["done"] > o["done"]:
            return f"{s['title']} {o['done']}→{s['done']}/{s['total']}"
    bs, as_ = before.get("seconds"), after.get("seconds")
    if bs is not None and as_ is not None and as_ > bs:
        return f"时长 +{int((as_ - bs) // 60)} 分钟"
    return None


def quota_done(stages):
    """三档是否都刷满。

    读不到档位（接口没回、返回空）一律**不算刷满** ——「看不到」不等于
    「已完成」，以前的误判就是在这栽的。
    """
    if not stages:
        return False
    return all(s["total"] > 0 and s["done"] >= s["total"] for s in stages)


# ----------------------------------------------------------------------------
# 界面工具
# ----------------------------------------------------------------------------
def find_any(d, texts, timeout=0):
    """按顺序找第一个存在的控件；timeout>0 时轮询等待。"""
    deadline = time.time() + timeout
    while True:
        for t in texts:
            try:
                el = d(textContains=t)
                if el.exists:
                    return el, t
            except Exception:
                pass
        if time.time() > deadline:
            return None, None
        time.sleep(1)


def is_main(d):
    try:
        return d.app_current().get("activity", "").endswith(".ui.MainActivity")
    except Exception:
        return False


def punch_home(d, tries=3):
    """【万能穿透】不管上层压着什么广告页/落地页，直接把宿主主界面拉到前台。

    比"找关闭按钮"稳得多：不依赖任何广告联盟的 UI，且 MainActivity 是
    singleTask，宿主状态完整保留，不会重走启动流程。
    """
    for _ in range(tries):
        try:
            d.shell(f"am start -n {CONFIG['main_activity']}")
        except Exception:
            pass
        time.sleep(3)
        if is_main(d):
            return True
    return is_main(d)


def dismiss_popups(d):
    """点掉挡路的弹窗。优先处理 app 的「确定要退出吗」。"""
    try:
        if d(textContains="退出").exists and d(text="取消").exists:
            d(text="取消").click()
            log("  已取消「确定要退出吗」弹窗")
            time.sleep(1.5)
            return True
    except Exception:
        pass

    el, t = find_any(d, CONFIG["popup"], timeout=0)
    if el:
        try:
            el.click()
            log(f"  点掉弹窗「{t}」")
            time.sleep(1)
            return True
        except Exception:
            pass
    return False


def ensure_on_ads_page(d, timeout=5):
    """确保停在「看广告 领时长」页面。

    竖屏手机分辨率下页面较长，主按钮会被挤到滚动区之外 —— 找不到就上滑几次。
    """
    el, _ = find_any(d, CONFIG["watch_button"], timeout=timeout)
    if el:
        return True

    # 切到主页 tab（按钮在主页，不在充值中心）
    for t in ["个人中心", "主页"]:
        try:
            tab = d(textContains=t)
            if tab.exists:
                tab.click()
                time.sleep(2.5)
                el, _ = find_any(d, CONFIG["watch_button"], timeout=3)
                if el:
                    return True
        except Exception:
            pass

    # 上滑找按钮（用相对坐标，不依赖分辨率）
    for _ in range(4):
        try:
            w, h = d.info["displayWidth"], d.info["displayHeight"]
            d.swipe(w // 2, int(h * 0.78), w // 2, int(h * 0.30), 0.25)
        except Exception:
            pass
        time.sleep(1.2)
        el, _ = find_any(d, CONFIG["watch_button"], timeout=2)
        if el:
            return True
    return False


def is_logged_in(d):
    for t in CONFIG["login_flags"]:
        try:
            if d(textContains=t).exists:
                return False
        except Exception:
            pass
    return True


# ----------------------------------------------------------------------------
# 每日状态（记住 last run day + 是否「全部看完」）
# ----------------------------------------------------------------------------
# 见文件头「每日状态」一节。为什么必须记 all_done：额度虽然是 0 点重置的，
# 但同一天内也可能只刷了一半（电脑关机 / 脚本被打断 / 广告无填充）。只按
# 「今天跑过没有」去重，这半天的额度就白丢了 —— 而只按「没刷满就一直跑」，
# 又会在广告无填充的日子里反复拉起模拟器。所以用 attempts 封顶。

def load_state():
    """读状态文件，返回 dict；不存在/为空/损坏 → {}。"""
    try:
        with open(CONFIG["state_file"], encoding="utf-8") as f:
            raw = f.read().strip()
    except Exception:
        return {}
    if not raw:
        return {}
    try:
        st = json.loads(raw)
        return st if isinstance(st, dict) else {}
    except Exception:
        return {}


def save_state(**kw):
    """合并写入状态文件（未指定的字段保持原值）。

    注意：一旦传入的 date 和文件里的日期不同，说明跨天了 —— 旧记录的所有字段
    （all_done / attempts / progress）立即作废，绝不继承。否则「昨天已看完」
    会顺着 merge 漏进今天，把当天的额度直接吞掉。
    """
    st = load_state()
    if kw.get("date") and st.get("date") and kw["date"] != st["date"]:
        st = {}
    st.update(kw)
    st["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        os.makedirs(os.path.dirname(CONFIG["state_file"]), exist_ok=True)
        with open(CONFIG["state_file"], "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"  ! 写入状态文件失败: {e}")
    return st


def _today():
    return time.strftime("%Y-%m-%d")


def state_today():
    """取「今天」的状态；文件里是别的日期（昨天）→ 当作全新的一天，不继承。"""
    st = load_state()
    if st.get("date") != _today():
        return {"date": _today(), "all_done": False, "attempts": 0, "progress": None}
    st.setdefault("all_done", False)
    st.setdefault("attempts", 0)
    return st


def bump_attempt():
    """当天尝试次数 +1，**立即落盘** —— 中途断电也照样算数，不会被无限重试。"""
    return save_state(date=_today(), attempts=int(state_today().get("attempts", 0)) + 1)


# ----------------------------------------------------------------------------
# 单轮流程
# ----------------------------------------------------------------------------
def run_one_round(d, adb, serial):
    """返回 (状态, 快照)。状态：'ok' 成功 / 'done' 已刷满 / 'fail' 失败 / 'error' 接口不通。"""
    dismiss_popups(d)

    # 先读进度再点 —— 接口不通就别点，点了也不知道成没成，白扣广告额度
    before = read_progress(adb, serial)
    if not before["ok"]:
        return "error", before

    if not ensure_on_ads_page(d):
        if quota_done(before["stages"]):
            return "done", before
        log("  ✗ 不在「看广告」页面")
        return "fail", before

    # 1) 点「看广告 领时长」
    el, label = find_any(d, CONFIG["watch_button"], timeout=5)
    if not el:
        log("  ✗ 找不到「看广告」按钮")
        return ("done" if quota_done(before["stages"]) else "fail"), before
    try:
        el.click()
    except Exception as e:
        log(f"  ✗ 点击失败: {e}")
        return "fail", before
    log(f"  已点击「{label}」")

    # 2) 等广告 Activity 起来
    t0 = time.time()
    while time.time() - t0 < CONFIG["ad_appear_timeout"]:
        if not is_main(d):
            break
        time.sleep(1)
    else:
        if quota_done(before["stages"]):
            return "done", before
        log("  ✗ 广告未出现（无填充 or 额度已用尽）")
        return "fail", before

    try:
        act = d.app_current().get("activity", "")
    except Exception:
        act = "?"
    log(f"  广告已出现 ({time.time()-t0:.0f}s): {act.split('.')[-1]}")

    # 3) 【核心】原地停留，不点任何按钮 —— 发奖只看时间够不够
    stay = CONFIG["ad_stay"] + random.uniform(0, CONFIG["ad_stay_jitter"])
    log(f"  停留 {stay:.0f}s（不点任何按钮）…")
    time.sleep(stay)

    # 4) 万能穿透回主界面
    if not punch_home(d):
        log("  ! 穿透失败，改按返回键")
        for _ in range(4):
            try:
                d.press("back")
            except Exception:
                pass
            time.sleep(1.5)
            dismiss_popups(d)
            if is_main(d):
                break
    time.sleep(2)
    dismiss_popups(d)

    # 5) 校验：全看接口。界面提示不算数（实测有「提示失败、时长确实增加」，
    #    也有「文案说看完了、其实还差几次」）。
    after = read_progress(adb, serial)
    if not after["ok"]:
        log(f"  ✗ 读不到进度，本轮结果未知：{after['reason']}")
        return "error", before
    got = progressed(before, after)
    if got:
        log(f"  ✓ 成功  {got}   （{fmt_progress(after)}）")
        return "ok", after
    if quota_done(after["stages"]):
        return "done", after
    log(f"  ✗ 未生效  {fmt_progress(before)} → {fmt_progress(after)}")
    return "fail", after


# ----------------------------------------------------------------------------
# 辅助
# ----------------------------------------------------------------------------
def dump_ui(d):
    xml = d.dump_hierarchy()
    log("--- 当前界面文本节点 ---")
    for m in re.finditer(r"<node[^>]*>", xml):
        n = m.group(0)

        def g(k):
            r = re.search(k + r'="([^"]*)"', n)
            return r.group(1) if r else ""

        t = (g("text") or g("contentDescription")).strip()
        rid = g("resource-id").split("/")[-1]
        if t or rid:
            log(f"  {g('bounds')}  {rid[:45]:<45}  {t[:40]}")
    log(f"前台: {d.app_current()}")


RUN_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "EtalienDailyCheckin"


def cmd_renew_token():
    """只把凭据续一份新的：启模拟器 → 读 App 的 token 存进 cred.json → 关模拟器。

    给 watchdog 当**退路**用 —— 它那边补凭据是先 `etapi.py scan`（提权重抓 PC
    客户端，不启模拟器、约 1 秒），走不通才调这里。因为 PC 端凭据过期后没法
    自愈（客户端自己也不刷新），能续期的只有 App（它带 refresh/token 逻辑）。

    这里只借 App 的手刷新凭据，不刷广告、不动每日状态，所以不受 attempts 上限影响。
    """
    adb = find_adb()
    try:
        ensure_ready(adb)
    except SystemExit as e:
        log(f"✗ 续期失败：{e}")
        return 1
    r = read_progress(adb, _serial(), tries=1)
    try:
        shutdown_emulator()
    except Exception:
        pass
    if r["ok"]:
        log(f"✓ 凭据已续到最新：{fmt_progress(r)}")
        return 0
    log(f"✗ 续期失败：{r['reason']}")
    return 1


def cmd_install():
    """写进 HKCU 的 Run 键 → 每次登录自动跑一趟。

    触发多次是安全的：状态文件里记了 date + all_done，刷满了秒退，
    没刷满（中途关机 / 上次失败）就接着补。
    """
    import winreg

    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pyw):
        pyw = sys.executable
    # 登录后等 30s 再开跑：够 explorer 把自启项都拉起来、网络/磁盘稳定下来，
    # 又不至于把整个流程拖太久。
    cmdline = '"%s" "%s" --delay 30' % (pyw, os.path.abspath(__file__))

    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE)
    winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, cmdline)
    winreg.CloseKey(key)

    print("已安装开机自启（注册表 Run 项）：")
    print("  HKCU\\%s\\%s" % (RUN_KEY, RUN_NAME))
    print("  启动命令:", cmdline)


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只探测界面，不点击")
    ap.add_argument("--rounds", type=int, default=CONFIG["max_rounds"])
    ap.add_argument("--force", action="store_true", help="无视「今天已看完」和尝试上限，强制跑")
    ap.add_argument("--keep-emulator", action="store_true", help="跑完不关模拟器")
    ap.add_argument("--delay", type=int, default=0,
                    help="启动前先等这么多秒（自启时让系统先稳定下来）")
    ap.add_argument("--state", action="store_true", help="打印每日状态后退出（不连模拟器）")
    ap.add_argument("--install", action="store_true", help="安装开机自启（注册表 Run 项）")
    ap.add_argument("--uninstall", action="store_true", help="移除开机自启")
    ap.add_argument("--renew-token", action="store_true",
                    help="只续凭据（启模拟器读 App token），不刷广告 —— 供 watchdog 调用")
    args = ap.parse_args()

    if args.renew_token:
        return cmd_renew_token()
    if args.install:
        return cmd_install()
    if args.uninstall:
        return cmd_uninstall()
    if args.state:
        st = state_today()
        print(json.dumps(st, ensure_ascii=False, indent=2))
        print(f"#{st['date']} 已尝试 {st['attempts']} 次，"
              f"全部看完：{'是' if st['all_done'] else '否'}")
        return

    today = _today()

    # 去重（两个维度，缺一不可）：
    #   今天 + 已全部看完   → 跳过
    #   今天 + 没看完       → 继续补跑（电脑中途关机 / 上次失败）
    #   不是今天            → 必跑（额度 0 点重置）
    if not args.dry_run and not args.force:
        st = state_today()
        if st["all_done"]:
            log(f"今天（{today}）额度已全部看完，跳过。要强制重跑请加 --force。")
            return
        if st["attempts"] >= CONFIG["max_attempts_per_day"]:
            log(f"今天（{today}）已尝试 {st['attempts']} 次仍未看完"
                f"（上次：{st.get('progress') or '进度未知'}），今日不再重试（--force 可强制）。")
            return
        if st["attempts"]:
            log(f"今天（{today}）已跑过 {st['attempts']} 次但**未全部看完**"
                f"（上次：{st.get('progress') or '进度未知'}），接着补完剩余额度…")

    if args.delay > 0 and not args.dry_run:
        log(f"等待 {args.delay} 秒后开始（让系统先稳定下来）…")
        time.sleep(args.delay)

    d, adb = connect()

    if args.dry_run:
        dump_ui(d)
        return

    log("启动 app…")
    d.app_start(CONFIG["package"])
    time.sleep(5)
    dismiss_popups(d)

    all_done = False       # 今天额度是否已全部看完 → 决定以后还要不要补跑
    progress = None        # 进度快照，便于知道上次停在哪
    started = False        # 是否「有效尝试」：决定要不要把今天记进状态
    # 退出即彻底关掉模拟器（用户明确要求）。要留着现场排查/手动登录，
    # 加 --keep-emulator。
    shutdown = True
    try:
        # 登录状态以接口为准（能读到进度就是已登录）。界面文案只用来补充提示 ——
        # UI 会漏渲染、也会出现同名字样，拿它当判定条件会误退。
        cur = read_progress(adb, _serial())
        if not cur["ok"]:
            hint = "界面显示需要登录，请手动登录一次。" if not is_logged_in(d) else ""
            log(f"✗ 接口读不到进度（{cur['reason']}）{hint}本轮不写状态。"
                "（想留着模拟器排查，加 --keep-emulator）")
            return

        if quota_done(cur["stages"]):
            log(f"启动时接口已显示三档刷满：{fmt_progress(cur)}")
            started = True
            all_done = True
            progress = fmt_progress(cur)      # 让 --state 能看到停在哪
            return

        if not ensure_on_ads_page(d):
            log("✗ 未找到「看广告 领时长」入口，dump 当前界面。本轮不写状态。")
            dump_ui(d)
            return

        started = True
        bump_attempt()     # 立即落盘：中途被关机也算一次尝试，不会被无限重试
        log(f"起始：{fmt_progress(cur)}")

        ok = fail = error = 0
        for i in range(1, args.rounds + 1):
            log(f"--- 第 {i} 轮 ---")
            status, snap = "fail", cur
            for attempt in range(1 + CONFIG["retry_per_round"]):
                if attempt:
                    log(f"  重试 #{attempt}")
                    punch_home(d)
                    time.sleep(3)
                status, snap = run_one_round(d, adb, _serial())
                if status != "fail":
                    break
            if status == "ok":
                ok += 1
                fail = error = 0
            elif status == "done":
                log(f"接口显示三档已全部看完，收工（{fmt_progress(snap)}）")
                all_done = True
                break
            elif status == "error":
                # 接口不通时**不推断结果**：既不算成功也不算失败，重试几次还不行就收工
                error += 1
                if error >= 2:
                    log("接口一直读不到进度，停止（不猜、不误判；已完成的额度不会丢）")
                    break
                time.sleep(5)
                continue
            else:
                fail += 1
                if fail >= CONFIG["max_consecutive_fail"]:
                    log(f"连续 {fail} 轮失败，停止（已完成的额度不会丢，下次登录接着补）")
                    break

            # 每轮都刷一次进度快照：万一接下来被关机，也能看出停在哪
            progress = fmt_progress(snap)
            save_state(date=today, progress=progress)

            if i < args.rounds:
                gap = random.uniform(*CONFIG["round_gap"])
                log(f"  间隔 {gap:.0f}s（模拟真人节奏）")
                time.sleep(gap)

        final = read_progress(adb, _serial())
        progress = fmt_progress(final)
        if not all_done and final["ok"]:
            all_done = quota_done(final["stages"])
        log(f"===== 结束：成功 {ok} 轮，失败 {fail} 轮，{progress}，"
            f"{'已全部看完' if all_done else '未看完'} =====")
    finally:
        if started:
            save_state(date=today, all_done=all_done, progress=progress)
        if shutdown and not args.keep_emulator:
            shutdown_emulator()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("已被手动中断")
