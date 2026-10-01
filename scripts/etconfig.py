#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""统一配置加载 —— 把「跟这台机器绑定」的东西从代码里赶出去。

代码里只留应用自身的常量（包名、按钮文案、接口路径……），
凡是会因人而异的（模拟器装在哪、adb 在哪、OAID 填什么）一律放
项目根目录的 ``config.json``，该文件已被 ``.gitignore`` 忽略。

首次使用：

    copy config.example.json config.json    # Windows
    cp    config.example.json config.json   # Git Bash

然后按实际情况填写。缺失必填项时会直接报错并指明缺哪一项，
不会静默用「某个默认值」跑出一个看起来正常、实际上错的结果。
"""

import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(ROOT, "config.json")
EXAMPLE_FILE = os.path.join(ROOT, "config.example.json")

_cache = None

# 缺省值：仅用于「不该逼用户配」的项；私有/环境相关的项一律不给默认值
DEFAULTS = {
    "mumu_vmindex": "0",
    "serial": "127.0.0.1:16384",
    "adb_ports": ["127.0.0.1:16384", "127.0.0.1:7555"],
    "mumu_boot_timeout": 240,
    "ad_stay": 15,
    "ad_stay_jitter": 4,
    "idle_minutes": 15,
}


def load(reload=False):
    """读取 config.json；不存在或损坏时返回 {}（让调用方走 require 报错）。"""
    global _cache
    if _cache is None or reload:
        data = {}
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
        except FileNotFoundError:
            data = {}
        except Exception:
            data = {}
        _cache = data
    return _cache


def get(key, default=None):
    """取配置项：优先 config.json，其次内置默认值。"""
    if key in load():
        return load()[key]
    if default is not None:
        return default
    return DEFAULTS.get(key)


def require(key, hint=""):
    """取必填配置项；没有就抛 SystemExit，把「缺什么、去哪补」说清楚。"""
    v = get(key)
    if v:
        return v
    raise SystemExit(
        "\n缺少必填配置项：%s%s\n"
        "  1) 把 %s\n     复制为 %s\n"
        "  2) 填好这一项后重试\n"
        % (key, ("（%s）" % hint) if hint else "",
           EXAMPLE_FILE, CONFIG_FILE)
    )
