# -*- coding: utf-8 -*-
"""自更新接管的离线用例：解析、下载判定、安装命令与三个分支。"""
import unittest
from unittest import mock

from ..helpers import Isolated        # noqa: F401  （顺带把 src 挂进 sys.path）
from etalien.adwatch import ui, update

MB = 1024 * 1024


class QuietLog:
    """把 update.log 换成空实现。

    不换的话，用例里的「发现更新弹窗」「已更新到 vX」会真写进 output/adwatch.log，
    和线上记录混在一起，事后没法分辨哪次是真的。
    """

    def setUp(self):
        super().setUp()
        p = mock.patch.object(update, "log")
        p.start()
        self.addCleanup(p.stop)


class Clock:
    """假时钟：sleep 直接把时间往前推，超时循环不会真等。"""

    def __init__(self):
        self.t = 0.0

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += max(float(s), 1.0)


class FakeDevice:
    """只认 textContains / text 两种选择器的假设备。"""

    def __init__(self, texts=None, main=True):
        self.texts = list(texts or [])
        self.main = main
        self.pressed = []
        self.clicked = []

    def __call__(self, **kw):
        val = next(iter(kw.values()))
        hit = next((t for t in self.texts if val in t), None)
        if hit is None:
            return FakeEl("", exists=False)
        el = FakeEl(hit)
        el.click = lambda: self.clicked.append(hit)
        return el

    def press(self, key):
        self.pressed.append(key)

    def app_current(self):
        if self.main:
            return {"activity": "com.etalien.booster.ui.MainActivity"}
        return {"activity": "com.android.packageinstaller.PackageInstallerActivity"}


class FakeEl:
    def __init__(self, text, exists=True):
        self._text = text
        self.exists = exists

    def get_text(self):
        return self._text

    def click(self):
        pass


def shell_by_size(sizes):
    """按顺序吐出 stat 的体积，序列走完后一直重复最后一个。"""
    seq = list(sizes)

    def _f(adb, cmd, timeout=30):
        if cmd.startswith("stat -c"):
            return str(seq.pop(0) if seq else sizes[-1])
        return ""
    return _f


class TestParse(unittest.TestCase):

    def test_识别弹窗上的版本号(self):
        d = FakeDevice(["发现新版本v3.17.10"])
        self.assertEqual(update.version_on_screen(d), "3.17.10")

    def test_没有弹窗时返回None(self):
        self.assertIsNone(update.version_on_screen(FakeDevice(["可暂停时长"])))

    def test_从dumpsys取已装版本(self):
        out = "    versionCode=331710000 minSdk=26\n    versionName=3.17.10\n"
        with mock.patch.object(update, "shell", return_value=out):
            self.assertEqual(update.installed_version("adb"), "3.17.10")

    def test_读不到版本返回None(self):
        with mock.patch.object(update, "shell", return_value=""):
            self.assertIsNone(update.installed_version("adb"))


class TestDownload(QuietLog, unittest.TestCase):

    def test_大小稳定才算下完(self):
        # 0 → 20MB（还在长）→ 20MB（稳定）→ 返回
        with mock.patch.object(update, "time", Clock()), \
                mock.patch.object(update, "shell", shell_by_size([0, 20 * MB, 20 * MB])):
            path, size = update.wait_download("adb", "3.17.10", timeout=60)
        self.assertTrue(path.endswith("/3.17.10.apk"))
        self.assertEqual(size, 20 * MB)

    def test_一直没下完就超时(self):
        with mock.patch.object(update, "time", Clock()), \
                mock.patch.object(update, "shell", shell_by_size([0])):
            self.assertEqual(update.wait_download("adb", "3.17.10", timeout=6), (None, 0))

    def test_半截文件不算下完(self):
        # 体积一直在变，哪怕已经超过阈值也不能拿去装
        with mock.patch.object(update, "time", Clock()), \
                mock.patch.object(update, "shell", shell_by_size([20 * MB, 21 * MB, 22 * MB])):
            self.assertEqual(update.wait_download("adb", "3.17.10", timeout=6), (None, 0))


