# -*- coding: utf-8 -*-
"""模拟器端（安卓 App）读取。

token 从 App 自己的 SharedPreferences 现读 —— App 会定期调 refresh/token 续期，
所以每次读到的就是最新那份。进度读 /v2/account/pc/ad/config 的 watchCnt。

续期不自己算：那个接口要客户端签名（sig），算不出来。只在 401 时把 App 摇醒，
让它按自己的规则刷新，再把新凭据读回来。
"""
import re
import time

from ..procs import run
from .creds import ANDROID_OS, load_dvc, save_cred, x_eta
from .proto import decode
from .transport import call

ANDROID_PKG = "com.etalien.booster"
ANDROID_SP = "/data/data/%s/shared_prefs/spUtils.xml" % ANDROID_PKG

_TOKEN_RE = re.compile(r'name="CUR_USER_TOKEN">([^<]+)<')
_REFRESH_RE = re.compile(r'name="LAST_REFRESH_TOKEN_TIME" value="(\d+)"')
_VER_RE = re.compile(r"versionName=([\d.]+)")


class TokenExpired(Exception):
    """服务端回 401 —— token 失效，调用方需要重新取一份再试。"""


def read_sp(adb, serial, timeout=20):
    """读 App 的 SharedPreferences 文本；非 root 时自动提权重试一次。

    这份文件里还有 LAST_LOGIN_PHONE（手机号），调用方不要把它落盘或打印。
    """
    def _cat():
        try:
            r = run([adb, "-s", serial, "shell", "cat", ANDROID_SP],
                    capture_output=True, timeout=timeout)
            return (r.stdout or b"").decode("utf-8", "ignore")
        except Exception:
            return ""

    xml = _cat()
    if "CUR_USER_TOKEN" not in xml:                      # Permission denied
        try:
            run([adb, "-s", serial, "root"], capture_output=True, timeout=30)
            time.sleep(2)
            run([adb, "-s", serial, "wait-for-device"],
                capture_output=True, timeout=30)
        except Exception:
            pass
        xml = _cat()
    return xml


def read_token(adb, serial):
    """App 当前 token 与它的续期时间戳（LAST_REFRESH_TOKEN_TIME）。

    每次都现读：App 会按需续期，读到的就是最新那份。实测 token 新鲜时冷启动
    并不会触发续期，所以「刷新」得靠 401 时把它摇醒（见 wake_app / read_progress）。
    """
    xml = read_sp(adb, serial)
    m = _TOKEN_RE.search(xml)
    if not m:
        return None, None
    ts = _REFRESH_RE.search(xml)
    return m.group(1).strip(), (int(ts.group(1)) if ts else None)


def read_ver(adb, serial, timeout=20):
    """App 实际版本号，用来拼 x-eta 的 ver。

    不能写死：ver 与 token 来源端不匹配一律回 401，App 一升级写死的值就废了。
    读不到返回 None —— 调用方应当直接失败，不要拿旧值顶上。
    """
    try:
        r = run([adb, "-s", serial, "shell", "dumpsys", "package", ANDROID_PKG],
                capture_output=True, timeout=timeout)
    except Exception:
        return None
    m = _VER_RE.search((r.stdout or b"").decode("utf-8", "ignore"))
    return m.group(1) if m else None


def sim_call(path, token, ver, timeout=20):
    """用安卓端 x-eta 调接口，返回原始响应。

    401 抛 TokenExpired，其余非 200 抛 RuntimeError（把服务端原因带出来）。
    dvc 一律取本机那个（scan 抓出来落在 output/device.txt）—— 它跟 token 的
    来源端无关，但服务端会校验它是否属于当前账号：随便填会回
    400 invalid device id，拿 App 自己的 android_id 去试同样不行。
    """
    dvc = load_dvc()
    st, raw = call(path, b"", token, dvc, timeout=timeout,
                   xeta=x_eta(dvc, ANDROID_OS, ver))
    if st == 401:
        raise TokenExpired("HTTP 401 %r" % raw[:60])
    if st != 200:
        raise RuntimeError("HTTP %s %r" % (st, raw[:80]))
    return raw


