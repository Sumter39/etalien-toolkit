#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""外星仔加速器 API 客户端 —— 命令行工具。

协议：HTTPS + protobuf。鉴权用 authorization 头（裸 token），另需 x-eta 描述头。
x-eta 里的 os/ver 必须与 token 的来源端一致，否则一律回 401 token expired：
PC 端 os=2、App 端 os=1；ver 取实际客户端版本（App 端现场读，PC 端从内存抓），不写死。
dvc 服务端会校验，只能填 output/device.txt 里那一份，乱填回 400 invalid device id。

用法：
    python src/tools/etapi.py duration          # 可暂停时长（PC 端凭据）
    python src/tools/etapi.py sim               # 模拟器端：读 App token + 三档今日进度
    python src/tools/etapi.py state             # 用户状态
    python src/tools/etapi.py pause on|off      # 暂停 / 恢复计时
    python src/tools/etapi.py scan              # 抓 PC 端 token（自动提权）
    python src/tools/etapi.py watch 10 120      # 每 10 秒采样，共 120 秒

scan 会把自己提权重启（客户端 requireAdministrator，普通权限取不到 token）：
UAC 关闭时静默完成，开启时弹一次确认框。抓完自动校验接口并对比新旧 token。
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from etalien import api            # noqa: E402
from etalien import config as cfg   # noqa: E402


def cmd_duration(a):
    st, raw = api.call("/v2/account/remain/duration",
                       api.field(1, 0) if a.json_body else b"")
    if st != 200:
        print("HTTP %s: %r" % (st, raw[:300]))
        return
    fields = api.decode(raw)
    print("HTTP 200  原始: %s" % raw.hex())
    for f, kind, v in fields:
        if kind == "varint":
            note = ""
            if 60 < v < 86400 * 400:
                note = "   (= %s)" % api.fmt_dur(v) if v < 86400 else "   (ts? %d)" % v
            print("  field%d = %s%s" % (f, v, note))
        else:
            print("  field%d = %r" % (f, v))


def cmd_state(a):
    for path in ["/v2/account/user/v2_state/get", "/v2/account/get/time/protection/data",
                 "/v2/account/user/notification/get"]:
        st, raw = api.call(path)
        print("### %s  ->  HTTP %s" % (path, st))
        if raw:
            if raw[:1] in (b"{", b"["):
                print("   ", raw.decode("utf-8", "replace")[:400])
            else:
                print("    hex:", raw.hex()[:300])
                for f, _kind, v in api.decode(raw):
                    print("      field%d = %r" % (f, v))
        print()


def cmd_pause(a):
    want = 1 if a.mode in ("on", "1", "true") else 0
    print("尝试 POST /v2/account/update/pause/state  body: field1=%d" % want)
    for body, desc in [(api.field(1, want), "field1=%d" % want),
                       (api.field(1, 1) + api.field(2, want),
                        "field1=1,field2=%d" % want)]:
        st, raw = api.call("/v2/account/update/pause/state", body)
        print("  [%s] HTTP %s  %s" % (desc, st, raw.hex()[:200]))


def cmd_scan(a):
    """一键抓 token —— 普通权限下自动提权，用户只需要敲一条命令。

    为什么不直接提权：客户端是 requireAdministrator，普通进程 OpenProcess
    会被内核直接拒绝。UAC 已关闭时提权不弹框，整条链路对用户无感；
    正常开启则弹一次确认框。
    """
    # 提权子进程分支：只抓、只写结果文件（pythonw 无 stdout，不能 print）
    if a.elevated:
        return api.scan_job(a.result or api.SCAN_RESULT)

    old = (api.load_creds().get("pc-client") or {}).get("token", "")

    if not a.no_elevate and not api.is_admin():
        if not api.client_pid():
            print("✗ 客户端没在运行 —— 先启动外星仔 PC 客户端并登录")
            sys.exit(1)
        print("当前不是管理员，自动提权抓取…（若弹出 UAC 提示请点「是」）")
        r = api.scan_elevated()
        how = "提权"
    else:
        how = "管理员" if api.is_admin() else "当前权限"
        try:
            r = api.scan()
            r["ok"] = bool(r.get("token"))
        except Exception as e:
            r = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}

    try:
        os.remove(api.SCAN_RESULT)                   # 临时结果文件不留
    except Exception:
        pass

    if not r.get("ok"):
        print("✗ 抓取失败：%s" % (r.get("error") or
              "内存里没找到 token。可能是没登录，也可能是客户端很久没发过请求 ——"
              "token 只在 HTTP 头里短暂停留。点一下客户端界面再试"))
        sys.exit(1)

    tok, dvc = r.get("token"), r.get("dvc")
    # 由当前进程落盘，保证 output/ 下文件归属一致
    os.makedirs(os.path.dirname(api.DVC_FILE), exist_ok=True)
    if dvc:
        open(api.DVC_FILE, "w", encoding="utf-8").write(dvc)
    api.save_cred(tok, api.PC_OS, r.get("ver") or api.PC_VER, "pc-client")

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
    st, raw = api.call("/v2/account/remain/duration", timeout=15)
    if st == 200:
        fs = {f: v for f, k, v in api.decode(raw) if k == "varint"}
        print("校验  : HTTP 200 OK    剩余 %s" % api.fmt_dur(fs.get(1, 0)))
    else:
        print("校验  : HTTP %s  %r  ← token 可能已失效" % (st, raw[:80]))


def cmd_watch(a):
    total = 0
    print("每 %ds 采样一次，共 %ds" % (a.interval, a.seconds))
    print("%9s  %12s  %12s  变化" % ("时刻", "时长", "field3"))
    prev = None
    while total <= a.seconds:
        st, raw = api.call("/v2/account/remain/duration")
        if st == 200:
            fs = {f: v for f, k, v in api.decode(raw) if k == "varint"}
            d = fs.get(1)
            f3 = fs.get(3)
            delta = ""
            if prev is not None and d is not None:
                delta = "%+ds" % (d - prev)
            print("%9s  %12s  %12s  %s" % (
                time.strftime("%H:%M:%S"),
                api.fmt_dur(d) if d is not None else "?",
                str(f3), delta), flush=True)
            prev = d
        else:
            print("%9s  HTTP %s %r" % (time.strftime("%H:%M:%S"), st, raw[:80]), flush=True)
        time.sleep(a.interval)
        total += a.interval


def cmd_sim(a):
    """模拟器端读一份进度（不点广告、不改状态）。"""
    adb = a.adb or cfg.adb_path()
    serial = a.serial or cfg.serial()

    r = api.read_progress(adb, serial)
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
    print("时长   : %s" % (api.fmt_dur(r["seconds"]) if r["seconds"] is not None else "?"))
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
