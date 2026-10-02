"""外星仔加速器 API 客户端（PC 客户端 + 安卓 App 两端）。

协议：HTTPS + protobuf。鉴权用 authorization 头（裸 token），另需 x-eta 描述头。
x-eta 里的 os/ver 必须与 token 的来源端一致，否则一律回 401 token expired：
PC 端 os=2、App 端 os=1；ver 取实际客户端版本（App 端由 `android_ver()` 现场读，
PC 端由 `scan` 从内存抓），不写死。
`dvc` 服务端会校验，只能填 output/device.txt 里那一份，乱填回 400 invalid device id。

用法：
    python etapi.py duration          # 可暂停时长（PC 端凭据）
    python etapi.py sim               # 模拟器端：读 App token + 三档今日进度
    python etapi.py state             # 用户状态
    python etapi.py pause on|off      # 暂停 / 恢复计时
    python etapi.py scan              # 抓 PC 端 token（自动提权）
    python etapi.py watch 10 120      # 每 10 秒采样，共 120 秒

`scan` 会把自己提权重启（客户端 requireAdministrator，普通权限取不到 token）：
UAC 关闭时静默完成，开启时弹一次确认框。抓完自动校验接口并对比新旧 token。

`sim` 走模拟器：token 从 App 自己的 SharedPreferences 现读（App 会定期 refresh，
所以每次读到的就是最新的），进度读 /v2/account/pc/ad/config 的 watchCnt。
"""

import argparse
import ctypes
import ctypes.wintypes as wt
import gzip
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = "https://api.et-api.com"

# x-eta 里的 os/ver 必须与 token 的来源端配对，配错一律 401 token expired。
# App 端 ver 由 `android_ver()` 现场从模拟器读（写死的话 App 一升级就全线 401）。
PC_OS, PC_VER = 2, "1.24.11"
ANDROID_OS = 1

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "output")
DVC_FILE = os.path.join(OUT_DIR, "device.txt")
# 凭据 + 来源端（os/ver）。设备 ID 单独放 device.txt（它与机器绑定，换机要重填）。
CRED_FILE = os.path.join(OUT_DIR, "cred.json")


def x_eta(dvc, os_type=PC_OS, ver=PC_VER):
    """构造 x-eta 描述头。os/ver 必须与 token 的来源端一致。"""
    return "os=%s&ver=%s&dvc=%s&ch=default" % (os_type, ver, dvc)



# ---------------------------------------------------------------- 凭据


def load_token(src=None):
    """取一份 token。凭据连来源端（os/ver）一起存在 output/cred.json。"""
    c = load_cred(src)
    if c:
        return c["token"]
    raise SystemExit(
        "没有可用凭据 —— 跑 `python etapi.py scan` 抓 PC 端的，"
        "或先跑一次 checkin（它会顺手存下模拟器端 App 的 token）"
    )


