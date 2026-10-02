# -*- coding: utf-8 -*-
"""暂停态判读 · 回归测试。

field4 只在暂停时出现（proto3 默认值不序列化），所以判定要写成 == 1，
「字段缺失」和「值为 0」得分开对待；读不到时返回双 None，不能据此推断状态。

跑法：
    python -m unittest discover -s test -v
"""
import unittest
from unittest import mock

from etalien.api import endpoints

from ..helpers import Isolated, api


class TestPauseStateParse(Isolated):
    """field4 语义：缺字段 = 加速中，不是「读不到」。"""

    def _resp(self, body):
        return mock.patch.object(endpoints, "call", return_value=(200, body))

    def test_暂停态(self):
        with self._resp(api.field(1, 27020) + api.field(3, 1790936117) + api.field(4, 1)):
            left, paused = api.pause_state()
        self.assertEqual(left, 27020)
        self.assertTrue(paused)

    def test_加速态字段缺失(self):
        with self._resp(api.field(1, 27018) + api.field(3, 1790936117)):
            left, paused = api.pause_state()
        self.assertEqual(left, 27018)
        self.assertFalse(paused, "字段缺失是「加速中」，不是「读不到」")

    def test_读不到时双None(self):
        with mock.patch.object(endpoints, "call", return_value=(401, b"token expired")):
            self.assertEqual(api.pause_state(), (None, None))
        with mock.patch.object(endpoints, "call", return_value=(None, b"URLError")):
            self.assertEqual(api.pause_state(), (None, None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
