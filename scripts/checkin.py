#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
外星仔加速器 · 每日「看广告领时长」自动签到
============================================
实现方式：**纯 UI 自动化 + 停留计时**，不调用任何接口。

核心原理（实测验证）
------------------------
外星仔的发奖判定不在广告页上，而在宿主 App 里：**只要广告 Activity 存活够
时间，回到主界面就自动发奖**（+20 分钟 / +1 次）。

因此整个流程不需要识别任何广告内容：

    点「看广告 领时长」 → 等广告 Activity 起来 → 停 15 秒 → 拉回主界面 → 校验

实测跨联盟通杀（三套完全不同的 UI，同一套逻辑全通过）：
    快手   KSAdSDK   WMPortraitActivity
    倍孜   BeiZi     BeiZiNewRewardVideoActivity
    穿山甲 bytedance PortraitTransparentAdActivity

为什么不用接口 / 不点领奖按钮
-----------------------------
1. 协议层已被官方封死：补发接口砍掉、token 换认证。
2. 发奖由服务端按广告播放记录校验，客户端伪造无效。实测"不等够时间直接穿透"
   会得到「广告加载失败」，进度不变 —— 时间没到就是不发。
3. 反过来，"停够时间但不点任何按钮"照样发奖。所以点按钮是纯粹的额外风险。

校验以后端数据为准（进度 N/9 或 可暂停时长），**不看 UI 提示** ——
实测存在"提示失败但时长确实增加"的情况。

环境前提
--------
1. 已装 MuMu 模拟器，并把 `MuMuManager.exe` / `adb.exe` 的路径、
   以及一个自定的 OAID 填进项目根目录的 `config.json`
   （模板见 `config.example.json`）
2. 模拟器里已装外星仔并登录
3. 【关键】已用 KernelSU(ksud) 把机型属性改成 OnePlus 并写入 persist.oaid
   —— 否则广告 SDK 报 "oaid sdk not find"，广告永远卡在"加载中"
   （`ensure_ready()` 每次运行都会自动重刷，见下）

用法
----
    python scripts/checkin.py                 # 跑满今天额度
    python scripts/checkin.py --rounds 3      # 只跑 3 轮
    python scripts/checkin.py --dry-run       # 只探测界面，不点击
    python scripts/checkin.py --state         # 打印每日状态（看到哪、刷完没）

每日状态（output/state.json）
-----------------------------
额度 0 点整点重置，所以跨天判断很简单。真正要防的是**同一天内只刷了一半**：
刷到第 12 轮时电脑被关机、脚本被打断、或广告临时无填充 —— 只按「今天跑过没有」
去重的话，这一天剩下的额度就永远补不回来了。所以状态记两个维度：

    date       最后一次跑是哪天        → 不是今天 → 必跑
    all_done   那天是否已全部看完      → 是今天但没看完 → 登录后继续补跑
    attempts   当天已尝试的次数        → 防止刷不满时反复启动模拟器
    progress   上次进度快照（几时几分）→ 排查用

