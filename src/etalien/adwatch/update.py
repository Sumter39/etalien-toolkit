# -*- coding: utf-8 -*-
"""App 自更新的就地接管。

弹「发现新版本」时不能放它挡着：那一层盖住了「看广告 领时长」的入口，
主流程判定失败就会关模拟器，而下载/安装是一断就得从头再来的事。

流程：点「立即更新」→ 等 App 自己把 APK 下完 → pm install -r → 重开 App 回主界面。
只动 App 和 adb，全程不碰模拟器进程；没做成时由调用方保住实例，别让它被关掉。
"""
import re
import time

from . import ui
from .adb import shell
from .config import CONFIG, log

APK_DIR = "/sdcard/Android/data/%s/files" % CONFIG["package"]
TMP_APK = "/data/local/tmp/etalien_update.apk"
MIN_APK = 10 * 1024 * 1024          # 小于这个体积的一律当没下完


def version_on_screen(d):
    """更新弹窗上的目标版本号；界面上没有这个弹窗返回 None。"""
    try:
        el = d(textContains="发现新版本")
        if not el.exists:
            return None
        text = el.get_text() or ""
    except Exception:
        return None
    m = re.search(r"(\d+\.\d+\.\d+)", text)
    return m.group(1) if m else None


def installed_version(adb):
    """模拟器里已安装的版本号；读不到返回 None。"""
    out = shell(adb, "dumpsys package %s | grep versionName | head -1" % CONFIG["package"])
    m = re.search(r"versionName=(\S+)", out)
    return m.group(1) if m else None


def _size(adb, path):
    out = shell(adb, "stat -c %%s %s 2>/dev/null" % path)
    return int(out) if out.isdigit() else 0


def wait_download(adb, ver, timeout=None):
    """等 App 把新版 APK 下完，返回 (设备路径, 字节数)；超时返回 (None, 0)。

    App 直接写最终文件名、中途不改名（实测 files/3.17.10.apk），所以只能用
    「两次采样大小一致」判完成 —— 光看文件出现会把半截文件送去安装。
    """
    path = "%s/%s.apk" % (APK_DIR, ver)
    timeout = CONFIG["update_download_timeout"] if timeout is None else timeout
    t0 = time.time()
    last = -1
    while time.time() - t0 < timeout:
        size = _size(adb, path)
        if size >= MIN_APK and size == last:
            return path, size
        if size and size != last:
            log("  下载中 %s（%.1f MB）" % (ver, size / 1048576.0))
        last = size
        time.sleep(3)
    return None, 0


def install(adb, path, timeout=None):
    """装新版，返回是否成功。

    不能直接 `adb install <设备路径>` —— adb 客户端会把它当主机上的文件去 stat；
    而 App 私有目录在 shell 侧又被 scoped storage 挡着，所以先 root 拷到
    /data/local/tmp 再交给设备端的 pm 装。`-r` 保数据，登录态和额度都还在。
    """
    timeout = CONFIG["update_install_timeout"] if timeout is None else timeout
    shell(adb, "cp %s %s && chmod 644 %s" % (path, TMP_APK, TMP_APK), timeout=120)
    try:
        out = shell(adb, "pm install -r %s" % TMP_APK, timeout=timeout)
    finally:
        shell(adb, "rm -f %s" % TMP_APK)
    return "Success" in out


def _relaunch(d, adb, timeout=90):
    """把 App 拉回主界面。装完包进程会被系统杀掉，必须重开一次。"""
    shell(adb, "am start -n %s" % CONFIG["main_activity"], timeout=30)
    t0 = time.time()
    while time.time() - t0 < timeout:
        if ui.is_main(d):
            return True
        time.sleep(2)
    return ui.is_main(d)


def handle(d, adb):
    """有更新弹窗就把它做完。返回 none 无弹窗 / ok 已更新 / stuck 没做成。"""
    ver = version_on_screen(d)
    if not ver:
        return "none"

    cur = installed_version(adb)
    if cur == ver:
        log("  更新弹窗是旧的（已装 v%s），收掉它" % ver)
        skip(d, adb)
        return "none"

    log("⚠ 发现更新弹窗 v%s（当前 v%s）—— 就地更新，不重启模拟器" % (ver, cur))

    el, _ = ui.find_any(d, ["立即更新"], timeout=3)
    if not el:
        log("  ✗ 找不到「立即更新」按钮")
        return "stuck"
    try:
        el.click()
    except Exception as e:
        log("  ✗ 点「立即更新」失败: %s" % e)
        return "stuck"

    path, size = wait_download(adb, ver)
    if not path:
        log("  ✗ 等不到 APK 下载完成（%ds）" % CONFIG["update_download_timeout"])
        return "stuck"
    log("  下载完成 %.1f MB，开始安装（App 会短暂退出，模拟器不动）" % (size / 1048576.0))

    # 下完后 App 会抛一个「打开方式」选择器，先清掉这一层，别让它盖在最后
    for _ in range(2):
        try:
            if d(text="打开方式").exists:
                d.press("back")
                time.sleep(1)
        except Exception:
            pass

    if not install(adb, path):
        log("  ✗ 安装失败（APK 可能没下全，或签名对不上）")
        return "stuck"

    got = installed_version(adb)
    if got != ver:
        log("  ✗ 装完版本对不上（期望 %s，实际 %s）" % (ver, got))
        return "stuck"
    log("  ✓ 已更新到 v%s" % got)

    if not _relaunch(d, adb):
        log("  ! 更新后没回到主界面，接着按界面流程试")
    time.sleep(3)
    return "ok"


def skip(d, adb):
    """收掉更新弹窗、不装（--no-update 与「弹窗是旧的」两种情况用）。"""
    if not version_on_screen(d):
        return                  # 没有弹窗就别按返回，会把 App 退掉
    try:
        d.press("back")
        time.sleep(1)
    except Exception:
        pass
    if not ui.is_main(d):
        _relaunch(d, adb)
