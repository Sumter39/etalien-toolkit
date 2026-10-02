# -*- coding: utf-8 -*-
"""界面操作。

界面只用来「找到并点下去」，成没成一律回接口核对（见 progress）。
"""
import re
import time

from .config import CONFIG, log


def find_any(d, texts, timeout=0):
    """按顺序找第一个存在的控件；timeout > 0 时轮询等待。"""
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
    """宿主主界面是否在前台。"""
    try:
        return d.app_current().get("activity", "").endswith(".ui.MainActivity")
    except Exception:
        return False


def punch_home(d, tries=3):
    """万能穿透：不管上层压着什么广告页 / 落地页，直接把宿主主界面拉到前台。

    比「找关闭按钮」稳得多：不依赖任何广告联盟的 UI，且 MainActivity 是
    singleTask，宿主状态完整保留，不会重走启动流程。
    """
    for _ in range(tries):
        try:
            d.shell("am start -n %s" % CONFIG["main_activity"])
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
            log("  点掉弹窗「%s」" % t)
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
    """界面上有没有「未登录」标志。只作提示，判定一律以接口为准。"""
    for t in CONFIG["login_flags"]:
        try:
            if d(textContains=t).exists:
                return False
        except Exception:
            pass
    return True


def dump_ui(d):
    """把当前界面的控件文本打到日志（排查界面改版用）。"""
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
            log("  %s  %-45s  %s" % (g("bounds"), rid[:45], t[:40]))
    log("前台: %s" % (d.app_current(),))
