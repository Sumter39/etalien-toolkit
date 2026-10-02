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
    python src/tools/probe.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from etalien import OUT_DIR                   # noqa: E402
from etalien import config as cfg              # noqa: E402
from etalien.procs import capture             # noqa: E402

KEYWORDS = ("外星", "alien", "et001", "acceler", "star", "xingyu", "jiasu")


def log(msg):
    print(msg, flush=True)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    report = []

    # ---- 1. adb ----
    adb = cfg.adb_path()
    log("[OK] adb: %s" % adb)
    report.append("adb: %s" % adb)

    # ---- 2. 设备 ----
    _, out, err = capture([adb, "devices", "-l"])
    log("[--] adb devices -l\n%s\n%s" % (out, err))
    report.append("devices:\n" + out)

    devices = [l.split()[0] for l in out.splitlines()[1:] if l.strip() and "\t" in l]
    devices = [d for d in devices if d]
    if not devices:
        log("[X] 没有检测到设备。请确认模拟器已启动、ADB 调试已开启。")
        return 1
    dev = devices[0]
    log("[OK] 设备: %s" % dev)

    def sh(*args, **kw):
        return capture([adb, "-s", dev] + list(args), **kw)

    # ---- 3. 包名 ----
    _, out, _ = sh("shell", "pm", "list", "packages", "-3")
    third_party = [l.replace("package:", "").strip()
                   for l in out.splitlines() if l.startswith("package:")]
    log("[--] 第三方应用 %d 个" % len(third_party))

    candidates = [p for p in third_party if any(k in p.lower() for k in KEYWORDS)]
    log("[--] 疑似外星仔包名: %s" % candidates)

    if not candidates:
        log("[!] 关键词没匹配到。下面列出全部第三方包名，请人工认一下：")
        for p in third_party:
            log("    " + p)
        report.append("all_packages:\n" + "\n".join(third_party))
    else:
        report.append("candidate_packages:\n" + "\n".join(candidates))

    # ---- 4. dump 界面 + 截图 ----
    _, out, _ = sh("shell", "dumpsys", "window", "windows")
    focus = ""
    for line in out.splitlines():
        if "mCurrentFocus" in line or "mFocusedApp" in line:
            focus += line.strip() + "\n"
    log("[--] 当前焦点窗口:\n%s" % focus)
    report.append("focus:\n" + focus)

    # uiautomator dump（需要 app 在前台才准）
    sh("shell", "uiautomator", "dump", "/sdcard/ui.xml")
    _, out, _ = sh("shell", "cat", "/sdcard/ui.xml")
    if out:
        fp = os.path.join(OUT_DIR, "ui_dump.xml")
        with open(fp, "w", encoding="utf-8") as f:
            f.write(out)
        log("[OK] 界面层级已保存: %s" % fp)

    # 截图
    sh("shell", "screencap", "-p", "/sdcard/screen.png")
    capture([adb, "-s", dev, "pull", "/sdcard/screen.png",
             os.path.join(OUT_DIR, "screen.png")])
    log("[OK] 截图已保存: %s" % os.path.join(OUT_DIR, "screen.png"))

    fp = os.path.join(OUT_DIR, "probe_report.txt")
    with open(fp, "w", encoding="utf-8") as f:
        f.write("\n\n".join(report))
    log("\n[OK] 报告已保存: %s" % fp)
    log("界面改版或换环境时，拿这份报告重新核对 adwatch/config.py 里的文案常量。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
