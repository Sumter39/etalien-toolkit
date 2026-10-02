# -*- coding: utf-8 -*-
"""凭据：token 与它的来源端。

x-eta 里的 os/ver 必须与 token 的来源端一致，配错一律回 401 token expired：
PC 端 os=2、App 端 os=1；ver 取客户端实际版本（App 端现读 dumpsys，PC 端从内存抓），
一律不写死 —— 客户端一升级，写死的值就废了。

设备 ID（dvc）单独放 output/device.txt：它与机器绑定、与 token 来源端无关，
换机要重填，所以不跟 token 挤在一个文件里。
"""
import json
import os
import time

from .. import OUT_DIR

PC_OS, PC_VER = 2, "1.24.11"
ANDROID_OS = 1

DVC_FILE = os.path.join(OUT_DIR, "device.txt")
CRED_FILE = os.path.join(OUT_DIR, "cred.json")


def x_eta(dvc, os_type=PC_OS, ver=PC_VER):
    """构造 x-eta 描述头。os/ver 必须与 token 的来源端一致。"""
    return "os=%s&ver=%s&dvc=%s&ch=default" % (os_type, ver, dvc)


def xeta_of(cred, dvc):
    """按凭据的来源端拼 x-eta。"""
    return x_eta(dvc, cred.get("os", PC_OS), cred.get("ver", PC_VER))


def load_token(src=None):
    """取一份 token。凭据连来源端（os/ver）一起存在 output/cred.json。"""
    c = load_cred(src)
    if c:
        return c["token"]
    raise SystemExit(
        "没有可用凭据 —— 跑 python src/tools/etapi.py scan 抓 PC 端的，"
        "或先跑一次 adwatch（它会顺手存下模拟器端 App 的 token）"
    )


def load_dvc():
    """设备 ID（x-eta 头里的 dvc）。

    由 scan 从客户端内存抓出来后落在 output/device.txt。
    刻意不给默认值 —— 它跟具体设备绑定，写死一个值等于把别人的设备 ID 带进
    你的仓库，且换机后会出现「看着能跑、其实用错 ID」的隐蔽故障。

    服务端会校验它是否属于当前账号：随便填会回 400 invalid device id，
    所以模拟器端也复用这一份，不要另外造。
    """
    if os.path.exists(DVC_FILE):
        with open(DVC_FILE, encoding="utf-8") as f:
            d = f.read().strip()
        if d:
            return d
    raise SystemExit(
        "没有设备 ID —— 先跑 python src/tools/etapi.py scan"
        "（需要外星仔 PC 客户端在运行且已登录）"
    )


def load_creds():
    """全部凭据 {src: {token, os, ver, ts, saved}}；文件不存在/损坏返回 {}。"""
    try:
        with open(CRED_FILE, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return {}
    if not isinstance(d, dict):
        return {}
    return {k: v for k, v in d.items() if isinstance(v, dict) and v.get("token")}


def cred_rank(cred):
    """排序键：保存时间新的在前。

    谁最后被刷新过谁就最新 —— 两端都会写 saved / ts：scan 刷 pc-client，
    adwatch 每轮刷 android-app。时间并列时（同一秒内写的）按 src 定序，保证稳定。
    """
    return (-cred.get("ts", 0), cred.get("src") or "")


_preferred_src = None


def prefer_cred(src):
    """指定后续 call() 用哪一份凭据。

    output/cred.json 里同时存着两端（pc-client / android-app）的凭据时，
    默认取保存时间最新的那份；调用方探活后可以用这个把选定的那份钉住。
    """
    global _preferred_src
    _preferred_src = src


def load_cred(src=None):
    """取一份凭据：指定 src 就取那一份，否则取「钉住的那份」或最新的。"""
    creds = load_creds()
    src = src or _preferred_src
    if src:
        return creds.get(src)
    if not creds:
        return None
    return min(creds.values(), key=cred_rank)


def save_cred(token, os_type, ver, src):
    """存一份凭据。os/ver 必须一起存 —— 光有 token 不知道该配哪组 x-eta。"""
    creds = load_creds()
    creds[src] = {"token": token, "os": os_type, "ver": ver, "src": src,
                  "saved": time.strftime("%Y-%m-%d %H:%M:%S"), "ts": int(time.time())}
    try:
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(CRED_FILE, "w", encoding="utf-8") as f:
            json.dump(creds, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return creds[src]
