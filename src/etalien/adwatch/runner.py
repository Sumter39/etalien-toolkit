# -*- coding: utf-8 -*-
"""单轮流程与主流程。

一轮 = 点「看广告 领时长」→ 等广告 Activity → 原地停留 → 拉回主界面 → 回接口核对。
发了奖由服务端按播放时长算，脚本不做任何识别。
"""
import argparse
import json
import os
import random
import sys
import time

from .. import config as cfg
from . import emulator, progress, state, ui
from .adb import connect, ensure_ready
from .config import CONFIG, log

RUN_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "EtalienDailyCheckin"


def run_round(d, adb, serial):
    """跑一轮，返回 (状态, 快照)。状态：ok 成功 / done 已刷满 / fail 失败 / error 接口不通。"""
    ui.dismiss_popups(d)

    # 先读进度再点 —— 接口不通就别点，点了也不知道成没成，白扣广告额度
    before = progress.fetch(adb, serial)
    if not before["ok"]:
        return "error", before

    if not ui.ensure_on_ads_page(d):
        if progress.all_done(before["stages"]):
            return "done", before
        log("  ✗ 不在「看广告」页面")
        return "fail", before

    # 1) 点「看广告 领时长」
    el, label = ui.find_any(d, CONFIG["watch_button"], timeout=5)
    if not el:
        log("  ✗ 找不到「看广告」按钮")
        return ("done" if progress.all_done(before["stages"]) else "fail"), before
    try:
        el.click()
    except Exception as e:
        log("  ✗ 点击失败: %s" % e)
        return "fail", before
    log("  已点击「%s」" % label)

    # 2) 等广告 Activity 起来
    t0 = time.time()
    while time.time() - t0 < CONFIG["ad_appear_timeout"]:
        if not ui.is_main(d):
            break
        time.sleep(1)
    else:
        if progress.all_done(before["stages"]):
            return "done", before
        log("  ✗ 广告未出现（无填充 or 额度已用尽）")
        return "fail", before

    try:
        act = d.app_current().get("activity", "")
    except Exception:
        act = "?"
    log("  广告已出现 (%.0fs): %s" % (time.time() - t0, act.split(".")[-1]))

    # 3) 核心：原地停留，不点任何按钮 —— 发奖只看时间够不够
    stay = CONFIG["ad_stay"] + random.uniform(0, CONFIG["ad_stay_jitter"])
    log("  停留 %.0fs（不点任何按钮）…" % stay)
    time.sleep(stay)

    # 4) 万能穿透回主界面
    if not ui.punch_home(d):
        log("  ! 穿透失败，改按返回键")
        for _ in range(4):
            try:
                d.press("back")
            except Exception:
                pass
            time.sleep(1.5)
            ui.dismiss_popups(d)
            if ui.is_main(d):
                break
    time.sleep(2)
    ui.dismiss_popups(d)

    # 5) 校验：全看接口。界面提示不算数（实测有「提示失败、时长确实增加」，
    #    也有「文案说看完了、其实还差几次」）。
    after = progress.fetch(adb, serial)
    if not after["ok"]:
        log("  ✗ 读不到进度，本轮结果未知：%s" % after["reason"])
        return "error", before
    got = progress.delta(before, after)
    if got:
        log("  ✓ 成功  %s   （%s）" % (got, progress.fmt(after)))
        return "ok", after
    if progress.all_done(after["stages"]):
        return "done", after
    log("  ✗ 未生效  %s → %s" % (progress.fmt(before), progress.fmt(after)))
    return "fail", after


def cmd_renew_token():
    """只把凭据续一份新的：启模拟器 → 读 App 的 token 存进 cred.json → 关模拟器。

    给 timeguard 当退路用 —— 它那边补凭据是先 etapi.py scan（提权重抓 PC
    客户端，不启模拟器、约 1 秒），走不通才调这里。因为 PC 端凭据过期后没法
    自愈（客户端自己也不刷新），能续期的只有 App（它带 refresh/token 逻辑）。

    这里只借 App 的手刷新凭据，不刷广告、不动每日状态，所以不受 attempts 上限影响。
    """
    adb = cfg.adb_path()
    try:
        ensure_ready(adb)
    except SystemExit as e:
        log("✗ 续期失败：%s" % e)
        return 1
    r = progress.fetch(adb, cfg.serial(), tries=1)
    try:
        emulator.shutdown()
    except Exception:
        pass
    if r["ok"]:
        log("✓ 凭据已续到最新：%s" % progress.fmt(r))
        return 0
    log("✗ 续期失败：%s" % r["reason"])
    return 1


