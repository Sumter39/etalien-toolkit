# -*- coding: utf-8 -*-
"""pause 行为 · 回归测试。

盯三点：失败不置成功位、成功撤掉免重发窗口、让位时既不发请求也不算成功。

跑法：
    python -m unittest discover -s test -v
"""
import unittest
from unittest import mock

from ..helpers import Isolated, api, pauser, state


class TestPause(Isolated):
    """pause：失败不置成功位、成功打开免重发窗口、让位时静默返回。"""

    def test_网络失败时不置成功位(self):
        with mock.patch.object(api, "call", return_value=(None, b"URLError: 10061")):
            self.assertFalse(pauser.pause("测试"))
        self.assertEqual(state._STATE["last_result"], "net-error")
        self.assertEqual(pauser._INFLIGHT["done_ok_until"], 0.0,
                         "失败不该打开免重发窗口")

    def test_让位时不发请求也不算成功(self):
        """闸门处于免重发窗口时，pause() 立刻返回 None 且没有落实暂停。

        这条容易误判：返回 None 只表示「不必我再发」，不等于服务端已暂停。
        所以此时绝不能把 last_result 写成 paused。
        """
        self.hold_gate()
        called = []
        with mock.patch.object(api, "call",
                               side_effect=lambda *a, **k: called.append(a) or (200, b"")):
            self.assertIsNone(pauser.pause("测试", fast=True, channel="本触发源"))
        self.assertEqual(called, [], "让位时不该发请求")
        self.assertNotIn(state._STATE["last_result"], ("paused", "already-paused"),
                         "让位不等于成功，不能写成功位")

    def test_成功会打开免重发窗口(self):
        with mock.patch.object(api, "call", return_value=(200, b"")):
            self.assertTrue(pauser.pause("测试", fast=True))
        self.assertGreater(pauser._INFLIGHT["done_ok_until"], 0, "成功后该短暂免重发")

    def test_已是暂停态也算成功(self):
        body = b"can not update same pause state"
        with mock.patch.object(api, "call", return_value=(500, body)):
            self.assertTrue(pauser.pause("测试", fast=True))

    def test_fast路径不换凭据(self):
        """关机路径撞 401 直接放行，不能在这里再等一次凭据探活。"""
        with mock.patch.object(api, "call", return_value=(401, b"token expired")), \
                mock.patch.object(pauser, "resolve_cred") as m:
            self.assertFalse(pauser.pause("系统关机/重启", fast=True))
        m.assert_not_called()

    def test_非fast路径会换凭据重试(self):
        seq = [(401, b"token expired"), (200, b"")]
        with mock.patch.object(api, "call", side_effect=seq), \
                mock.patch.object(pauser, "resolve_cred", return_value=(True, "ok")):
            self.assertTrue(pauser.pause("锁屏"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
