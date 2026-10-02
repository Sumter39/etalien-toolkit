# -*- coding: utf-8 -*-
"""进度读取与判定 —— 全部走接口，不读界面。

UI 会滚动、会漏渲染，「读不到」最容易被误当成「已完成」；反过来，界面上的
「已领完」也可能其实还差几次。所以判定只吃接口数据，读不到就不做任何推断。
"""
import time

from .. import api
from .config import log


def fetch(adb, serial, tries=2):
    """读一次进度；失败会重读 App token 再试，仍失败返回最后一次的结果。

    每次都重新从模拟器取 token —— App 自己会调 refresh/token 续期，
    现读的就是最新的一份。
    """
    r = None
    for i in range(tries):
        r = api.read_progress(adb, serial)
        if r["ok"]:
            return r
        log("  ! 接口读取失败：%s" % r["reason"])
        if i + 1 < tries:
            time.sleep(4)
    return r


def fmt(p):
    """打一行，例如：时长 6时8分  [阶段一 9/9  阶段二 0/3  加油包 0/9]"""
    sec = p.get("seconds")
    bal = api.fmt_dur(sec) if sec is not None else "?"
    parts = ["%s %d/%d" % (s["title"], s["done"], s["total"])
             for s in (p.get("stages") or [])]
    return "时长 " + bal + ("  [" + "  ".join(parts) + "]" if parts else "")


def delta(before, after):
    """本轮是否真的进账；返回描述字符串，没进账返回 None。

    主判据是 watchCnt 增加 —— 它是纯计数，不像可暂停时长那样会被加速扣减
    稀释掉。时长净增只当旁证。
    """
    old = {s["title"]: s for s in (before.get("stages") or [])}
    for s in (after.get("stages") or []):
        o = old.get(s["title"])
        if o and s["done"] > o["done"]:
            return "%s %d→%d/%d" % (s["title"], o["done"], s["done"], s["total"])
    bs, as_ = before.get("seconds"), after.get("seconds")
    if bs is not None and as_ is not None and as_ > bs:
        return "时长 +%d 分钟" % int((as_ - bs) // 60)
    return None


def all_done(stages):
    """三档是否都刷满。

    读不到档位（接口没回、返回空）一律不算刷满 ——「看不到」不等于
    「已完成」，以前的误判就是在这栽的。
    """
    if not stages:
        return False
    return all(s["total"] > 0 and s["done"] >= s["total"] for s in stages)
