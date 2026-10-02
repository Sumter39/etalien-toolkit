#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""外星仔加速器 · 环境自检与界面探测。

换机器、换模拟器版本或界面改版后先跑一次，输出并保存到 output/：

    1. 可用的 adb 路径
    2. 已连接的设备列表
    3. 外星仔包名
    4. 当前界面层级（uiautomator dump）
    5. 当前界面截图

用法：
    python scripts/probe.py
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import etconfig as CFG

ROOT = CFG.ROOT
OUT = os.path.join(ROOT, "output")

KEYWORDS = ("外星", "alien", "et001", "acceler", "star", "xingyu", "jiasu")


def log(msg):
    print(msg, flush=True)


def find_adb():
    """adb.exe 的路径 —— 只认 config.json 里的 adb_path。

    刻意不内置「常见安装目录」清单，也不扫盘：猜中一个别的模拟器自带 adb，
    会连到错的设备上，而且出错点被推到后面某一步，很难查。
    缺了就直接报「填 adb_path」，模板在 config.example.json。
    """
    return CFG.require(
        "adb_path", r"adb.exe 的完整路径，通常在 <MuMu 安装目录>\nx_main\adb.exe")


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
