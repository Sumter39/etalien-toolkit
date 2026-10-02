# -*- coding: utf-8 -*-
"""pause 行为 · 回归测试。

盯三点：失败不置成功位、fast 路径先落盘、让位时既不发请求也不算成功。

跑法：
    python -m unittest discover -s test -v
"""
import os
import unittest
from unittest import mock

from ..helpers import Isolated, api, pauser, state


class TestPause(Isolated):
    """pause：失败不置成功位、fast 路径先落盘、成功撤欠账。"""

    def test_网络失败时不置成功位(self):
        with mock.patch.object(api, "call", return_value=(None, b"URLError: 10061")):
            self.assertFalse(pauser.pause("测试"))
        self.assertTrue(state._STATE["want_pause"], "失败也要记住「期望暂停」")
        self.assertNotIn(state._STATE["last_result"], ("paused", "already-paused"))
        self.assertGreater(state._STATE["retry_at"], 0, "失败要排下一次重试")

    def test_让位时不发请求也不算成功(self):
        """闸门被别的触发源占用时，pause() 立刻返回 None 且没有落实暂停。

        这条容易误判：返回 None 只表示「不必我再发」，不等于服务端已暂停。
        所以此时绝不能把 last_result 写成 paused，否则轮询的补发逻辑会误以为
        事情已经办完而不再重试。
        """
        self.assertTrue(pauser._claim_pause("别的触发源"))
        called = []
        with mock.patch.object(api, "call",
                               side_effect=lambda *a, **k: called.append(a) or (200, b"")):
            pauser.pause("测试", fast=True, channel="本触发源")
        self.assertEqual(called, [], "让位时不该发请求")
        self.assertNotIn(state._STATE["last_result"], ("paused", "already-paused"),
                         "让位不等于成功，不能写成功位")
        pauser._release_pause(False)

    def test_fast路径发请求前先落盘(self):
        seen = {}

        def spy(*a, **k):
            seen["pending"] = os.path.exists(state.PEND_FILE)
            return 200, b""

        with mock.patch.object(api, "call", side_effect=spy):
            pauser.pause("系统关机/重启", fast=True)
        self.assertTrue(seen["pending"], "请求发出时欠账必须已经落盘了")

    def test_成功会撤掉欠账(self):
        with mock.patch.object(api, "call", return_value=(200, b"")):
            self.assertTrue(pauser.pause("测试", fast=True))
        self.assertIsNone(state.load_pending())

    def test_已是暂停态也算成功(self):
        body = b"can not update same pause state"
        with mock.patch.object(api, "call", return_value=(500, body)):
            self.assertTrue(pauser.pause("测试", fast=True))
        self.assertIsNone(state.load_pending())

    def test_非fast路径不写欠账(self):
        seen = {}

        def spy(*a, **k):
            seen["pending"] = os.path.exists(state.PEND_FILE)
            return 200, b""

        with mock.patch.object(api, "call", side_effect=spy):
            pauser.pause("锁屏")
        self.assertFalse(seen["pending"], "锁屏这类事件进程还活着，不必落盘")


if __name__ == "__main__":
    unittest.main(verbosity=2)
