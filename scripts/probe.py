#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
外星仔加速器 · 环境自检与界面探测
=====================================
换机器 / 换模拟器版本 / 界面改版后，先跑这个脚本摸一遍环境。

它会输出并保存到 output/：
  1. 可用的 adb 路径
  2. 已连接的设备列表
  3. 外星仔加速器的包名
  4. 当前界面的层级结构（uiautomator dump）
  5. 当前界面截图

用法：
  python scripts/probe.py
"""
import os
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import etconfig as CFG

ROOT = CFG.ROOT
OUT = os.path.join(ROOT, "output")

# 常见模拟器自带的 adb 位置（兜底用；config.json 里的 adb_path 优先）
ADB_CANDIDATES = [
    r"E:\MuMu\shell\adb.exe",
    r"D:\MuMu\shell\adb.exe",
    r"C:\MuMu\shell\adb.exe",
    r"E:\MuMuPlayer\shell\adb.exe",
    r"D:\MuMuPlayer\shell\adb.exe",
    r"C:\Program Files\Netease\MuMuPlayer\shell\adb.exe",
    r"C:\Program Files\Netease\MuMu Player 5\shell\adb.exe",
    r"C:\Program Files\Netease\MuMuPlayer-5.0\shell\adb.exe",
    # MuMu 12 旧版目录
    r"C:\Program Files\Netease\MuMu Player 12\shell\adb.exe",
    r"C:\Program Files\Netease\MuMuPlayer-12.0\shell\adb.exe",
    r"C:\Program Files (x86)\Netease\MuMu Player 12\shell\adb.exe",
    r"D:\Program Files\Netease\MuMu Player 12\shell\adb.exe",
    r"D:\Program Files\Netease\MuMuPlayer-12.0\shell\adb.exe",
    r"C:\MuMu\emulator\nemu\vmonitor\bin\adb_server.exe",
    # 备选模拟器
    r"C:\Program Files\Nox\bin\adb.exe",
    r"C:\Nox\bin\adb.exe",
    r"D:\Nox\bin\adb.exe",
    r"C:\LDPlayer\LDPlayer9\adb.exe",
    r"C:\LDPlayer\LDPlayer64\adb.exe",
    r"D:\LDPlayer\LDPlayer9\adb.exe",
    r"D:\LDPlayer9\adb.exe",
    r"C:\Program Files\ldplayer\LDPlayer9\adb.exe",
    r"C:\Program Files\BlueStacks_nxt\HD-Adb.exe",
]

KEYWORDS = ("外星", "alien", "et001", "acceler", "star", "xingyu", "jiasu")


def log(msg):
    print(msg, flush=True)


def find_adb():
    """定位 adb.exe：config.json 指定 > PATH > 模拟器常见目录 > 兜底扫描。"""
    cfg = CFG.get("adb_path")
    if cfg and os.path.isfile(cfg):
        return cfg
    p = shutil.which("adb")
    if p:
        return p
    for c in ADB_CANDIDATES:
        if os.path.isfile(c):
            return c
    # 兜底：扫一层盘符下的常见目录
    import glob
    for drive in ("C:", "D:", "E:"):
        for pat in (r"\*\*\shell\adb.exe", r"\*\adb.exe", r"\*\*\adb.exe",
                    r"\*\*\*\adb.exe"):
            hits = glob.glob(drive + pat)
            if hits:
                return hits[0]
    return None


def run(cmd, timeout=30):
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        out = (r.stdout or b"").decode("utf-8", "ignore")
        err = (r.stderr or b"").decode("utf-8", "ignore")
        return r.returncode, out.strip(), err.strip()
    except Exception as e:
        return -1, "", str(e)


def main():
    os.makedirs(OUT, exist_ok=True)
    report = []

    # ---- 1. adb ----
    adb = find_adb()
    if not adb:
        log("[X] 没找到 adb.exe")
        log("    → 确认模拟器已安装。MuMu 12 的 adb 通常在:")
        log("      <安装目录>\\shell\\adb.exe")
        log("    → 若仍找不到，把模拟器安装目录告诉我。")
        return 1
    log(f"[OK] adb: {adb}")
    report.append(f"adb: {adb}")

    # ---- 2. 设备 ----
    code, out, err = run([adb, "devices", "-l"])
    log(f"[--] adb devices -l\n{out}\n{err}")
    report.append("devices:\n" + out)

    devices = [l.split()[0] for l in out.splitlines()[1:] if l.strip() and "\t" in l]
    devices = [d for d in devices if d]
    if not devices:
        log("[X] 没有检测到设备。请确认模拟器已启动、ADB 调试已开启。")
        return 1
    dev = devices[0]
    log(f"[OK] 设备: {dev}")

    def sh(*args, **kw):
        return run([adb, "-s", dev, *args], **kw)

    # ---- 3. 包名 ----
    code, out, err = sh("shell", "pm", "list", "packages", "-3")
    third_party = [l.replace("package:", "").strip() for l in out.splitlines() if l.startswith("package:")]
    log(f"[--] 第三方应用 {len(third_party)} 个")

    candidates = [p for p in third_party if any(k in p.lower() for k in KEYWORDS)]
    log(f"[--] 疑似外星仔包名: {candidates}")

    if not candidates:
        log("[!] 关键词没匹配到。下面列出全部第三方包名，请人工认一下：")
        for p in third_party:
            log("    " + p)
        report.append("all_packages:\n" + "\n".join(third_party))
    else:
        report.append("candidate_packages:\n" + "\n".join(candidates))

    # ---- 4. dump 界面 + 截图 ----
    code, out, err = sh("shell", "dumpsys", "window", "windows")
    focus = ""
    for line in out.splitlines():
        if "mCurrentFocus" in line or "mFocusedApp" in line:
            focus += line.strip() + "\n"
    log(f"[--] 当前焦点窗口:\n{focus}")
    report.append("focus:\n" + focus)

    # uiautomator dump（需要 app 在前台才准）
    sh("shell", "uiautomator", "dump", "/sdcard/ui.xml")
    code, out, err = sh("shell", "cat", "/sdcard/ui.xml")
    if out:
        fp = os.path.join(OUT, "ui_dump.xml")
        with open(fp, "w", encoding="utf-8") as f:
            f.write(out)
        log(f"[OK] 界面层级已保存: {fp}")

    # 截图
    sh("shell", "screencap", "-p", "/sdcard/screen.png")
    code, _, _ = run([adb, "-s", dev, "pull", "/sdcard/screen.png",
                      os.path.join(OUT, "screen.png")])
    log(f"[OK] 截图已保存: {os.path.join(OUT, 'screen.png')}")

    fp = os.path.join(OUT, "probe_report.txt")
    with open(fp, "w", encoding="utf-8") as f:
        f.write("\n\n".join(report))
    log(f"\n[OK] 报告已保存: {fp}")
    log("界面改版或换环境时，拿这份报告重新核对 checkin.py 里的文案常量。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
