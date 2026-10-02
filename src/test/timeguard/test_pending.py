# -*- coding: utf-8 -*-
"""欠账标记 · 回归测试。

mark_pending 必须在发请求之前落盘 —— 关机回调里只剩几秒，请求可能卡到进程被杀。
这里盯住「写、读、撤」以及各种坏输入。

跑法：
    python -m unittest discover -s test -v
"""
import json
import unittest

from ..helpers import Isolated, state


class TestPendingMark(Isolated):
    """欠账标记：写、读、撤，以及各种坏输入。"""

    def test_没有标记时返回None(self):
        self.assertIsNone(state.load_pending())

    def test_写进去能原样读回来(self):
        state.mark_pending("系统关机/重启")
        d = state.load_pending()
        self.assertEqual(d["reason"], "系统关机/重启")
        self.assertIn("at", d)

    def test_撤销后读不到(self):
        state.mark_pending("系统关机/重启")
        state.clear_pending()
        self.assertIsNone(state.load_pending())

    def test_撤销一个不存在的标记不报错(self):
        state.clear_pending()

    def test_坏json当成没有(self):
        with open(state.PEND_FILE, "w", encoding="utf-8") as f:
            f.write("{ 这不是 json")
        self.assertIsNone(state.load_pending())

    def test_只有时间没有原因算无效(self):
        with open(state.PEND_FILE, "w", encoding="utf-8") as f:
            json.dump({"at": "2026-10-02 17:03:30"}, f)
        self.assertIsNone(state.load_pending())


if __name__ == "__main__":
    unittest.main(verbosity=2)