class TestInstall(unittest.TestCase):

    def _run(self, pm_out):
        cmds = []

        def _shell(adb, cmd, timeout=30):
            cmds.append(cmd)
            return pm_out if cmd.startswith("pm install") else ""

        with mock.patch.object(update, "shell", _shell):
            ok = update.install("adb", "/sdcard/x/3.17.10.apk")
        return ok, cmds

    def test_成功(self):
        ok, cmds = self._run("Success")
        self.assertTrue(ok)
        self.assertIn("cp /sdcard/x/3.17.10.apk /data/local/tmp/etalien_update.apk", cmds[0])
        self.assertIn("pm install -r /data/local/tmp/etalien_update.apk", cmds[1])
        self.assertIn("rm -f /data/local/tmp/etalien_update.apk", cmds[2])

    def test_失败也清掉临时包(self):
        ok, cmds = self._run("Failure [INSTALL_FAILED_INVALID_APK]")
        self.assertFalse(ok)
        self.assertTrue(any(c.startswith("rm -f") for c in cmds))


class TestHandle(QuietLog, unittest.TestCase):

    def test_没有弹窗什么都不做(self):
        d = FakeDevice(["可暂停时长"])
        with mock.patch.object(update, "shell") as sh:
            self.assertEqual(update.handle(d, "adb"), "none")
        sh.assert_not_called()

    def test_弹窗版本与已装版本相同时只收掉(self):
        d = FakeDevice(["发现新版本v3.17.10", "立即更新"])
        with mock.patch.object(update, "shell", return_value="versionName=3.17.10"), \
                mock.patch.object(update, "time", Clock()):
            self.assertEqual(update.handle(d, "adb"), "none")
        self.assertIn("back", d.pressed)
        self.assertEqual(d.clicked, [])

    def test_没有弹窗时skip不按返回(self):
        # --no-update 在正常界面上也会被调用，按返回会把 App 退掉
        d = FakeDevice(["可暂停时长"])
        with mock.patch.object(update, "shell") as sh:
            update.skip(d, "adb")
        self.assertEqual(d.pressed, [])
        sh.assert_not_called()

    def test_下载超时算没做成(self):
        d = FakeDevice(["发现新版本v3.17.10", "立即更新"])
        cmds = []

        def _shell(adb, cmd, timeout=30):
            cmds.append(cmd)
            if "dumpsys" in cmd:
                return "versionName=3.16.10"
            return "0"

        with mock.patch.object(update, "shell", _shell), \
                mock.patch.object(update, "time", Clock()), \
                mock.patch.dict(update.CONFIG, {"update_download_timeout": 6}):
            self.assertEqual(update.handle(d, "adb"), "stuck")
        self.assertEqual(d.clicked, ["立即更新"])
        self.assertFalse(any(c.startswith("pm install") for c in cmds))

    def test_走完下载安装并重开(self):
        d = FakeDevice(["发现新版本v3.17.10", "立即更新"])
        state = {"ver": "3.16.10", "sizes": [30 * MB]}

        def _shell(adb, cmd, timeout=30):
            if "dumpsys" in cmd:
                return "versionName=%s" % state["ver"]
            if cmd.startswith("stat -c"):
                return str(state["sizes"][0])
            if cmd.startswith("pm install"):
                state["ver"] = "3.17.10"
                return "Success\nSuccess"
            return ""

        with mock.patch.object(update, "shell", _shell), \
                mock.patch.object(update, "time", Clock()):
            self.assertEqual(update.handle(d, "adb"), "ok")
        self.assertEqual(d.clicked, ["立即更新"])


class TestPopupGuard(unittest.TestCase):

    def test_更新弹窗不被当普通弹窗点掉(self):
        d = FakeDevice(["发现新版本v3.17.10", "关闭"])
        self.assertFalse(ui.dismiss_popups(d))
        self.assertEqual(d.clicked, [])


if __name__ == "__main__":
    unittest.main()