def cmd_install():
    """写进 HKCU 的 Run 键 → 每次登录自动跑一趟。

    触发多次是安全的：状态文件里记了 date + all_done，刷满了秒退，
    没刷满（中途关机 / 上次失败）就接着补。
    """
    import winreg

    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pyw):
        pyw = sys.executable
    # 登录后等 30 秒再开跑：够 explorer 把自启项都拉起来、网络/磁盘稳定下来，
    # 又不至于把整个流程拖太久。
    cmdline = '"%s" "%s" --delay 30' % (pyw, os.path.abspath(sys.argv[0]))

    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE)
    winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, cmdline)
    winreg.CloseKey(key)

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
    ap.add_argument("--renew-token", action="store_true",
                    help="只续凭据（启模拟器读 App token），不刷广告 —— 供 timeguard 调用")
    args = ap.parse_args()

    if args.renew_token:
        return cmd_renew_token()
    if args.install:
        return cmd_install()
    if args.uninstall:
        return cmd_uninstall()
    if args.state:
        st = state.today()
        print(json.dumps(st, ensure_ascii=False, indent=2))
        print("#%s 已尝试 %d 次，全部看完：%s"
              % (st["date"], st["attempts"], "是" if st["all_done"] else "否"))
        return

    today = state.date_str()

    # 去重（两个维度，缺一不可）：
    #   今天 + 已全部看完   → 跳过
    #   今天 + 没看完       → 继续补跑（电脑中途关机 / 上次失败）
    #   不是今天            → 必跑（额度 0 点重置）
    if not args.dry_run and not args.force:
        st = state.today()
        if st["all_done"]:
            log("今天（%s）额度已全部看完，跳过。要强制重跑请加 --force。" % today)
            return
        if st["attempts"] >= CONFIG["max_attempts_per_day"]:
            log("今天（%s）已尝试 %d 次仍未看完（上次：%s），今日不再重试（--force 可强制）。"
                % (today, st["attempts"], st.get("progress") or "进度未知"))
            return
        if st["attempts"]:
            log("今天（%s）已跑过 %d 次但未全部看完（上次：%s），接着补完剩余额度…"
                % (today, st["attempts"], st.get("progress") or "进度未知"))

    if args.delay > 0 and not args.dry_run:
        log("等待 %d 秒后开始（让系统先稳定下来）…" % args.delay)
        time.sleep(args.delay)

    d, adb = connect()

    if args.dry_run:
        ui.dump_ui(d)
        return

    log("启动 app…")
    d.app_start(CONFIG["package"])
    time.sleep(5)
    ui.dismiss_popups(d)

    done_today = False     # 今天额度是否已全部看完 → 决定以后还要不要补跑
    snapshot = None        # 进度快照，便于知道上次停在哪
    started = False        # 是否「有效尝试」：决定要不要把今天记进状态
    try:
        # 登录状态以接口为准（能读到进度就是已登录）。界面文案只用来补充提示 ——
        # UI 会漏渲染、也会出现同名字样，拿它当判定条件会误退。
        cur = progress.fetch(adb, cfg.serial())
        if not cur["ok"]:
            hint = "界面显示需要登录，请手动登录一次。" if not ui.is_logged_in(d) else ""
            log("✗ 接口读不到进度（%s）%s本轮不写状态。"
                "（想留着模拟器排查，加 --keep-emulator）" % (cur["reason"], hint))
            return

        if progress.all_done(cur["stages"]):
            log("启动时接口已显示三档刷满：%s" % progress.fmt(cur))
            started = True
            done_today = True
            snapshot = progress.fmt(cur)      # 让 --state 能看到停在哪
            return

        if not ui.ensure_on_ads_page(d):
            log("✗ 未找到「看广告 领时长」入口，dump 当前界面。本轮不写状态。")
            ui.dump_ui(d)
            return

        started = True
        state.bump_attempt()     # 立即落盘：中途被关机也算一次尝试，不会被无限重试
        log("起始：%s" % progress.fmt(cur))

        ok = fail = error = 0
        for i in range(1, args.rounds + 1):
            log("--- 第 %d 轮 ---" % i)
            status, snap = "fail", cur
            for attempt in range(1 + CONFIG["retry_per_round"]):
                if attempt:
                    log("  重试 #%d" % attempt)
                    ui.punch_home(d)
                    time.sleep(3)
                status, snap = run_round(d, adb, cfg.serial())
                if status != "fail":
                    break
            if status == "ok":
                ok += 1
                fail = error = 0
            elif status == "done":
                log("接口显示三档已全部看完，收工（%s）" % progress.fmt(snap))
                done_today = True
                break
            elif status == "error":
                # 接口不通时不推断结果：既不算成功也不算失败，重试几次还不行就收工
                error += 1
                if error >= 2:
                    log("接口一直读不到进度，停止（不猜、不误判；已完成的额度不会丢）")
                    break
                time.sleep(5)
                continue
            else:
                fail += 1
                if fail >= CONFIG["max_consecutive_fail"]:
                    log("连续 %d 轮失败，停止（已完成的额度不会丢，下次登录接着补）" % fail)
                    break

            # 每轮都刷一次进度快照：万一接下来被关机，也能看出停在哪
            snapshot = progress.fmt(snap)
            state.save(date=today, progress=snapshot)

            if i < args.rounds:
                gap = random.uniform(*CONFIG["round_gap"])
                log("  间隔 %.0fs（模拟真人节奏）" % gap)
                time.sleep(gap)

        final = progress.fetch(adb, cfg.serial())
        snapshot = progress.fmt(final)
        if not done_today and final["ok"]:
            done_today = progress.all_done(final["stages"])
        log("===== 结束：成功 %d 轮，失败 %d 轮，%s，%s ====="
            % (ok, fail, snapshot, "已全部看完" if done_today else "未看完"))
    finally:
        if started:
            state.save(date=today, all_done=done_today, progress=snapshot)
        # 退出即彻底关掉模拟器（用户明确要求）。要留着现场排查/手动登录，加 --keep-emulator。
        if not args.keep_emulator:
            emulator.shutdown()
