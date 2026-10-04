# -*- coding: utf-8 -*-
"""并发闸门 · 回归测试。

消息循环（关机/睡眠）和 poll_loop（客户端退出/锁屏/空闲）是两个线程，都可能在同一
刻喊暂停。它们靠 _claim_pause 抢闸门 —— 只有一条真正发请求，其余让位。
这里盯住这套闸门的行为，全程离线、不碰 output/。

覆盖：
    ① 成功后短暂免重发；窗口过期后必须能重新发；
    ② 失败的那条会释放闸门，让别的继续试；
    ③ channel 参数会写进日志，便于事后归因。

跑法：
    python -m unittest discover -s test -v
"""
import threading
import time
import unittest
from unittest import mock

from ..helpers import Isolated, api, pauser, state


class TestPauseGate(Isolated):
    def test_001_窗口内只有一条触发源抢到闸门(self):
        self.assertTrue(pauser._claim_pause())
        pauser._release_pause(True)          # 成功 → 打开免重发窗口
        self.assertFalse(pauser._claim_pause())
        self.assertFalse(pauser._claim_pause())

    def test_002_免重发窗口会过期(self):
        """成功位必须带 TTL —— 否则关机成功后，之后每一次暂停都被短路。"""
        self.assertTrue(pauser._claim_pause())
        pauser._release_pause(True)
        self.assertFalse(pauser._claim_pause(), "窗口内应该让位")
        pauser._INFLIGHT["done_ok_until"] = time.time() - 0.01   # 把时间往前拨
        self.assertTrue(pauser._claim_pause(), "窗口过期后必须能重新发")

    def test_003_失败后闸门放开让别人试(self):
        self.assertTrue(pauser._claim_pause())
        pauser._release_pause(False)         # 失败
        self.assertTrue(pauser._claim_pause(),
                        "前一条失败后，后面那条必须还能发")

    def test_004_成功之后并发全部让位(self):
        """成功后打开免重发窗口，紧接着的并发一律只打一次真实请求。"""
        calls = []

        def fake_call(path, body=b"", **kw):
            calls.append(path)
            return 200, b""

        with mock.patch.object(api, "call", side_effect=fake_call):
            self.assertTrue(pauser.pause("第一条", fast=True))
            threads = [threading.Thread(target=pauser.pause,
                                        kwargs={"reason": "后续%d" % i, "channel": "ch%d" % i})
                       for i in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)

        self.assertEqual(len(calls), 1,
                         "成功后的 6 条并发只该让位，实际发了 %d 次" % len(calls))

    def test_005_日志里带触发源名(self):
        with mock.patch.object(api, "call", return_value=(200, b"")):
            pauser.pause("测试", fast=False, channel="消息")
        with open(state.LOG_FILE, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("← 消息", text, "日志必须标出是哪个触发源发的")


if __name__ == "__main__":
    unittest.main(verbosity=2)