def load_dvc():
    """设备 ID（x-eta 头里的 dvc）。

    由 `scan` 从客户端内存抓出来后落在 output/device.txt。
    **这里刻意不给默认值** —— 它跟具体设备绑定，写死一个值等于把别人的
    设备 ID 带进你的仓库，且换机后会出现「看着能跑、其实用错 ID」的隐蔽故障。

    服务端会校验它是否属于当前账号：随便填会回 `400 invalid device id`，
    所以模拟器端也复用这一份，不要另外造。
    """
    if os.path.exists(DVC_FILE):
        d = open(DVC_FILE, encoding="utf-8").read().strip()
        if d:
            return d
    raise SystemExit(
        "没有设备 ID —— 先跑 `python etapi.py scan`"
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

    谁最后被刷新过谁就最新 —— 两端都会写 `saved` / `ts`：`scan` 刷 pc-client，
    签到每轮刷 android-app。时间并列时（同一秒内写的）按 src 定序，保证稳定。
    """
    return (-cred.get("ts", 0), cred.get("src") or "")


_preferred_src = None


def prefer_cred(src):
    """指定后续 `call()` 用哪一份凭据。

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


def xeta_of(cred, dvc):
    """按凭据的来源端拼 x-eta。"""
    return x_eta(dvc, cred.get("os", PC_OS), cred.get("ver", PC_VER))


def check_cred(src=None, timeout=10):
    """探活：拿凭据打一次只读接口。返回 dict：ok / token / os / ver / src / status。

    `ok` 是**三态**，调用方必须按三态处理：

        True    200，这份凭据能用
        False   401，这份凭据明确失效（换掉 / 重新登录）
        None    未知 —— 没网、超时、5xx 等。**不能当成失效**

    最后一条是踩过坑的：网络一抖就判「失效」，会触发一次毫无意义的换凭据
    （乃至启动模拟器续期）。
    """
    cred = load_cred(src)
    if not cred:
        return {"ok": False, "reason": "没有凭据"}
    try:
        dvc = load_dvc()
    except SystemExit as e:
        return {"ok": False, "reason": str(e)}
    st, raw = call("/v2/account/remain/duration", b"", cred["token"], dvc,
                   timeout=timeout, xeta=xeta_of(cred, dvc))
    out = dict(cred)
    out["status"] = st
    if st == 200:
        out["ok"] = True
    elif st == 401:
        out["ok"] = False
        out["reason"] = "HTTP 401 %r" % raw[:60]
    else:
        out["ok"] = None
        out["reason"] = ("网络不可达 %r" % raw[:60]) if st is None \
            else "HTTP %s %r" % (st, raw[:60])
    return out


# ---------------------------------------------------------------- HTTP


def call(path, body=b"", token=None, dvc=None, timeout=20, extra=None, xeta=None):
    """POST 一个接口。

    token/dvc 缺省从 output/ 读；xeta 缺省**跟着凭据的来源端**走 —— cred.json
    里存了 os/ver，配错会一律回 401。
    """
    token = token or load_token()
    dvc = dvc or load_dvc()
    if not xeta:
        cred = load_cred()
        xeta = xeta_of(cred, dvc) if cred else x_eta(dvc)
    req = urllib.request.Request(BASE + path, data=body, method="POST")
    headers = {
        "authorization": token,
        "x-eta": xeta,
        "user-agent": f"Dart/3.10 (dart:io)",
        "content-type": "application/x-protobuf",
        "accept": "application/x-protobuf",
        "accept-encoding": "gzip",
    }
    if extra:
        headers.update(extra)
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return r.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            if e.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
        except Exception:
            pass
        return e.code, raw
    except Exception as e:
        return None, f"{type(e).__name__}: {e}".encode()


# ---------------------------------------------------------------- protobuf


def read_varint(b, i):
    n = 0
    shift = 0
    while i < len(b):
        x = b[i]
        i += 1
        n |= (x & 0x7F) << shift
        if not x & 0x80:
            return n, i
        shift += 7
        if shift > 63:
            break
    raise ValueError("varint 溢出")


def pb_decode(b):
    """解出 [(field_no, kind, value), ...]，不含 .proto 也能读。"""
    out = []
    i = 0
    while i < len(b):
        try:
            key, i = read_varint(b, i)
        except ValueError:
            break
        field, wire = key >> 3, key & 7
        if wire == 0:
            v, i = read_varint(b, i)
            out.append((field, "varint", v))
        elif wire == 2:
            ln, i = read_varint(b, i)
            out.append((field, "bytes", b[i:i + ln]))
            i += ln
        elif wire == 5:
            out.append((field, "f32", b[i:i + 4]))
            i += 4
        elif wire == 1:
            out.append((field, "f64", b[i:i + 8]))
            i += 8
        else:
            break
    return out


def pb_varint(n):
    out = bytearray()
    while True:
        x = n & 0x7F
        n >>= 7
        out.append(x | 0x80 if n else x)
        if not n:
            return bytes(out)


def pb_v(f, n):
    return pb_varint(f << 3) + pb_varint(n)


def pb_b(f, b):
    if isinstance(b, str):
        b = b.encode()
    return pb_varint((f << 3) | 2) + pb_varint(len(b)) + b


def fmt_dur(sec):
    return f"{sec // 3600}时{sec % 3600 // 60}分{sec % 60}秒"


# ---------------------------------------------------------------- 模拟器端（安卓 App）


ANDROID_PKG = "com.etalien.booster"
ANDROID_SP = "/data/data/%s/shared_prefs/spUtils.xml" % ANDROID_PKG

_TOKEN_RE = re.compile(r'name="CUR_USER_TOKEN">([^<]+)<')
_REFRESH_RE = re.compile(r'name="LAST_REFRESH_TOKEN_TIME" value="(\d+)"')
_VER_RE = re.compile(r"versionName=([\d.]+)")


class TokenExpired(Exception):
    """服务端回 401 —— token 失效，调用方需要重新取一份再试。"""


def android_sp(adb, serial, timeout=20):
    """读 App 的 SharedPreferences 文本；非 root 时自动提权重试一次。

    ⚠ 这份文件里还有 LAST_LOGIN_PHONE（手机号），调用方**不要**把它落盘或打印。
    """
    def _cat():
        try:
            r = subprocess.run([adb, "-s", serial, "shell", "cat", ANDROID_SP],
                               capture_output=True, timeout=timeout)
            return (r.stdout or b"").decode("utf-8", "ignore")
        except Exception:
            return ""

    xml = _cat()
    if "CUR_USER_TOKEN" not in xml:                      # Permission denied
        try:
            subprocess.run([adb, "-s", serial, "root"], capture_output=True, timeout=30)
            time.sleep(2)
            subprocess.run([adb, "-s", serial, "wait-for-device"],
                           capture_output=True, timeout=30)
        except Exception:
            pass
        xml = _cat()
    return xml


def android_token(adb, serial):
    """App 当前 token 与它的续期时间戳（LAST_REFRESH_TOKEN_TIME）。

    每次都现读：App 会按需调 /v2/account/refresh/token 续期，读到的就是最新那份。
    实测 token 新鲜时冷启动并不会触发续期，所以「刷新」得靠 401 时把它摇醒
    （见 `wake_android_app` / `read_progress`）。
    """
    xml = android_sp(adb, serial)
    m = _TOKEN_RE.search(xml)
    if not m:
        return None, None
    ts = _REFRESH_RE.search(xml)
    return m.group(1).strip(), (int(ts.group(1)) if ts else None)


def android_ver(adb, serial, timeout=20):
    """App 实际版本号，用来拼 x-eta 的 ver。

    不能写死：ver 与 token 来源端不匹配一律回 401，App 一升级写死的值就废了。
    读不到返回 None —— 调用方应当直接失败，不要拿旧值顶上。
    """
    try:
        r = subprocess.run([adb, "-s", serial, "shell", "dumpsys", "package", ANDROID_PKG],
                           capture_output=True, timeout=timeout)
    except Exception:
        return None
    m = _VER_RE.search((r.stdout or b"").decode("utf-8", "ignore"))
    return m.group(1) if m else None


def sim_call(path, token, ver, timeout=20):
    """用安卓端 x-eta 调接口，返回原始响应。

    401 抛 TokenExpired，其余非 200 抛 RuntimeError（把服务端原因带出来）。
    dvc 一律取本机那个（`scan` 抓出来落在 output/device.txt）—— 它跟 token 的
    来源端**无关**，但服务端会校验它是否属于当前账号：随便填会回
    `400 invalid device id`，拿 App 自己的 android_id 去试同样不行。
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
        f2 list[]   广告条目，**条目个数 = 该档总次数**
        f3 watchCnt **今日已完成次数**
        f6 title    阶段一 / 阶段二 / 加油包

    App 自己算 N/M 用的就是这两个字段（APK 里 PcAdConfigLevelItem 只剩
    getWatchCnt / getListList 这两个 getter 被 R8 保留），所以这是权威数据。
    """
    stages = []
    for f, kind, v in pb_decode(raw):
        if f != 1 or kind != "bytes":
            continue
        it = {"title": "", "done": 0, "total": 0}
        for ff, kk, vv in pb_decode(v):
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
    return next((v for f, k, v in pb_decode(raw) if k == "varint" and f == 1), None)


def wake_android_app(adb, serial, wait=15, timeout=60):
    """把 App 冷启动一次，逼它自己去续期 token。

    续期接口 `/v2/account/refresh/token` 要客户端签名（`sig`），我们算不出来，
    所以**不自己续**：只负责把 App 摇醒，让它按自己的规则刷新，再把新凭据读回来。
    （实测 App 只在 token 需要刷新时才动手，token 新鲜时冷启动不会改它。）
    """
    main = "%s/%s.ui.MainActivity" % (ANDROID_PKG, ANDROID_PKG)
    try:
        subprocess.run([adb, "-s", serial, "shell", "am", "force-stop", ANDROID_PKG],
                       capture_output=True, timeout=timeout)
        time.sleep(1.5)
        subprocess.run([adb, "-s", serial, "shell", "am", "start", "-n", main],
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
    token, ts = android_token(adb, serial)
    if not token:
        out["reason"] = "读不到 App token（模拟器没起来 / 未登录）"
        return out
    out["token"], out["token_ts"] = token, ts
    ver = android_ver(adb, serial)
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
        wake_android_app(adb, serial)
        token2, ts2 = android_token(adb, serial)
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
    # **每次都要复写**（不看 token 变没变）：saved / ts 是「谁最新」的依据，
    # 跳过写入会让这份的时间戳停在旧值，选凭据时就轮不到它。
    save_cred(out["token"], ANDROID_OS, ver, "android-app")
    out["ok"] = True
    return out


# ---------------------------------------------------------------- 内存抓 token


PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01
k32 = ctypes.WinDLL("kernel32", use_last_error=True)


class MBI(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_ulonglong), ("AllocationBase", ctypes.c_ulonglong),
        ("AllocationProtect", wt.DWORD), ("PartitionId", wt.WORD), ("__pad1", wt.WORD),
        ("RegionSize", ctypes.c_ulonglong), ("State", wt.DWORD), ("Protect", wt.DWORD),
        ("Type", wt.DWORD), ("__pad2", wt.DWORD),
    ]


k32.OpenProcess.restype = wt.HANDLE
k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.VirtualQueryEx.restype = ctypes.c_size_t
k32.VirtualQueryEx.argtypes = [wt.HANDLE, ctypes.c_ulonglong, ctypes.POINTER(MBI), ctypes.c_size_t]
k32.ReadProcessMemory.restype = wt.BOOL
k32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_ulonglong, ctypes.c_void_p,
                                  ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]


