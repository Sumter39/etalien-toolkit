# -*- coding: utf-8 -*-
"""DNS 预热 · 回归测试。

getaddrinfo() 是阻塞的系统调用，socket 的 timeout 管不到它（只覆盖 connect/send/recv）。
关机时 Windows 先拆网络组件再通知程序，那一刻解析可能长时间不返回，而系统判
「未响应」只有 5 秒。唯一的办法是提前解析好存起来 —— 见 prewarm()。

跑法：
    python -m unittest discover -s src/test -t src -v
"""
import socket
import unittest
from unittest import mock

from ..helpers import api


class TestPrewarm(unittest.TestCase):
    """prewarm 要把 BASE 的主机名缓存下来，dns_ready 如实反映缓存状态。"""

    def setUp(self):
        self.cache = dict(api.transport._DNS_CACHE)
        api.transport._DNS_CACHE.clear()

    def tearDown(self):
        api.transport._DNS_CACHE.clear()
        api.transport._DNS_CACHE.update(self.cache)

    def test_解析成功会进缓存(self):
        self.assertFalse(api.dns_ready())
        self.assertTrue(api.prewarm())
        self.assertTrue(api.dns_ready())

    def test_解析失败返回False且不进缓存(self):
        with mock.patch.object(socket, "getaddrinfo",
                               side_effect=socket.gaierror("解析不了")):
            self.assertFalse(api.prewarm())
        self.assertFalse(api.dns_ready(), "失败不该留下半截缓存")

    def test_缓存命中时不走解析(self):
        api.prewarm()
        with mock.patch.object(socket, "getaddrinfo",
                               side_effect=AssertionError("命中缓存却去解析了")):
            st, _ = api.call("/v2/account/remain/duration", b"", timeout=2,
                             token="x", dvc="y")
        self.assertIsNone(st, "网络不通时应当失败，但不该卡在解析上")

    def test_同一主机多次预热是幂等的(self):
        self.assertTrue(api.prewarm())
        first = api.transport._DNS_CACHE
        self.assertTrue(api.prewarm())
        self.assertEqual(first, api.transport._DNS_CACHE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
