# -*- coding: utf-8 -*-
"""DNS 超时保护 · 回归测试。

urlopen(timeout=) 只覆盖 connect/send/recv，管不到 getaddrinfo —— 它是阻塞的
系统调用，关机时可能长时间不返回，把请求拖到进程被系统杀掉。
install_dns_timeout 负责让它快速失败，转去走「落盘欠账 + 开机补发」。

跑法：
    python -m unittest discover -s test -v
"""
import socket
import time
import unittest

from ..helpers import api


class TestDnsTimeout(unittest.TestCase):
    """install_dns_timeout 要拦得住「卡住的 getaddrinfo」。"""

    def setUp(self):
        self.orig = socket.getaddrinfo

    def tearDown(self):
        socket.getaddrinfo = self.orig

    def test_卡住的解析会在超时后抛错(self):
        def hang(*a, **k):
            time.sleep(10)
            return []

        socket.getaddrinfo = hang          # 先换成慢的，再包一层
        api.install_dns_timeout(1.0)
        t0 = time.time()
        with self.assertRaises(socket.gaierror):
            socket.getaddrinfo("example.com", 80)
        self.assertLess(time.time() - t0, 3, "超时没生效，还是被卡住了")

    def test_真实请求在DNS卡住时快速失败(self):
        def hang(*a, **k):
            time.sleep(10)
            return []

        socket.getaddrinfo = hang
        api.install_dns_timeout(1.0)
        t0 = time.time()
        st, _ = api.call("/v2/account/remain/duration", b"", timeout=2)
        self.assertIsNone(st, "DNS 卡住时应当失败，而不是假装成功")
        self.assertLess(time.time() - t0, 3, "请求被 DNS 拖了 10 秒 —— 超时保护失效")

    def test_正常解析不受影响(self):
        api.install_dns_timeout(2.0)
        self.assertTrue(socket.getaddrinfo("api.et-api.com", 443))


if __name__ == "__main__":
    unittest.main(verbosity=2)
