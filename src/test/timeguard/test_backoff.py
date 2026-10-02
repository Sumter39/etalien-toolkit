# -*- coding: utf-8 -*-
"""失败状态机与退避重试 · 回归测试。

_settle 成功撤欠账、失败排退避；need_repause 只管「该暂停 + 没落实 + 过了退避」。
盯住的是「一次失败不能就此揭过」这个承诺。

跑法：
    python -m unittest discover -s test -v
"""
import time
import unittest

from ..helpers import Isolated, pauser, state


class TestSettle(Isolated):
    """_settle：成功撤欠账、失败排退避。"""

    def test_成功会撤欠账并清零退避(self):
        state.mark_pending("测试")
        state._STATE["retry_count"] = 3
        self.assertTrue(pauser._settle(True, "paused"))
        self.assertIsNone(state.load_pending())
        self.assertEqual(state._STATE["retry_count"], 0)
        self.assertEqual(state._STATE["retry_at"], 0)

    def test_失败会保留欠账并排下次重试(self):
        state.mark_pending("测试")
        self.assertFalse(pauser._settle(False, "net-error"))
        self.assertIsNotNone(state.load_pending(), "失败不能把欠账也撤了")
        self.assertEqual(state._STATE["retry_count"], 1)
        self.assertGreater(state._STATE["retry_at"], time.time())

    def test_退避间隔有上限(self):
        for _ in range(30):
            pauser._settle(False, "net-error")
        self.assertLessEqual(state._STATE["retry_at"] - time.time(),
                             pauser.PAUSE_RETRY_MAX + 1)

    def test_退避逐次拉长(self):
        delays = []
        for _ in range(4):
            state._STATE["retry_at"] = 0
            pauser._settle(False, "net-error")
            delays.append(state._STATE["retry_at"] - time.time())
        self.assertEqual(delays, sorted(delays))


class TestNeedRepause(Isolated):
    """need_repause：只在「该暂停 + 没落实 + 过了退避」时才补。"""

    def test_从没暂停过就不补(self):
        self.assertFalse(pauser.need_repause())

    def test_已落实就不补(self):
        state._STATE.update({"want_pause": True, "last_result": "paused"})
        self.assertFalse(pauser.need_repause())
        state._STATE["last_result"] = "already-paused"
        self.assertFalse(pauser.need_repause())

    def test_失败且过了退避就补(self):
        state._STATE.update({"want_pause": True, "last_result": "net-error",
                             "retry_at": time.time() - 1})
        self.assertTrue(pauser.need_repause())

    def test_退避期内不重复发(self):
        state._STATE.update({"want_pause": True, "last_result": "net-error",
                             "retry_at": time.time() + 60})
        self.assertFalse(pauser.need_repause(), "退避期内重复发会把日志刷爆")


if __name__ == "__main__":
    unittest.main(verbosity=2)
