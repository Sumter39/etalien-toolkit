# -*- coding: utf-8 -*-
"""凭据探活与补齐。

token 过期后没法自己续 —— 续期接口要客户端签名，算不出来（PC 客户端自己也不会
刷新：实测重启客户端两分钟都不产出 token）。所以只能「换一份」：cred.json 里通常
有两份 ——

    pc-client     etapi.py scan 抓的，客户端登录态还在时才刷新得出来
    android-app   每天跑一次 adwatch 时顺手存下的。App 自己带续期逻辑，这份活得久

按保存时间从新到旧探活：谁最后被刷新过，谁最可能还有效。哪份通过就用哪份并钉住，
后续请求都照它的 os/ver 拼 x-eta。

只有 401 才判失效：网络不通 / 超时 / 5xx 一律算「未知」——这时既不该报警
（凭据可能是好的），更不该去启动模拟器续期（白折腾一分钟，网络恢复就好了）。
"""
import os
import sys
import time

from .. import SRC_DIR, api
from ..procs import run
from .state import _STATE, log

CRED_RECHECK = 1800        # 凭据复检间隔（秒）
RENEW_GAP = 1800           # 两次「补凭据」之间的最小间隔（秒）
SCAN_TRIES = 3             # 重抓 PC 端时扫几次
SCAN_GAP = 5               # 两次扫描之间的间隔（秒）

_last_cred_check = 0.0
_last_renew = 0.0


def resolve_cred(force=False):
    """挑一份还能用的凭据并钉住它；返回 (ok, 说明)。

    ok 三态：True 有一份能用 / False 全部明确失效（401）/ None 判断不了（没网）。

    跳过的那个分支不等于「可用」：必须沿用上次结论，否则「凭据全废」会被翻成
    「可用」，既让 --status 报假象，又让 poll_loop 永远等不到 renew 的触发条件
    （它只在 ok is False 时才续期），等于白等一个复检周期。
    """
    global _last_cred_check
    now = time.time()
    if not force and now - _last_cred_check < CRED_RECHECK:
        return _STATE.get("cred_ok", True), \
            "距上次复检不足 %d 分钟，沿用上次结论" % (CRED_RECHECK // 60)
    _last_cred_check = now

    creds = api.load_creds()
    if not creds:
        return False, "没有凭据（先跑 tools/etapi.py scan，或跑一次 adwatch）"
    dead, unknown = [], []
    for c in sorted(creds.values(), key=api.cred_rank):
        r = api.check_cred(c["src"])
        if r.get("ok"):
            api.prefer_cred(c["src"])
            return True, "%s（%s 存）" % (c["src"], c.get("saved"))
        note = "%s %s" % (c["src"], r.get("reason") or r.get("status"))
        (unknown if r.get("ok") is None else dead).append(note)
    if dead:
        return False, "凭据全部失效：" + "；".join(dead)
    return None, "判断不了（非 401）：" + "；".join(unknown)


def _run_child(rel, args, timeout):
    """跑 src/ 下的入口脚本，返回 (退出码, 输出末行)。

    rel 是相对 src/ 的路径，例如 tools/etapi.py。
    """
    script = os.path.join(SRC_DIR, rel)
    if not os.path.exists(script):
        return 127, "找不到 %s" % rel
    py = os.path.join(os.path.dirname(sys.executable), "python.exe")
    if not os.path.exists(py):
        py = sys.executable
    try:
        r = run([py, script] + args, cwd=os.path.dirname(script),
                capture_output=True, timeout=timeout)
    except Exception as e:
        return -1, "%s: %s" % (type(e).__name__, e)
    tail = (r.stdout or b"").decode("utf-8", "ignore").strip().splitlines()
    return r.returncode, (tail[-1] if tail else "")


def renew_cred():
    """两份凭据都失效时补一份新的 —— 能不动模拟器就不动。

    顺序和 cred_rank() 同一原则，先 PC 后 App：

    1. tools/etapi.py scan —— 提权读 PC 客户端内存抓它当前的 token，约 1 秒，
       不启模拟器。客户端在跑且发过请求时走得通。token 明文只在请求头
       缓冲区里存活，会被回收 / 换页，所以隔几秒连扫 SCAN_TRIES 次。
       客户端没在运行时直接跳过这一步（没有内存可读，扫了也是空手而归）。
    2. scripts/adwatch.py --renew-token —— 启模拟器借 App 的手续一份，约 1 分钟。
       上面那条走不通（客户端没开 / 内存里没有现成 token）才走这条。

    只能在 poll_loop（独立线程）里调用：最坏会阻塞一分钟以上，绝不能放进
    主线程的关机 / 休眠处理里 —— 那时候离断电只剩几秒。
    """
    global _last_renew
    now = time.time()
    if now - _last_renew < RENEW_GAP:
        return False, "距上次尝试不足 %d 分钟，跳过" % (RENEW_GAP // 60)
    _last_renew = now

    # 客户端没开就没有内存可读，scan 会进门就退（退出码 1）。先探一下进程：
    # 否则 SCAN_TRIES 次秒退 + 中间两次 SCAN_GAP 等待全白费（约 12 秒）。
    scan_rel = os.path.join("tools", "etapi.py")
    if not api.client_pid():
        log("PC 客户端没在运行，重抓无从谈起 → 直接启动模拟器借 App 续期…")
    else:
        log("凭据全部失效 —— 先试提权重抓 PC 客户端（不启模拟器）…")
        rc, tail = 1, ""
        for i in range(SCAN_TRIES):
            rc, tail = _run_child(scan_rel, ["scan"], timeout=120)
            if rc == 0 and api.check_cred("pc-client").get("ok"):
                return True, "重抓 PC 端成功"
            if i < SCAN_TRIES - 1:
                time.sleep(SCAN_GAP)     # 同一次调用内内存不会变，只能隔一会儿重扫
        why = tail if rc != 0 else "重抓到的那份不顶用"
        log("· PC 端补不上（%s）→ 启动模拟器借 App 续期（约 1 分钟，期间别断电）…" % why)

    rc, tail = _run_child(os.path.join("scripts", "adwatch.py"),
                          ["--renew-token"], timeout=300)
    if rc != 0:
        return False, "续期失败：%s" % (tail or "退出码 %s" % rc)
    return True, "借 App 续期成功"
