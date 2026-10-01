"""外星仔加速器 PC 端 API 客户端。

接口逆向自客户端进程内存（app.so 里的路由 + 运行时 HTTP/2 报文）。
协议：HTTPS + protobuf，鉴权靠 authorization 头（裸 token），
另需 x-eta 描述头（os/ver/dvc/ch），sig 非强制。

用法：
    python etapi.py duration          # 可暂停时长
    python etapi.py state             # 用户状态
    python etapi.py protection        # 时长保护数据
    python etapi.py pause on|off      # 暂停 / 恢复计时
    python etapi.py scan              # 一键抓 token（自动提权，不用手动开管理员 CMD）
    python etapi.py watch 10 120      # 每 10 秒采样一次，共 120 秒，观察时长走势

关于 scan
---------
token 是明文躺在客户端进程内存里的，但客户端 manifest 是 requireAdministrator，
普通权限 OpenProcess 会被直接拒绝。所以 `scan` 会自动把自己提权再抓：
UAC 已关闭时静默完成；正常开启会弹一次确认框，点「是」即可。
抓完会自动校验一次接口可用性，并对比新旧 token 是否变化。
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
CLIENT_VER = "1.24.11"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "output")
TOKEN_FILE = os.path.join(OUT_DIR, "token.txt")
DVC_FILE = os.path.join(OUT_DIR, "device.txt")

DEFAULT_XETA = "os=2&ver=" + CLIENT_VER + "&dvc={dvc}&ch=default"


# ---------------------------------------------------------------- 凭据


def load_token():
    if os.path.exists(TOKEN_FILE):
        t = open(TOKEN_FILE, encoding="utf-8").read().strip()
        if t:
            return t
    raise SystemExit("没有 token —— 先跑 `python etapi.py scan`（需要客户端在运行）")


def load_dvc():
    """设备 ID（x-eta 头里的 dvc）。

    由 `scan` 从客户端内存抓出来后落在 output/device.txt。
    **这里刻意不给默认值** —— 它跟具体设备绑定，写死一个值等于把别人的
    设备 ID 带进你的仓库，且换机后会出现「看着能跑、其实用错 ID」的隐蔽故障。
    """
    if os.path.exists(DVC_FILE):
        d = open(DVC_FILE, encoding="utf-8").read().strip()
        if d:
            return d
    raise SystemExit(
        "没有设备 ID —— 先跑 `python etapi.py scan`"
        "（需要外星仔 PC 客户端在运行且已登录）"
    )


# ---------------------------------------------------------------- HTTP


def call(path, body=b"", token=None, dvc=None, timeout=20, extra=None):
    token = token or load_token()
    dvc = dvc or load_dvc()
    req = urllib.request.Request(BASE + path, data=body, method="POST")
    headers = {
        "authorization": token,
        "x-eta": DEFAULT_XETA.format(dvc=dvc),
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

    tok_pat = re.compile(rb"authorization:\s*([A-Za-z0-9_\-]{60,})")
    xeta_pat = re.compile(rb"x-eta@?[:\s]*os=(\d+)&ver=([\d.]+)&dvc=([0-9a-f]{16,})&ch=(\w+)")
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


def scan_creds():
    """扫描并写入 output/（提权后可直接调用）。"""
    r = scan_memory()
    os.makedirs(OUT_DIR, exist_ok=True)
    if r["token"]:
        open(TOKEN_FILE, "w", encoding="utf-8").write(r["token"])
    if r["dvc"]:
        open(DVC_FILE, "w", encoding="utf-8").write(r["dvc"])
    return r


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
    if getattr(a, "elevated", False):
        return _scan_job(a.result or _SCAN_RESULT)

    old = ""
    if os.path.exists(TOKEN_FILE):
        old = open(TOKEN_FILE, encoding="utf-8").read().strip()

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
        print("✗ 抓取失败：%s" % (r.get("error") or "内存里没找到 token（客户端登录了吗？）"))
        sys.exit(1)

    tok, dvc = r.get("token"), r.get("dvc")
    # 由当前进程落盘，保证 output/ 下文件归属一致
    os.makedirs(OUT_DIR, exist_ok=True)
    open(TOKEN_FILE, "w", encoding="utf-8").write(tok)
    if dvc:
        open(DVC_FILE, "w", encoding="utf-8").write(dvc)

    print("扫描 %.1f MB / %.1fs（%s）" % (r.get("scanned_mb") or 0, r.get("seconds") or 0, how))
    print("token : %s…" % tok[:40])
    print("dvc   : %s" % dvc)
    print("ver   : %s" % r.get("ver"))
    if not old:
        print("状态  : 首次抓取 → 已写入 output/token.txt")
    elif old == tok:
        print("状态  : 与上次相同（token 没变）")
    else:
        print("状态  : ↻ 与上次不同 → 已更新 output/token.txt")

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

    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
