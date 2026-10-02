# -*- coding: utf-8 -*-
"""统一配置加载。

代码里只留与应用自身相关的常量（包名、按钮文案、接口路径……）；
凡是随机器变化的（模拟器路径、adb 路径、OAID、设备 ID 等）一律从项目根目录的
config.json 读取，该文件已被 .gitignore 忽略。

首次使用：

    copy config.example.json config.json    # Windows
    cp    config.example.json config.json   # Git Bash

必填项缺失时抛 SystemExit，并指明缺哪一项、模板在哪。
"""
import json
import os

from . import ROOT

CONFIG_FILE = os.path.join(ROOT, "config.json")
EXAMPLE_FILE = os.path.join(ROOT, "config.example.json")

_cache = None

# 缺省值只有一个用途：纯调优参数，不该逼用户配。
# 跟本机绑定的项（模拟器/adb 路径、实例编号、serial、OAID）一律不给默认值 ——
# 写死一个值会在换机后出现「看着能跑、其实连错设备」的隐蔽故障，比直接报错危险得多。
DEFAULTS = {
    "mumu_boot_timeout": 240,
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


# 下面几个本机项刻意做成函数而不是模块级常量 —— 这样 --state、--install 这类
# 不碰模拟器的子命令，在 config.json 还没填好时也能正常用。


def mumu_manager():
    """MuMuManager.exe 的完整路径。"""
    return require("mumu_manager", "MuMuManager.exe 的完整路径")


def adb_path():
    """adb.exe 的完整路径。

    刻意不内置「常见安装目录」清单，也不扫盘：猜中一个别的模拟器自带 adb，
    会连到错的设备上，而且出错点被推到后面某一步，很难查。
    """
    return require("adb_path", r"adb.exe 的完整路径，通常在 <MuMu 安装目录>\nx_main\adb.exe")


def serial():
    """模拟器 adb 地址。每台机器都可能不一样。"""
    return require("serial", "模拟器 adb 地址，如 127.0.0.1:16384")


def vmindex():
    """MuMu 实例编号（多开器里的序号）。"""
    return require("mumu_vmindex", "MuMu 实例编号，多开器里能看到，通常填 0")


def oaid():
    """注入模拟器的假 OAID，任意 UUID 即可。"""
    return require("oaid", "注入模拟器的假 OAID，任意 UUID 即可")
