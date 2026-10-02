# -*- coding: utf-8 -*-
"""看广告领时长 · 常量、参数与日志。

分两类，别混：
    ① 应用自身的常量（包名、按钮文案、档位名称…）—— 写死在代码里，与机器无关
    ② 跟本机绑定的值（模拟器路径、adb 路径、OAID…）—— 一律放 config.json，
       见 etalien/config.py。config.json 已被 .gitignore 忽略，不会外泄。
"""
import os

from .. import OUT_DIR
from ..config import get
from ..logs import make

CONFIG = {
    "package": "com.etalien.booster",
    "main_activity": "com.etalien.booster/com.etalien.booster.ui.MainActivity",

    "fake_brand": "OnePlus",          # OnePlus → 广告SDK走OPPO路径 → 直接读 persist.oaid
    "brand_props": [
        "ro.product.manufacturer", "ro.product.brand",
        "ro.product.vendor.manufacturer", "ro.product.system.manufacturer",
        "ro.product.odm.manufacturer", "ro.product.vendor.brand",
        "ro.product.system.brand", "ro.product.odm.brand",
    ],

    # 「看广告 领时长」按钮文案（含广告加载失败后的重试态）
    "watch_button": ["看广告 领时长", "看广告领时长", "点击重试"],

    # 核心参数：广告页停留秒数。实测 15 秒即可发奖，故基准 15 秒 + 0~4 秒抖动。
    # 这是广告端的行为，跟本机无关，所以写死在代码里而不是放 config.json。
    "ad_stay": 15,
    "ad_stay_jitter": 4,

    # 等广告 Activity 出现的最长秒数
    "ad_appear_timeout": 40,

    # 模拟器开机 / adb 就绪的上限（调优参数，config.json 可覆盖）
    "mumu_boot_timeout": get("mumu_boot_timeout"),

    # 弹窗关键词
    "popup": ["我知道了", "跳过", "关闭", "以后再说", "暂不更新"],

    # 未登录标志
    "login_flags": ["注册登录", "请输入手机号", "登录后开启加速"],

    # 轮次控制
    # 额度分三档：阶段一 9 次×20分 → 阶段二 3 次×30分 → 加油包 9 次×10分，
    # 单日 21 轮刷满约 6 小时可暂停时长。这里给足上限，实际由「接口显示刷满」
    # 或「连续失败」提前终止。
    "max_rounds": 25,
    "round_gap": (6, 14),        # 轮次间随机间隔（模拟真人节奏）
    "retry_per_round": 2,        # 单轮内失败重试次数
    "max_consecutive_fail": 3,   # 连续失败多少轮后停止

    # 每日状态：date（最后跑哪天）+ all_done（那天是否全部看完）+ attempts + progress
    "state_file": os.path.join(OUT_DIR, "adwatch_state.json"),
    # 同一天最多尝试几次。额度刷不满（广告无填充等）时避免反复拉起模拟器；
    # 正常情况 1~2 次就收工，--force 可无视上限强制再跑。
    "max_attempts_per_day": 6,
    # 运行日志（计划任务用 pythonw.exe 无控制台，必须落盘）
    "log_file": os.path.join(OUT_DIR, "adwatch.log"),
}

log = make(lambda: CONFIG["log_file"])