因此自启触发多次是安全的：刷满了就跳过，没刷满就接着刷。
"""
import argparse
import json
import os
import random
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import etconfig as CFG
from probe import find_adb

# ----------------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------------
# 分两类，别混：
#   ① 应用自身的常量（包名、按钮文案、档位名称…）—— 写死在代码里，与机器无关
#   ② 跟本机绑定的值（模拟器路径、adb 路径、OAID…）—— 一律放 config.json，
#      见 scripts/etconfig.py。config.json 已被 .gitignore 忽略，不会外泄。
# ----------------------------------------------------------------------------
_ROOT = CFG.ROOT

CONFIG = {
    "package": "com.etalien.booster",
    "main_activity": "com.etalien.booster/com.etalien.booster.ui.MainActivity",

    # --- 环境相关（config.json）---
    "serial": CFG.get("serial"),
    "mumu_vmindex": CFG.get("mumu_vmindex"),
    "mumu_boot_timeout": CFG.get("mumu_boot_timeout"),
    "adb_ports": CFG.get("adb_ports"),

    "fake_brand": "OnePlus",          # OnePlus → 广告SDK走OPPO路径 → 直接读 persist.oaid
    "brand_props": [
        "ro.product.manufacturer", "ro.product.brand",
        "ro.product.vendor.manufacturer", "ro.product.system.manufacturer",
        "ro.product.odm.manufacturer", "ro.product.vendor.brand",
        "ro.product.system.brand", "ro.product.odm.brand",
    ],

    # 「看广告 领时长」按钮文案（含广告加载失败后的重试态）
    "watch_button": ["看广告 领时长", "看广告领时长", "点击重试"],

    # 【核心参数】广告页停留秒数。实测 15s 即可发奖，故基准 15s + 0~4s 抖动。
    "ad_stay": CFG.get("ad_stay"),
    "ad_stay_jitter": CFG.get("ad_stay_jitter"),

    # 等广告 Activity 出现的最长秒数
    "ad_appear_timeout": 40,

    # 今日额度用尽标志（三档全满后主按钮会变成这句）
    "done_flags": ["今日广告已看完", "请明日再来", "今日次数已用完"],

    # 弹窗关键词
    "popup": ["我知道了", "跳过", "关闭", "以后再说", "暂不更新"],

    # 未登录标志
    "login_flags": ["注册登录", "请输入手机号", "登录后开启加速"],

    # 轮次控制
    # 额度分三档：阶段一 9 次(20分) → 阶段二 3 次(30分) → 加油包(实测≥7次,10分)。
    # 实测单日 19 轮刷满，这里给足上限，实际由"额度用尽"或"连续失败"提前终止。
    "max_rounds": 25,
    "round_gap": (6, 14),        # 轮次间随机间隔（模拟真人节奏）
    "retry_per_round": 2,        # 单轮内失败重试次数
    "max_consecutive_fail": 3,   # 连续失败多少轮后停止

    # 每日状态：date(最后跑哪天) + all_done(那天是否全部看完) + attempts + progress
    "state_file": os.path.join(_ROOT, "output", "state.json"),
    # 同一天最多尝试几次。额度刷不满（广告无填充等）时避免反复拉起模拟器；
    # 正常情况 1~2 次就收工，--force 可无视上限强制再跑。
    "max_attempts_per_day": 6,
    # 运行日志（计划任务用 pythonw.exe 无控制台，必须落盘）
    "log_file": os.path.join(_ROOT, "output", "checkin.log"),
}


_LOG_FP = None


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)          # pythonw.exe 下无控制台，会抛错
    except Exception:
        pass
    global _LOG_FP
    try:
        if _LOG_FP is None:
            path = CONFIG["log_file"]
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if os.path.exists(path) and os.path.getsize(path) > 1_000_000:
                os.replace(path, path + ".1")          # 超 1MB 轮转
            _LOG_FP = open(path, "a", encoding="utf-8")
            _LOG_FP.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 =====\n")
        _LOG_FP.write(line + "\n")
        _LOG_FP.flush()
    except Exception:
        pass


# ----------------------------------------------------------------------------
# 环境自愈（幂等：模拟器没开就开、属性被还原就重刷）
# ----------------------------------------------------------------------------
def _mumu_manager():
    """MuMuManager.exe 的路径。

    刻意做成函数而不是模块级常量 —— 这样 `--state`、`--install` 这类
    不碰模拟器的子命令，在 config.json 还没填好时也能正常用。
    """
    return CFG.require("mumu_manager", "MuMuManager.exe 的完整路径")


def _mumu_info():
    """查询 MuMu 实例状态；失败返回 {}。"""
    try:
        r = subprocess.run([_mumu_manager(), "info", "-v", CONFIG["mumu_vmindex"]],
                           capture_output=True, timeout=30)
        return json.loads((r.stdout or b"").decode("utf-8", "ignore"))
    except Exception:
        return {}


def emulator_started():
    return bool(_mumu_info().get("is_android_started"))


def start_emulator():
    """用 MuMu 官方 MuMuManager 拉起实例，并等 Android 启动完成。"""
    mgr = _mumu_manager()
    if not os.path.exists(mgr):
        log(f"✗ 找不到 {mgr} —— 确认 config.json 里的 mumu_manager 填对了")
        sys.exit(1)

    log("模拟器未启动，正在拉起…")
    subprocess.run([mgr, "control", "-v", CONFIG["mumu_vmindex"], "launch"],
                   capture_output=True, timeout=180)
    t0 = time.time()
    while time.time() - t0 < CONFIG["mumu_boot_timeout"]:
        if emulator_started():
            log(f"模拟器已启动（{time.time() - t0:.0f}s）")
            return True
        time.sleep(5)
    log(f"✗ 模拟器启动超时（{CONFIG['mumu_boot_timeout']}s）")
    return False


def shutdown_emulator():
    """刷完后关闭模拟器（用户明确要求）。"""
    mgr = _mumu_manager()
    log("正在关闭模拟器…")
    try:
        subprocess.run([mgr, "control", "-v", CONFIG["mumu_vmindex"], "shutdown"],
                       capture_output=True, timeout=120)
        time.sleep(5)
    except Exception as e:
        log(f"  ! shutdown 异常: {e}")
    if emulator_started():
        log("  ! 实例仍在运行")
    else:
        log("  ✓ 模拟器已关闭")


def mumu_sh(cmd, timeout=40):
    """通过 MuMu 自带的 shell 通道执行命令。

    ⚠ 用这个**而不是** `adb root`：`adb root` 会重启 adbd，重启期间命令仍以
    shell(uid=2000) 身份执行，访问 /data/adb/ksud 会 Permission denied，
    冷启动时极难卡准时机。而 MuMuManager 的 sh 通道身份直接是 uid=0(root)。
    """
    try:
        r = subprocess.run([_mumu_manager(), "sh", "-v", CONFIG["mumu_vmindex"], "-c", cmd],
                           capture_output=True, timeout=timeout)
        return (r.stdout or b"").decode("utf-8", "ignore").strip()
    except Exception:
        return ""


def ensure_ready(adb):
    """确保：模拟器在跑 → adb 可连 → OAID/品牌属性已注入。"""
    # 1) 模拟器实例
    if not emulator_started() and not start_emulator():
        sys.exit(1)

    # 2) 等 adb 可连
    for _ in range(40):
        for port in CONFIG["adb_ports"]:
            subprocess.run([adb, "connect", port], capture_output=True, timeout=20)
        out = subprocess.run([adb, "devices"], capture_output=True, timeout=20).stdout.decode("utf-8", "ignore")
        if CONFIG["serial"] in out:
            break
        time.sleep(5)
    else:
        log("✗ 等不到模拟器 adb 就绪（120s）")
        sys.exit(1)

    # 3) 注入 OAID 与品牌属性（MuMu 每次重启都会还原 ro.*），带重试验证。
    #    冷启动时系统可能还没就绪，mumu_sh 会返回空 → 靠重试兜住。
    oaid_value = CFG.require("oaid", "注入模拟器的假 OAID，任意 UUID 即可")
    brand = oaid = ""
    for i in range(8):
        mumu_sh(f"setprop persist.oaid {oaid_value}")
        for k in CONFIG["brand_props"]:
            mumu_sh(f"/data/adb/ksud resetprop {k} {CONFIG['fake_brand']}")
        brand = mumu_sh("getprop ro.product.manufacturer")
        oaid = mumu_sh("getprop persist.oaid")
        if CONFIG["fake_brand"].lower() in brand.lower() and oaid_value in oaid:
            log(f"OAID 环境已就绪（brand={brand}, oaid={oaid[:8]}…）")
            return
        log(f"  属性未生效（brand={brand!r}），重试 {i + 1}/8…")
        time.sleep(4)
    log(f"⚠ OAID 环境异常（brand={brand!r}, oaid={oaid[:12]!r}）—— 广告可能无填充")


def connect():
    adb = find_adb()
    if not adb:
        log("找不到 adb.exe —— 确认模拟器已装，或直接把路径填进 config.json 的 adb_path")
        sys.exit(1)
    os.environ["ADBUTILS_ADB_PATH"] = adb
    os.environ["PATH"] = os.path.dirname(adb) + os.pathsep + os.environ.get("PATH", "")
    log(f"adb: {adb}")

    ensure_ready(adb)

    import uiautomator2 as u2
    try:
        d = u2.connect(CONFIG["serial"])
    except Exception:
        d = u2.connect()
    info = d.info
    w, h = info.get("displayWidth") or 0, info.get("displayHeight") or 0
    log(f"已连接 {d.serial}  {w}x{h}")
    if w > h:
        log("⚠ 当前是横屏。App 为竖屏设计，建议固定为竖屏：")
        log(f'  MuMuManager.exe setting -v {CONFIG["mumu_vmindex"]} -k resolution_mode -val phone.1')
    return d


# ----------------------------------------------------------------------------
# 状态读取（以后端数据为准）
# ----------------------------------------------------------------------------
STAGES = ["阶段一", "阶段二", "加油包"]


def read_state(d):
    """返回 (可暂停总秒数, 各档位状态 dict)，读不到的为 None。

    界面把额度分成三档，满额后文案会从 "N / M" 变成「已完成」：
        阶段一  9 次 × 20 分钟
        阶段二  3 次 × 30 分钟
        加油包  待解锁
    所以进度用通用解析，且**核心判定一律以「时长是否增加」为准**，
    不依赖任何具体文案格式。
    """
    xml = d.dump_hierarchy()
    vals = [v.strip() for v in re.findall(r'text="([^"]*)"', xml) if v.strip()]

    seconds = None
    for i, v in enumerate(vals):
        if v == "时" and i >= 1 and i + 4 < len(vals) \
                and vals[i + 2] == "分" and vals[i + 4] == "秒":
            try:
                seconds = int(vals[i - 1]) * 3600 + int(vals[i + 1]) * 60 + int(vals[i + 3])
            except ValueError:
                pass
            break

    stages = {}
    for name in STAGES:
        if name not in vals:
            continue
        i = vals.index(name)
        for j in range(i + 1, min(i + 4, len(vals))):
            if vals[j] in ("已完成", "待解锁") or re.fullmatch(r"\d+\s*/\s*\d+", vals[j]):
                stages[name] = vals[j]
                break
    return seconds, stages


def fmt_state(state):
    seconds, stages = state
    if seconds is None:
        bal = "?"
    else:
        bal = f"{seconds // 3600}时{seconds % 3600 // 60}分{seconds % 60}秒"
    detail = "  ".join(f"{k} {v}" for k, v in stages.items() if k in ("阶段一", "阶段二"))
    return f"时长 {bal}" + (f"  [{detail}]" if detail else "")


# ----------------------------------------------------------------------------
# 界面工具
# ----------------------------------------------------------------------------
def find_any(d, texts, timeout=0):
    """按顺序找第一个存在的控件；timeout>0 时轮询等待。"""
    deadline = time.time() + timeout
    while True:
        for t in texts:
            try:
                el = d(textContains=t)
                if el.exists:
                    return el, t
            except Exception:
                pass
        if time.time() > deadline:
            return None, None
        time.sleep(1)


def is_main(d):
    try:
        return d.app_current().get("activity", "").endswith(".ui.MainActivity")
    except Exception:
        return False


def punch_home(d, tries=3):
    """【万能穿透】不管上层压着什么广告页/落地页，直接把宿主主界面拉到前台。

    比"找关闭按钮"稳得多：不依赖任何广告联盟的 UI，且 MainActivity 是
    singleTask，宿主状态完整保留，不会重走启动流程。
    """
    for _ in range(tries):
        try:
            d.shell(f"am start -n {CONFIG['main_activity']}")
        except Exception:
            pass
        time.sleep(3)
        if is_main(d):
            return True
    return is_main(d)


def dismiss_popups(d):
    """点掉挡路的弹窗。优先处理 app 的「确定要退出吗」。"""
    try:
        if d(textContains="退出").exists and d(text="取消").exists:
            d(text="取消").click()
            log("  已取消「确定要退出吗」弹窗")
            time.sleep(1.5)
            return True
    except Exception:
        pass

    el, t = find_any(d, CONFIG["popup"], timeout=0)
    if el:
        try:
            el.click()
            log(f"  点掉弹窗「{t}」")
            time.sleep(1)
            return True
        except Exception:
            pass
    return False


def ensure_on_ads_page(d, timeout=5):
    """确保停在「看广告 领时长」页面。

    竖屏手机分辨率下页面较长，主按钮会被挤到滚动区之外 —— 找不到就上滑几次。
    """
    el, _ = find_any(d, CONFIG["watch_button"], timeout=timeout)
    if el:
        return True

    # 切到主页 tab（按钮在主页，不在充值中心）
    for t in ["个人中心", "主页"]:
        try:
            tab = d(textContains=t)
            if tab.exists:
                tab.click()
                time.sleep(2.5)
                el, _ = find_any(d, CONFIG["watch_button"], timeout=3)
                if el:
                    return True
        except Exception:
            pass

    # 上滑找按钮（用相对坐标，不依赖分辨率）
    for _ in range(4):
        try:
            w, h = d.info["displayWidth"], d.info["displayHeight"]
            d.swipe(w // 2, int(h * 0.78), w // 2, int(h * 0.30), 0.25)
        except Exception:
            pass
        time.sleep(1.2)
        el, _ = find_any(d, CONFIG["watch_button"], timeout=2)
        if el:
            return True
    return False


def is_logged_in(d):
    for t in CONFIG["login_flags"]:
        try:
            if d(textContains=t).exists:
                return False
        except Exception:
            pass
    return True


def is_exhausted(d):
    """今日额度是否已全部刷完（三档满额后主按钮变成「今日广告已看完」）。"""
    for t in CONFIG["done_flags"]:
        try:
            if d(textContains=t).exists:
                return True
        except Exception:
            pass
    return False


def _stage_done(v):
    """单档是否已完成：文案「已完成」，或进度形如 9/9。"""
    if not v:
        return False
    if v == "已完成":
        return True
    m = re.fullmatch(r"(\d+)\s*/\s*(\d+)", v)
    return bool(m and m.group(1) == m.group(2))


def stages_all_done(stages):
    """按三档进度数据判断是否刷满 —— 不依赖那句「今日广告已看完」的文案。

    够不着的一档（界面没渲染到）不下结论；「待解锁」的加油包不算未完成。
    """
    if not stages:
        return False
    saw = False
    for name in STAGES:
        v = stages.get(name)
        if v is None or v == "待解锁":
            continue
        saw = True
        if not _stage_done(v):
            return False
    return saw


def quota_done(d, stages=None):
    """额度是否刷满：UI 提示 或 三档进度数据，任一成立即算。"""
    if is_exhausted(d):
        return True
    if stages is None:
        try:
            _, stages = read_state(d)
        except Exception:
            return False
    return stages_all_done(stages)


# ----------------------------------------------------------------------------
# 每日状态（记住 last run day + 是否「全部看完」）
# ----------------------------------------------------------------------------
# 见文件头「每日状态」一节。为什么必须记 all_done：额度虽然是 0 点重置的，
# 但同一天内也可能只刷了一半（电脑关机 / 脚本被打断 / 广告无填充）。只按
# 「今天跑过没有」去重，这半天的额度就白丢了 —— 而只按「没刷满就一直跑」，
# 又会在广告无填充的日子里反复拉起模拟器。所以用 attempts 封顶。

def load_state():
    """读状态文件，返回 dict；不存在/为空/损坏 → {}。"""
    try:
        with open(CONFIG["state_file"], encoding="utf-8") as f:
            raw = f.read().strip()
    except Exception:
        return {}
    if not raw:
        return {}
    if not raw.startswith("{"):                 # 旧格式：只有一个日期字符串
        return {"date": raw, "all_done": True, "attempts": 1}
    try:
        st = json.loads(raw)
        return st if isinstance(st, dict) else {}
    except Exception:
        return {}


def save_state(**kw):
    """合并写入状态文件（未指定的字段保持原值）。

    注意：一旦传入的 date 和文件里的日期不同，说明跨天了 —— 旧记录的所有字段
    （all_done / attempts / progress）立即作废，绝不继承。否则「昨天已看完」
    会顺着 merge 漏进今天，把当天的额度直接吞掉。
    """
    st = load_state()
    if kw.get("date") and st.get("date") and kw["date"] != st["date"]:
        st = {}
    st.update(kw)
    st["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        os.makedirs(os.path.dirname(CONFIG["state_file"]), exist_ok=True)
        with open(CONFIG["state_file"], "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"  ! 写入状态文件失败: {e}")
    return st


def _today():
    return time.strftime("%Y-%m-%d")


def state_today():
    """取「今天」的状态；文件里是别的日期（昨天）→ 当作全新的一天，不继承。"""
    st = load_state()
    if st.get("date") != _today():
        return {"date": _today(), "all_done": False, "attempts": 0, "progress": None}
    st.setdefault("all_done", False)
    st.setdefault("attempts", 0)
    return st


def bump_attempt():
    """当天尝试次数 +1，**立即落盘** —— 中途断电也照样算数，不会被无限重试。"""
    return save_state(date=_today(), attempts=int(state_today().get("attempts", 0)) + 1)


# ----------------------------------------------------------------------------
# 单轮流程
# ----------------------------------------------------------------------------
def run_one_round(d):
    """返回 (状态, 快照)。状态：'ok' 成功 / 'done' 今日额度用尽 / 'fail' 失败。"""
    dismiss_popups(d)
    if not ensure_on_ads_page(d):
        if quota_done(d):
            return "done", None
        log("  ✗ 不在「看广告」页面")
        return "fail", None

    before = read_state(d)

    # 1) 点「看广告 领时长」
    el, label = find_any(d, CONFIG["watch_button"], timeout=5)
    if not el:
        log("  ✗ 找不到「看广告」按钮")
        return "done" if quota_done(d, before[1]) else "fail", before
    try:
        el.click()
    except Exception as e:
        log(f"  ✗ 点击失败: {e}")
        return "fail", before
    log(f"  已点击「{label}」")

    # 2) 等广告 Activity 起来
    t0 = time.time()
    while time.time() - t0 < CONFIG["ad_appear_timeout"]:
        if not is_main(d):
            break
        time.sleep(1)
    else:
        if quota_done(d, before[1]):
            return "done", before
        log("  ✗ 广告未出现（无填充 or 额度已用尽）")
        return "fail", before

    try:
        act = d.app_current().get("activity", "")
    except Exception:
        act = "?"
    log(f"  广告已出现 ({time.time()-t0:.0f}s): {act.split('.')[-1]}")

    # 3) 【核心】原地停留，不点任何按钮 —— 发奖只看时间够不够
    stay = CONFIG["ad_stay"] + random.uniform(0, CONFIG["ad_stay_jitter"])
    log(f"  停留 {stay:.0f}s（不点任何按钮）…")
    time.sleep(stay)

    # 4) 万能穿透回主界面
    if not punch_home(d):
        log("  ! 穿透失败，改按返回键")
        for _ in range(4):
            try:
                d.press("back")
            except Exception:
                pass
            time.sleep(1.5)
            dismiss_popups(d)
            if is_main(d):
                break
    time.sleep(2)
    dismiss_popups(d)

    # 5) 校验（以后端数据为准，不看 UI 提示）
    #    实测存在"提示领取失败、但时长确实增加"的情况，所以一律以时长增量为准。
    after = read_state(d)
    bsec, _ = before
    asec, _ = after
    if bsec is not None and asec is not None and asec > bsec:
        log(f"  ✓ 成功  时长 +{(asec - bsec) // 60:.0f} 分钟   （{fmt_state(after)}）")
        return "ok", after
    if quota_done(d, after[1]):
        return "done", after
    log(f"  ✗ 未生效  {fmt_state(before)} → {fmt_state(after)}")
    return "fail", after


# ----------------------------------------------------------------------------
# 辅助
# ----------------------------------------------------------------------------
def dump_ui(d):
    xml = d.dump_hierarchy()
    log("--- 当前界面文本节点 ---")
    for m in re.finditer(r"<node[^>]*>", xml):
        n = m.group(0)

        def g(k):
            r = re.search(k + r'="([^"]*)"', n)
            return r.group(1) if r else ""

        t = (g("text") or g("contentDescription")).strip()
        rid = g("resource-id").split("/")[-1]
        if t or rid:
            log(f"  {g('bounds')}  {rid[:45]:<45}  {t[:40]}")
    log(f"前台: {d.app_current()}")


RUN_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "EtalienDailyCheckin"
LEGACY_STARTUP_CMD = "etalien_daily_checkin.cmd"


def _startup_dir():
    return os.path.join(os.environ.get("APPDATA", ""),
                        "Microsoft", "Windows", "Start Menu", "Programs", "Startup")


def cmd_install():
    """写进 HKCU 的 Run 键 → 每次登录自动跑一趟。

    触发多次是安全的：状态文件里记了 date + all_done，刷满了秒退，
    没刷满（中途关机 / 上次失败）就接着补。
    """
    import winreg

    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pyw):
        pyw = sys.executable
    cmdline = '"%s" "%s" --delay 120' % (pyw, os.path.abspath(__file__))

    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE)
    winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, cmdline)
    winreg.CloseKey(key)

    stale = os.path.join(_startup_dir(), LEGACY_STARTUP_CMD)
    if os.path.exists(stale):
        os.remove(stale)
        print("已清理旧启动项:", stale)

    print("已安装开机自启（注册表 Run 项）：")
    print("  HKCU\\%s\\%s" % (RUN_KEY, RUN_NAME))
    print("  启动命令:", cmdline)


def cmd_uninstall():
    import winreg

    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE)
        winreg.DeleteValue(key, RUN_NAME)
        winreg.CloseKey(key)
        print("已移除注册表自启项:", RUN_NAME)
    except FileNotFoundError:
        print("注册表里没有该项，无需移除")
    except Exception as e:
        print("移除失败:", e)

    stale = os.path.join(_startup_dir(), LEGACY_STARTUP_CMD)
    if os.path.exists(stale):
        os.remove(stale)
        print("已清理启动文件夹:", stale)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只探测界面，不点击")
    ap.add_argument("--rounds", type=int, default=CONFIG["max_rounds"])
    ap.add_argument("--force", action="store_true", help="无视「今天已看完」和尝试上限，强制跑")
    ap.add_argument("--keep-emulator", action="store_true", help="跑完不关模拟器")
    ap.add_argument("--delay", type=int, default=0,
                    help="启动前先等这么多秒（自启时让系统先稳定下来）")
    ap.add_argument("--state", action="store_true", help="打印每日状态后退出（不连模拟器）")
    ap.add_argument("--install", action="store_true", help="安装开机自启（注册表 Run 项）")
    ap.add_argument("--uninstall", action="store_true", help="移除开机自启")
    args = ap.parse_args()

    if args.install:
        return cmd_install()
    if args.uninstall:
        return cmd_uninstall()
    if args.state:
        st = state_today()
        print(json.dumps(st, ensure_ascii=False, indent=2))
        print(f"#{st['date']} 已尝试 {st['attempts']} 次，"
              f"全部看完：{'是' if st['all_done'] else '否'}")
        return

    today = _today()

    # 去重（两个维度，缺一不可）：
    #   今天 + 已全部看完   → 跳过
    #   今天 + 没看完       → 继续补跑（电脑中途关机 / 上次失败）
    #   不是今天            → 必跑（额度 0 点重置）
    if not args.dry_run and not args.force:
        st = state_today()
        if st["all_done"]:
            log(f"今天（{today}）额度已全部看完，跳过。要强制重跑请加 --force。")
            return
        if st["attempts"] >= CONFIG["max_attempts_per_day"]:
            log(f"今天（{today}）已尝试 {st['attempts']} 次仍未看完"
                f"（上次：{st.get('progress') or '进度未知'}），今日不再重试（--force 可强制）。")
            return
        if st["attempts"]:
            log(f"今天（{today}）已跑过 {st['attempts']} 次但**未全部看完**"
                f"（上次：{st.get('progress') or '进度未知'}），接着补完剩余额度…")

    if args.delay > 0 and not args.dry_run:
        log(f"等待 {args.delay} 秒后开始（让系统先稳定下来）…")
        time.sleep(args.delay)

    d = connect()

    if args.dry_run:
        dump_ui(d)
        return

    log("启动 app…")
    d.app_start(CONFIG["package"])
    time.sleep(5)
    dismiss_popups(d)

    all_done = False       # 今天额度是否已全部看完 → 决定以后还要不要补跑
    progress = None        # 进度快照，便于知道上次停在哪
    started = False        # 是否「有效尝试」：决定要不要把今天记进状态
    shutdown = False       # 是否关模拟器（登录/排查场景留着不关）
    try:
        if not is_logged_in(d):
            log("✗ 未登录，请先手动完成登录。本轮不关模拟器、不写状态。")
            return

        if not ensure_on_ads_page(d):
            if quota_done(d):
                log("启动时额度就已经是「全部看完」状态")
                shutdown = started = True
                # 只有非凌晨时段才敢认。凌晨 0-6 点看到「已完成」，可能是前一天
                # 的额度还没重置（界面没刷新），若就此收工，整个白天都不会再试，
                # 白丢一天额度。记成「没看完」→ 白天登录时自然补跑。
                if time.localtime().tm_hour >= 6:
                    all_done = True
                else:
                    log("（凌晨时段，可能是昨日额度未刷新 —— 本次记为「未看完」，白天会再补跑一趟）")
                return
            log("✗ 未找到「看广告 领时长」入口，dump 当前界面。本轮不关模拟器，不写状态。")
            dump_ui(d)
            return

        started = True
        bump_attempt()     # 立即落盘：中途被关机也算一次尝试，不会被无限重试
        log(f"起始：{fmt_state(read_state(d))}")

        ok = fail = 0
        for i in range(1, args.rounds + 1):
            log(f"--- 第 {i} 轮 ---")
            status = "fail"
            for attempt in range(1 + CONFIG["retry_per_round"]):
                if attempt:
                    log(f"  重试 #{attempt}")
                    punch_home(d)
                    time.sleep(3)
                status, snap = run_one_round(d)
                if status != "fail":
                    break
            if status == "ok":
                ok += 1
                fail = 0
            elif status == "done":
                log("今日广告已全部看完，收工")
                all_done = True
                break
            else:
                fail += 1
                if fail >= CONFIG["max_consecutive_fail"]:
                    log(f"连续 {fail} 轮失败，停止（已完成的额度不会丢，下次登录接着补）")
                    break

            # 每轮都刷一次进度快照：万一接下来被关机，也能看出停在哪
            progress = fmt_state(snap) if snap else fmt_state(read_state(d))
            save_state(date=today, progress=progress)

            if i < args.rounds:
                gap = random.uniform(*CONFIG["round_gap"])
                log(f"  间隔 {gap:.0f}s（模拟真人节奏）")
                time.sleep(gap)

        final = read_state(d)
        progress = fmt_state(final)
        # 轮次跑满/提前停都再确认一遍额度：UI 提示 或 三档进度数据
        if not all_done:
            all_done = quota_done(d, final[1])
        log(f"===== 结束：成功 {ok} 轮，失败 {fail} 轮，{progress}，"
            f"{'已全部看完' if all_done else '未看完'} =====")
        shutdown = True
    finally:
        if started:
            save_state(date=today, all_done=all_done, progress=progress)
        if shutdown and not args.keep_emulator:
            shutdown_emulator()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("已被手动中断")
