# -*- coding: utf-8 -*-
"""测试共用：导入路径与隔离环境。

所有用例都在临时目录上跑，绝不碰 output/ 里的正式文件。
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from etalien import api                        # noqa: E402,F401  （供各用例直接取）
from etalien.timeguard import pauser, state    # noqa: E402


class Isolated(unittest.TestCase):
    """把落盘路径全挪到临时目录，绝不写 output/ 里的正式文件。

    供其他用例继承；本类自己不要定义 test_*，否则那些用例会被跑两遍。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="eta-test-")
        self._patches = [
            mock.patch.object(state, "STATE_FILE", os.path.join(self.tmp, "state.json")),
            mock.patch.object(state, "LOG_FILE", os.path.join(self.tmp, "guard.log")),
        ]
        for p in self._patches:
            p.start()
        self._state = dict(state._STATE)
        state._STATE.update({"last_result": None, "last_event": None})
        # 暂停闸门是有状态的：上一条用例成功后会把免重发窗口打开，
        # 不重置的话下一条用例的 pause() 会直接让位、根本不发请求。
        pauser.reset_pause_gate()

    def tearDown(self):
        state._STATE.clear()
        state._STATE.update(self._state)
        pauser.reset_pause_gate()
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def hold_gate(self):
        """模拟「刚刚发成功过」：把免重发窗口撑开，让 pause() 让位。"""
        pauser._release_pause(True)
