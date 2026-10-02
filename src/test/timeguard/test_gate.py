# -*- coding: utf-8 -*-
"""并发闸门 · 回归测试。

消息循环（关机/睡眠）和 poll_loop（客户端退出/锁屏/空闲/补发）是两个线程，
都可能在同一刻喊暂停。它们靠 _claim_pause 抢闸门 —— 只有一条真正发请求，
其余让位。这里盯住这套闸门的行为，全程离线、不碰 output/。

覆盖：
    ① 并发调 pause() 只发一次请求；
    ② 任一触发成功后，后续一律让位（不再重复发）；
    ③ 失败的那条会释放闸门，让别的继续试；
    ④ reset_pause_gate() 能重新开放闸门（非关机路径每次触发都要能发）；
    ⑤ channel 参数会写进日志，便于事后归因。

跑法：
    python -m unittest discover -s test -v
"""
import os
import threading
import time
import unittest
from unittest import mock

from ..helpers import Isolated, api, pauser, state


class TestPauseGate(Isolated):
    def test_001_只有一条触发源抢到闸门(self):
        self.assertTrue(pauser._claim_pause("A"))
        # 闸门已被 A 占用，后续一律让位
        self.assertFalse(pauser._claim_pause("B"))
        self.assertFalse(pauser._claim_pause("C"))

    def test_002_成功后其余一律让位(self):
        self.assertTrue(pauser._claim_pause("消息"))
        pauser._release_pause(True)          # 成功
        for ch in ("ENDSESSION", "轮询/锁屏", "轮询/补发"):
            self.assertFalse(pauser._claim_pause(ch),
                             "已有触发成功后 %s 不该再发" % ch)

    def test_003_失败后闸门放开让别人试(self):
        self.assertTrue(pauser._claim_pause("消息"))
        pauser._release_pause(False)         # 失败
        self.assertTrue(pauser._claim_pause("轮询/补发"),
                        "前一条失败后，后面那条必须还能发")

    def test_004_重置后闸门重新开放(self):
        pauser._claim_pause("消息")
        pauser._release_pause(True)          # 成功 → done_ok
        pauser.reset_pause_gate()
        self.assertTrue(pauser._claim_pause("新的场景"),
                        "reset 之后必须能重新发（非关机场景每次都该发）")

    def test_005_并发调pause只发一次请求(self):
        calls = []

        def fake_call(path, body=b"", **kw):
            calls.append(path)
            time.sleep(0.05)            # 模拟网络往返，放大并发窗口
            return 200, b""

        with mock.patch.object(api, "call", side_effect=fake_call):
            threads = [threading.Thread(target=pauser.pause,
                                        kwargs={"reason": "并发%d" % i,
                                                "fast": True,
                                                "channel": "ch%d" % i})
                       for i in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)

        self.assertEqual(len(calls), 1,
                         "两个线程并发只该发 1 次请求，实际 %d 次" % len(calls))

    def test_006_日志里带触发源名(self):
        with mock.patch.object(api, "call", return_value=(200, b"")):
            pauser.pause("测试", fast=False, channel="消息")
        with open(state.LOG_FILE, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("← 消息", text, "日志必须标出是哪个触发源发的")

    def test_007_让位时不写欠账(self):
        self.assertTrue(pauser._claim_pause("消息"))
        with mock.patch.object(api, "call", return_value=(200, b"")):
            # 让位的那条（fast=True）不该落盘欠账 —— 它根本没发请求
            pauser.pause("让位测试", fast=True, channel="ENDSESSION")
        self.assertFalse(os.path.exists(state.PEND_FILE),
                         "让位时不该落盘欠账文件")


if __name__ == "__main__":
    unittest.main(verbosity=2)