def _pid(name="etalien.exe"):
    o = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV", "/NH"],
                       capture_output=True).stdout.decode("gbk", "ignore")
    return int(o.split('","')[1].strip('"')) if '","' in o else None


def scan_memory():
    """纯扫描：从客户端进程内存提取 token / device id / 版本。

    **不落盘、不提权** —— 权限不够直接抛 PermissionError。
    提权子进程只调这个函数，结果交给父进程写盘：避免提权进程和普通进程
    在 output/ 下交叉写文件带来的 ACL 麻烦。
    """
    pid = _pid()
    if not pid:
        raise RuntimeError("客户端没在运行 —— 先启动外星仔 PC 客户端")
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        raise PermissionError(
            "OpenProcess 被拒绝 err=%d —— 客户端是 requireAdministrator，需要提权读内存"
            % ctypes.get_last_error())

    # 头名大小写不敏感：实测同一份 token 在内存里有 `authorization:` 和
    # `Authorization:` 两种形式（大写那几处还更多），HTTP/2 线上强制小写，
    # 但内存里缓存的原始头并不统一。只认小写会白丢一大半命中机会。
    tok_pat = re.compile(rb"authorization:\s*([A-Za-z0-9_\-]{60,})", re.I)
    xeta_pat = re.compile(rb"x-eta@?[:\s]*os=(\d+)&ver=([\d.]+)&dvc=([0-9a-f]{16,})&ch=(\w+)", re.I)
    token = dvc = ver = None

    addr, mbi = 0, MBI()
    total = 0
    t0 = time.time()
    while addr < 0x7FFFFFFFFFFF and time.time() - t0 < 120:
        if not k32.VirtualQueryEx(h, addr, ctypes.byref(mbi), ctypes.sizeof(mbi)):
            break
        size = mbi.RegionSize
        if size == 0:
            break
        if (mbi.State == MEM_COMMIT and not (mbi.Protect & PAGE_GUARD)
                and mbi.Protect != PAGE_NOACCESS and total < (7 << 30)):
            size = min(size, 64 << 20)
            try:
                buf = ctypes.create_string_buffer(size)
                got = ctypes.c_size_t(0)
                if k32.ReadProcessMemory(h, mbi.BaseAddress, buf, size, ctypes.byref(got)):
                    data = buf.raw[:got.value]
                    total += len(data)
                    if not token:
                        m = tok_pat.search(data)
                        if m:
                            token = m.group(1).decode()
                    if not dvc:
                        m = xeta_pat.search(data)
                        if m:
                            ver, dvc = m.group(2).decode(), m.group(3).decode()
                    if token and dvc:
                        break
            except Exception:
                pass
        addr = mbi.BaseAddress + mbi.RegionSize

    return {"token": token, "dvc": dvc, "ver": ver,
            "scanned_mb": round(total / 1048576, 1), "seconds": round(time.time() - t0, 1)}


