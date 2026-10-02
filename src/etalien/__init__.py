# -*- coding: utf-8 -*-
"""外星仔加速器工具包的库。

路径基准统一在这里定义 —— 包内部一律用 etalien.ROOT / etalien.OUT_DIR / etalien.SRC_DIR
取，不再各自 dirname(__file__) 往上数。层级跟着目录走，以后挪目录只改这一处。
"""
import os

# 本文件在 src/etalien/__init__.py，往上三层才是项目根 ——
# config.json、output/、test/ 都在那儿。
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# src/ 本身。入口脚本（src/scripts、src/tools）不在包里，靠这个拼路径去找它们。
SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

OUT_DIR = os.path.join(ROOT, "output")
