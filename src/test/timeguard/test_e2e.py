# -*- coding: utf-8 -*-
"""端到端 · 真的向运行中的守护进程投递一条 WM_QUERYENDSESSION。

那只是一条窗口消息，不会真的关机；但会触发一次真实（幂等的）暂停请求，
所以默认跳过，设 ETA_E2E=1 才跑。注意本类不继承 Isolated —— 它要看的就是真实日志。

跑法：
    ETA_E2E=1 python -m unittest discover -s test -v
"""
import os
import time
import unittest

from ..helpers import state


@unittest.skipUnless(os.name == "nt", "仅 Windows")
class TestShutdownMessageE2E(unittest.TestCase):

    @unittest.skipUnless(os.environ.get("ETA_E2E") == "1", "设 ETA_E2E=1 才跑")
    def test_投递关机消息后进程会去暂停(self):
        import ctypes
        try:
            import win32gui
        except ImportError:
            self.skipTest("缺 pywin32，换 venv 里的解释器")

        hwnd = win32gui.FindWindow("EtalienTimeGuardWnd", None)
        if not hwnd:
            self.skipTest("守护进程没在运行，无处投递")

        def tail():
            with open(state.LOG_FILE, encoding="utf-8", errors="replace") as f:
                return f.read().splitlines()

        before = len(tail())
        u32 = ctypes.windll.user32
        u32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                     ctypes.c_void_p, ctypes.c_void_p]
        self.assertTrue(u32.PostMessageW(hwnd, 0x0011, 0, 0), "PostMessageW 失败")

        time.sleep(4)
        text = "\n".join(tail()[before:])
        self.assertIn("收到系统结束会话通知", text, "守护进程没反应：\n" + text)
        self.assertTrue(
            any(k in text for k in ("已暂停计时", "暂停失败", "本来就是暂停状态")),
            "走了通知路径却没留下暂停结果：\n" + text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