# ---------------------------------------------------------------- 一键抓取（自动提权）


_SCAN_RESULT = os.path.join(OUT_DIR, "_scan_result.json")


def _is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _scan_job(result_file):
    """提权子进程的入口：扫描 → 结果写文件。

    刻意不 print —— 子进程用 pythonw.exe 拉起，没有 stdout，print 会抛异常。
    """
    res = {"ok": False}
    try:
        r = scan_memory()
        r["ok"] = bool(r.get("token"))
        res.update(r)
    except Exception as e:
        res["error"] = "%s: %s" % (type(e).__name__, e)
    try:
        with open(result_file, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False)
    except Exception:
        pass


def _scan_elevated(timeout=120):
    """以管理员身份重新拉起自己抓 token，等它把结果写出来后读回。

    UAC 已关闭时静默完成；正常开启会弹一次确认框，点「是」即可
    （本函数会一直等到超时）。
    """
    try:
        if os.path.exists(_SCAN_RESULT):
            os.remove(_SCAN_RESULT)
    except Exception:
        pass

    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    exe = pyw if os.path.exists(pyw) else sys.executable
    args = '"%s" scan --elevated --result "%s"' % (os.path.abspath(__file__), _SCAN_RESULT)

    try:
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, args, None, 0)
    except Exception as e:
        return {"ok": False, "error": "提权调用异常: %s" % e}
    if rc <= 32:
        return {"ok": False, "error": "提权被拒 rc=%d（UAC 被取消了？）" % rc}

    t0 = time.time()
    while time.time() - t0 < timeout:
        if os.path.exists(_SCAN_RESULT):
            time.sleep(0.3)                       # 等写盘落定
            try:
                with open(_SCAN_RESULT, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        time.sleep(0.4)
    return {"ok": False, "error": "等提权进程超时（%ds）" % timeout}



# ---------------------------------------------------------------- 命令


def cmd_duration(a):
    st, raw = call("/v2/account/remain/duration", pb_v(1, 0) if a.json_body else b"")
    if st != 200:
        print(f"HTTP {st}: {raw[:300]!r}")
        return
    fields = pb_decode(raw)
    print(f"HTTP 200  原始: {raw.hex()}")
    for f, kind, v in fields:
        if kind == "varint":
            note = ""
            if 60 < v < 86400 * 400:
                note = f"   (= {fmt_dur(v)})" if v < 86400 else f"   (ts? {v})"
            print(f"  field{f} = {v}{note}")
        else:
            print(f"  field{f} = {v!r}")


def cmd_state(a):
    for path in ["/v2/account/user/v2_state/get", "/v2/account/get/time/protection/data",
                 "/v2/account/user/notification/get"]:
        st, raw = call(path)
        print(f"### {path}  ->  HTTP {st}")
        if raw:
            if raw[:1] in (b"{", b"["):
                print("   ", raw.decode("utf-8", "replace")[:400])
            else:
                print("    hex:", raw.hex()[:300])
                for f, kind, v in pb_decode(raw):
                    print(f"      field{f} = {v!r}")
        print()


def cmd_pause(a):
    want = 1 if a.mode in ("on", "1", "true") else 0
    print(f"尝试 POST /v2/account/update/pause/state  body: field1={want}")
    for body, desc in [(pb_v(1, want), f"field1={want}"),
                       (pb_v(1, 1) + pb_v(2, want), f"field1=1,field2={want}")]:
        st, raw = call("/v2/account/update/pause/state", body)
        print(f"  [{desc}] HTTP {st}  {raw.hex()[:200]}")


def cmd_scan(a):
    """一键抓 token —— 普通权限下自动提权，用户只需要敲一条命令。

    为什么不直接提权：客户端是 requireAdministrator，普通进程 OpenProcess
    会被内核直接拒绝。UAC 已关闭时提权不弹框，整条链路对用户无感；
    正常开启则弹一次确认框。
    """
    # 提权子进程分支：只抓、只写结果文件（pythonw 无 stdout，不能 print）
    if a.elevated:
        return _scan_job(a.result or _SCAN_RESULT)

    old = (load_creds().get("pc-client") or {}).get("token", "")

    if not a.no_elevate and not _is_admin():
        if not _pid():
            print("✗ 客户端没在运行 —— 先启动外星仔 PC 客户端并登录")
            sys.exit(1)
        print("当前不是管理员，自动提权抓取…（若弹出 UAC 提示请点「是」）")
        r = _scan_elevated()
        how = "提权"
    else:
        how = "管理员" if _is_admin() else "当前权限"
        try:
            r = scan_memory()
            r["ok"] = bool(r.get("token"))
        except Exception as e:
            r = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}

    try:
        os.remove(_SCAN_RESULT)                   # 临时结果文件不留
    except Exception:
        pass

    if not r.get("ok"):
        print("✗ 抓取失败：%s" % (r.get("error") or
              "内存里没找到 token。可能是没登录，也可能是客户端很久没发过请求 ——"
              "token 只在 HTTP 头里短暂停留。点一下客户端界面再试"))
        sys.exit(1)

    tok, dvc = r.get("token"), r.get("dvc")
    # 由当前进程落盘，保证 output/ 下文件归属一致
    os.makedirs(OUT_DIR, exist_ok=True)
    if dvc:
        open(DVC_FILE, "w", encoding="utf-8").write(dvc)
    save_cred(tok, PC_OS, r.get("ver") or PC_VER, "pc-client")

    print("扫描 %.1f MB / %.1fs（%s）" % (r.get("scanned_mb") or 0, r.get("seconds") or 0, how))
    print("token : %s…" % tok[:40])
    print("dvc   : %s" % dvc)
    print("ver   : %s" % r.get("ver"))
    if not old:
        print("状态  : 首次抓取 → 已写入 output/cred.json（pc-client）")
    elif old == tok:
        print("状态  : 与上次相同（token 没变）")
    else:
        print("状态  : ↻ 与上次不同 → 已更新 output/cred.json（pc-client）")

    # 顺手验一下这把 token 能不能用
    st, raw = call("/v2/account/remain/duration", timeout=15)
    if st == 200:
        fs = {f: v for f, k, v in pb_decode(raw) if k == "varint"}
        print("校验  : HTTP 200 OK    剩余 %s" % fmt_dur(fs.get(1, 0)))
    else:
        print("校验  : HTTP %s  %r  ← token 可能已失效" % (st, raw[:80]))


