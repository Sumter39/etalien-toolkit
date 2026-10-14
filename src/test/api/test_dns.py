# -*- coding: utf-8 -*-
"""DNS 预热 · 回归测试。

getaddrinfo() 是阻塞的系统调用，socket 的 timeout 管不到它（只覆盖 connect/send/recv）。
关机时 Windows 先拆网络组件再通知程序，那一刻解析可能十几秒不返回，而系统判「未响应」
只有 5 秒 —— 本机就因此在 2026-10-14 被当成「阻止关机」杀掉过一次。

所以解析要提前做好，而且得真正接进请求路径：urllib 不会来查我们的缓存，
只能换掉 socket.getaddrinfo —— 见 prewarm() 与 _install_cache()。

跑法：
    python -m unittest discover -s src/test -t src -v
"""
import socket
import unittest
from unittest import mock

from ..helpers import api

HOST = "api.et-api.com"
ARGS = (443, 0, socket.SOCK_STREAM, 0, 0)


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
        with mock.patch.object(api.transport, "_REAL_GETADDRINFO",
                               side_effect=socket.gaierror("解析不了")):
            self.assertFalse(api.prewarm())
        self.assertFalse(api.dns_ready(), "失败不该留下半截缓存")

    def test_命中缓存时不走解析(self):
        """判据：缓存热了之后，解析函数一次都不该被调用。"""
        api.prewarm()
        with mock.patch.object(api.transport, "_REAL_GETADDRINFO",
                               side_effect=AssertionError("命中缓存却去解析了")):
            addrs = socket.getaddrinfo(HOST, *ARGS)
        self.assertTrue(addrs)

    def test_端口不同时不拿缓存顶包(self):
        api.prewarm()
        with mock.patch.object(api.transport, "_REAL_GETADDRINFO",
                               return_value=["真实解析的结果"]) as real:
            out = socket.getaddrinfo(HOST, 80, 0, socket.SOCK_STREAM)
        real.assert_called_once()
        self.assertEqual(out, ["真实解析的结果"])

    def test_连接层失败会丢掉缓存(self):
        api.prewarm()
        with mock.patch.object(api.transport._opener, "open",
                               side_effect=OSError("连不上")):
            st, _ = api.call("/v2/account/remain/duration", b"", timeout=1,
                             token="x", dvc="y")
        self.assertIsNone(st)
        self.assertFalse(api.dns_ready(), "连接失败后缓存该作废，下次重新解析")

    def test_同一主机多次预热是幂等的(self):
        self.assertTrue(api.prewarm())
        first = dict(api.transport._DNS_CACHE)
        self.assertTrue(api.prewarm())
        self.assertEqual(first, api.transport._DNS_CACHE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
