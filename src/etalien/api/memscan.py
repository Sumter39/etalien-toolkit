# -*- coding: utf-8 -*-
"""从客户端进程内存里抓 token / device id / 版本。

客户端 manifest 是 requireAdministrator，普通权限 OpenProcess 会被内核直接拒绝，
所以要提权。提权子进程只负责扫 + 把结果写文件，由普通权限的父进程落盘 ——
避免两个权限级别在 output/ 下交叉写文件带来的 ACL 麻烦。
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import sys
import time

from .. import OUT_DIR
from ..procs import run

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

SCAN_RESULT = os.path.join(OUT_DIR, "_scan_result.json")


def client_pid(name="etalien.exe"):
    """客户端进程号；没在运行返回 None。"""
    o = run(["tasklist", "/FI", "IMAGENAME eq %s" % name, "/FO", "CSV", "/NH"],
            capture_output=True).stdout.decode("gbk", "ignore")
    return int(o.split('","')[1].strip('"')) if '","' in o else None


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def scan():
    """纯扫描：从客户端进程内存提取 token / device id / 版本。

    不落盘、不提权 —— 权限不够直接抛 PermissionError。
    提权子进程只调这个函数，结果交给父进程写盘：避免提权进程和普通进程
    在 output/ 下交叉写文件带来的 ACL 麻烦。
    """
    pid = client_pid()
    if not pid:
        raise RuntimeError("客户端没在运行 —— 先启动外星仔 PC 客户端")
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        raise PermissionError(
            "OpenProcess 被拒绝 err=%d —— 客户端是 requireAdministrator，需要提权读内存"
            % ctypes.get_last_error())

    # 头名大小写不敏感：实测同一份 token 在内存里有 authorization: 和
    # Authorization: 两种形式（大写那几处还更多），HTTP/2 线上强制小写，
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


def scan_job(result_file):
    """提权子进程的入口：扫描 → 结果写文件。

    刻意不 print —— 子进程用 pythonw.exe 拉起，没有 stdout，print 会抛异常。
    """
    res = {"ok": False}
    try:
        r = scan()
        r["ok"] = bool(r.get("token"))
        res.update(r)
    except Exception as e:
        res["error"] = "%s: %s" % (type(e).__name__, e)
    try:
        with open(result_file, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False)
    except Exception:
        pass


def scan_elevated(timeout=120):
    """以管理员身份重新拉起自己抓 token，等它把结果写出来后读回。

    UAC 已关闭时静默完成；正常开启会弹一次确认框，点「是」即可
    （本函数会一直等到超时）。

    重新拉起的是「当初被执行的那个入口脚本」，用 sys.argv[0] 取 —— 库文件自己
    的路径在包里，拿它当入口会把 --elevated 交给一个不能被直接执行的模块。
    """
    try:
        if os.path.exists(SCAN_RESULT):
            os.remove(SCAN_RESULT)
    except Exception:
        pass

    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    exe = pyw if os.path.exists(pyw) else sys.executable
    entry = os.path.abspath(sys.argv[0])
    args = '"%s" scan --elevated --result "%s"' % (entry, SCAN_RESULT)

    try:
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, args, None, 0)
    except Exception as e:
        return {"ok": False, "error": "提权调用异常: %s" % e}
    if rc <= 32:
        return {"ok": False, "error": "提权被拒 rc=%d（UAC 被取消了？）" % rc}

    t0 = time.time()
    while time.time() - t0 < timeout:
        if os.path.exists(SCAN_RESULT):
            time.sleep(0.3)                       # 等写盘落定
            try:
                with open(SCAN_RESULT, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        time.sleep(0.4)
    return {"ok": False, "error": "等提权进程超时（%ds）" % timeout}
