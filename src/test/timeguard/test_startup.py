# -*- coding: utf-8 -*-
"""开机补账 · 回归测试。

startup_check 读欠账标记补发上次关机漏掉的那次暂停。三条要点：
有欠账就补、服务端已暂停就把账撤了、查不到状态照样补（欠账是确定的事）。

跑法：
    python -m unittest discover -s test -v
"""
import unittest
from unittest import mock

from ..helpers import Isolated, api, pauser, state


class TestStartupCheck(Isolated):
    """startup_check：开机把上次没发出去的暂停补上。"""

    def test_有欠账就补发(self):
        state.mark_pending("系统关机/重启")
        calls = []
        with mock.patch.object(api, "pause_state", return_value=(100, False)), \
                mock.patch.object(pauser, "pause",
                                  side_effect=lambda r, **k: calls.append(r) or True):
            pauser.startup_check()
        self.assertEqual(len(calls), 1)
        self.assertIn("开机补发", calls[0])

    def test_服务端已经暂停就不重发(self):
        state.mark_pending("系统关机/重启")
        calls = []
        with mock.patch.object(api, "pause_state", return_value=(100, True)), \
                mock.patch.object(pauser, "pause",
                                  side_effect=lambda r, **k: calls.append(r) or True):
            pauser.startup_check()
        self.assertEqual(calls, [])
        self.assertIsNone(state.load_pending(), "确认已暂停就该把欠账撤了")

    def test_查不到状态也要补发(self):
        state.mark_pending("系统关机/重启")
        calls = []
        with mock.patch.object(api, "pause_state", return_value=(None, None)), \
                mock.patch.object(pauser, "pause",
                                  side_effect=lambda r, **k: calls.append(r) or True):
            pauser.startup_check()
        self.assertEqual(len(calls), 1, "欠账是确定的事，不能因为查不到就算了")

    def test_没有欠账就什么都不做(self):
        calls = []
        with mock.patch.object(pauser, "pause",
                               side_effect=lambda r, **k: calls.append(r) or True):
            pauser.startup_check()
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