def parse_ad_config(raw):
    """解析 /v2/account/pc/ad/config 的响应。

    外层 field1 = repeated PcAdConfigLevelItem，每项：
        f2 list[]   广告条目，条目个数 = 该档总次数
        f3 watchCnt 今日已完成次数
        f6 title    阶段一 / 阶段二 / 加油包

    App 自己算 N/M 用的就是这两个字段（APK 里 PcAdConfigLevelItem 只剩
    getWatchCnt / getListList 这两个 getter 被 R8 保留），所以这是权威数据。
    """
    stages = []
    for f, kind, v in decode(raw):
        if f != 1 or kind != "bytes":
            continue
        it = {"title": "", "done": 0, "total": 0}
        for ff, kk, vv in decode(v):
            if ff == 3 and kk == "varint":
                it["done"] = vv
            elif ff == 6 and kk == "bytes":
                it["title"] = vv.decode("utf-8", "replace")
            elif ff == 2 and kk == "bytes":
                it["total"] += 1
        if it["total"]:
            stages.append(it)
    return stages


def ad_config(token, ver, timeout=20):
    """今日各档广告进度。读不到就抛（不返回空 —— 空会被误当成「没满」，也可能被当成「满了」）。"""
    return parse_ad_config(sim_call("/v2/account/pc/ad/config", token, ver, timeout=timeout))


def remain_duration(token, ver, timeout=20):
    """可暂停剩余秒数（field1）。"""
    raw = sim_call("/v2/account/remain/duration", token, ver, timeout=timeout)
    return next((v for f, k, v in decode(raw) if k == "varint" and f == 1), None)


def wake_app(adb, serial, wait=15, timeout=60):
    """把 App 冷启动一次，逼它自己去续期 token。

    续期接口要客户端签名，我们算不出来，所以不自己续：只负责把 App 摇醒，
    让它按自己的规则刷新，再把新凭据读回来。
    （实测 App 只在 token 需要刷新时才动手，token 新鲜时冷启动不会改它。）
    """
    main = "%s/%s.ui.MainActivity" % (ANDROID_PKG, ANDROID_PKG)
    try:
        run([adb, "-s", serial, "shell", "am", "force-stop", ANDROID_PKG],
            capture_output=True, timeout=timeout)
        time.sleep(1.5)
        run([adb, "-s", serial, "shell", "am", "start", "-n", main],
            capture_output=True, timeout=timeout)
    except Exception:
        return False
    time.sleep(wait)
    return True


def _read_once(token, ver, timeout):
    """读一组进度。token 失效抛 TokenExpired，接口异常抛 RuntimeError。"""
    return (remain_duration(token, ver, timeout=timeout),
            ad_config(token, ver, timeout=timeout))


def read_progress(adb, serial, timeout=20, auto_renew=True):
    """模拟器端一次读全：App token + 可暂停时长 + 三档今日进度。

    返回 dict：ok / reason / token / token_ts / seconds / stages / renewed。
    判定一律吃这份数据 —— UI 会滚动、会漏渲染，读不到不等于完成。

    token 失效（401）时若 auto_renew，会冷启动 App 让它自己续期再读一次。
    """
    out = {"ok": False, "reason": "", "token": None, "token_ts": None,
           "seconds": None, "stages": None, "renewed": False}
    token, ts = read_token(adb, serial)
    if not token:
        out["reason"] = "读不到 App token（模拟器没起来 / 未登录）"
        return out
    out["token"], out["token_ts"] = token, ts
    ver = read_ver(adb, serial)
    if not ver:
        out["reason"] = "读不到 App 版本号（x-eta 要用，不能拿旧值顶）"
        return out

    try:
        seconds, stages = _read_once(token, ver, timeout)
    except TokenExpired:
        if not auto_renew:
            out["reason"] = "token 已失效"
            return out
        # 自己不续期（算不出签名），只把 App 摇醒让它续，再读一次
        out["renewed"] = True
        wake_app(adb, serial)
        token2, ts2 = read_token(adb, serial)
        if not token2 or token2 == token:
            out["reason"] = "token 已失效，App 也没换出新 token（可能要重新登录）"
            return out
        out["token"], out["token_ts"] = token2, ts2
        try:
            seconds, stages = _read_once(token2, ver, timeout)
        except Exception as e:
            out["reason"] = "续期后仍不可用：%s" % e
            return out
    except Exception as e:
        out["reason"] = "%s: %s" % (type(e).__name__, e)
        return out

    out["seconds"], out["stages"] = seconds, stages
    if not stages:
        out["reason"] = "接口没给出档位进度"
        return out
    # 顺手把 App 端凭据存一份给 PC 端脚本用：App 会自己续期，这一份比 PC 客户端
    # 内存里那份活得久（PC 客户端 token 过期后并不会自己换新，实测重启也不行）。
    # 每次都要复写（不看 token 变没变）：saved / ts 是「谁最新」的依据，
    # 跳过写入会让这份的时间戳停在旧值，选凭据时就轮不到它。
    save_cred(out["token"], ANDROID_OS, ver, "android-app")
    out["ok"] = True
    return out