def cmd_watch(a):
    n, total = 0, 0
    print(f"每 {a.interval}s 采样一次，共 {a.seconds}s")
    print(f"{'时刻':>9}  {'时长':>12}  {'field3':>12}  变化")
    prev = None
    while total <= a.seconds:
        st, raw = call("/v2/account/remain/duration")
        if st == 200:
            fs = {f: v for f, k, v in pb_decode(raw) if k == "varint"}
            d = fs.get(1)
            f3 = fs.get(3)
            delta = ""
            if prev is not None and d is not None:
                delta = f"{d - prev:+d}s"
            print(f"{time.strftime('%H:%M:%S'):>9}  {fmt_dur(d) if d is not None else '?':>12}"
                  f"  {str(f3):>12}  {delta}", flush=True)
            prev = d
        else:
            print(f"{time.strftime('%H:%M:%S'):>9}  HTTP {st} {raw[:80]!r}", flush=True)
        time.sleep(a.interval)
        total += a.interval


def cmd_sim(a):
    """模拟器端读一份进度（不点广告、不改状态）。"""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import etconfig as CFG
    from probe import find_adb

    adb = a.adb or find_adb()
    serial = a.serial or CFG.require("serial", "模拟器 adb 地址，如 127.0.0.1:16384")

    r = read_progress(adb, serial)
    print("adb    : %s" % adb)
    print("serial : %s" % serial)
    if not r["ok"]:
        print("✗ 读取失败：%s" % r["reason"])
        return
    print("token  : %s…" % r["token"][:40])
    if r["token_ts"]:
        print("续期于 : %s" % time.strftime("%Y-%m-%d %H:%M:%S",
                                            time.localtime(r["token_ts"] / 1000
                                                           if r["token_ts"] > 1e11
                                                           else r["token_ts"])))
    print("时长   : %s" % (fmt_dur(r["seconds"]) if r["seconds"] is not None else "?"))
    print("进度   :")
    for s in r["stages"] or []:
        print("         %-6s %d/%d" % (s["title"], s["done"], s["total"]))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("duration", help="查可暂停时长")
    p.add_argument("--json-body", action="store_true", help="空 body 还是 field1=0")
    p.set_defaults(func=cmd_duration)

    sub.add_parser("state", help="查状态类接口").set_defaults(func=cmd_state)

    p = sub.add_parser("pause", help="设置暂停状态")
    p.add_argument("mode", choices=["on", "off"])
    p.set_defaults(func=cmd_pause)

    p = sub.add_parser("scan", help="一键抓 token（普通权限下自动提权，无需手动开管理员 CMD）")
    p.add_argument("--no-elevate", action="store_true", help="不自动提权，只用当前权限试")
    p.add_argument("--elevated", action="store_true", help=argparse.SUPPRESS)   # 内部：提权子进程
    p.add_argument("--result", default=None, help=argparse.SUPPRESS)            # 内部：结果回传路径
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("watch", help="连续采样观察时长走势")
    p.add_argument("interval", type=int, nargs="?", default=10)
    p.add_argument("seconds", type=int, nargs="?", default=120)
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("sim", help="模拟器端：读 App token + 今日广告进度")
    p.add_argument("--adb", default=None, help="adb.exe 路径（默认读 config.json 的 adb_path）")
    p.add_argument("--serial", default=None, help="模拟器 adb 地址（默认读 config.json 的 serial）")
    p.set_defaults(func=cmd_sim)

    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
