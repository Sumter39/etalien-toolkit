# -*- coding: utf-8 -*-
"""adb 连接、root 与机型属性注入。

走 adb 而不是 MuMuManager.exe sh：后者超时杀不掉子进程，会把调用它的 Python
一起拖死（实测卡住 10 分钟以上）。命令本身以什么身份执行由调用方保证 ——
要写系统属性就先调 ensure_root()。
"""
import os
import time

from .. import config as cfg
from ..procs import run
from . import emulator
from .config import CONFIG, log


def shell(adb, cmd, timeout=30):
    """在模拟器里执行 shell 命令，返回 stdout 文本。"""
    try:
        r = run([adb, "-s", cfg.serial(), "shell", cmd],
                capture_output=True, timeout=timeout)
        return (r.stdout or b"").decode("utf-8", "ignore").strip()
    except Exception:
        return ""


def ensure_root(adb):
    """让 adb shell 拿到 root 身份 —— 注入 ro.product.* 需要它。

    MuMu 的 adbd 支持 adb root（回 restarting adbd as root），重启后 id
    就是 uid=0。不要改用 MuMuManager.exe sh：那条通道虽然天生是 root，
    但请求超时杀不掉子进程，会把调用方一起拖死（实测卡 10 分钟以上）。

    adbd 重启会断开连接，所以必须在「adb 已就绪」之后调。
    """
    if shell(adb, "id").startswith("uid=0"):
        return True
    try:
        run([adb, "-s", cfg.serial(), "root"], capture_output=True, timeout=30)
        run([adb, "-s", cfg.serial(), "wait-for-device"],
            capture_output=True, timeout=60)
    except Exception:
        pass
    for _ in range(8):
        if shell(adb, "id").startswith("uid=0"):
            log("adb 已切到 root")
            return True
        time.sleep(3)
    return False


def ensure_ready(adb):
    """确保：模拟器在跑 → adb 可连 → OAID / 品牌属性已注入。"""
    # 1) 模拟器实例
    if not emulator.started() and not emulator.start():
        raise SystemExit(1)

    # 2) 等 adb 可连
    log("等待模拟器 adb 就绪…")
    t0 = time.time()
    while time.time() - t0 < CONFIG["mumu_boot_timeout"]:
        run([adb, "connect", cfg.serial()], capture_output=True, timeout=20)
        out = run([adb, "devices"], capture_output=True,
                  timeout=20).stdout.decode("utf-8", "ignore")
        if cfg.serial() in out:
            log("adb 已就绪（%.0fs）" % (time.time() - t0))
            break
        time.sleep(5)
    else:
        log("✗ 等不到模拟器 adb 就绪（%ds）" % CONFIG["mumu_boot_timeout"])
        raise SystemExit(1)

    # 3) 注入 OAID 与品牌属性（MuMu 每次重启都会还原 ro.*），带重试验证。
    #    冷启动时系统可能还没就绪，命令会返回空 → 靠重试兜住。
    if not ensure_root(adb):
        log("⚠ 拿不到 adb root，品牌属性刷不进去 —— 广告多半无填充")
    oaid_value = cfg.oaid()
    brand = oaid = ""
    for i in range(8):
        shell(adb, "setprop persist.oaid %s" % oaid_value)
        for k in CONFIG["brand_props"]:
            shell(adb, "/data/adb/ksud resetprop %s %s" % (k, CONFIG["fake_brand"]))
        brand = shell(adb, "getprop ro.product.manufacturer")
        oaid = shell(adb, "getprop persist.oaid")
        if CONFIG["fake_brand"].lower() in brand.lower() and oaid_value in oaid:
            log("OAID 环境已就绪（brand=%s, oaid=%s…）" % (brand, oaid[:8]))
            return
        log("  属性未生效（brand=%r），重试 %d/8…" % (brand, i + 1))
        time.sleep(4)
    log("⚠ OAID 环境异常（brand=%r, oaid=%r）—— 广告可能无填充" % (brand, oaid[:12]))


def connect():
    """连上模拟器，返回 (uiautomator2 设备, adb 路径)。"""
    adb = cfg.adb_path()
    os.environ["ADBUTILS_ADB_PATH"] = adb
    os.environ["PATH"] = os.path.dirname(adb) + os.pathsep + os.environ.get("PATH", "")
    log("adb: %s" % adb)

    ensure_ready(adb)

    import uiautomator2 as u2
    try:
        d = u2.connect(cfg.serial())
    except Exception:
        d = u2.connect()
    info = d.info
    w, h = info.get("displayWidth") or 0, info.get("displayHeight") or 0
    log("已连接 %s  %dx%d" % (d.serial, w, h))
    if w > h:
        log("⚠ 当前是横屏。App 为竖屏设计，建议固定为竖屏：")
        log("  MuMuManager.exe setting -v %s -k resolution_mode -val phone.1" % cfg.vmindex())
    return d, adb
