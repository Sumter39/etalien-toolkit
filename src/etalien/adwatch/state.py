# -*- coding: utf-8 -*-
"""每日状态（记住 last run day + 是否「全部看完」）。

为什么必须记 all_done：额度虽然是 0 点重置的，但同一天内也可能只刷了一半
（电脑关机 / 脚本被打断 / 广告无填充）。只按「今天跑过没有」去重，这半天的
额度就白丢了 —— 而只按「没刷满就一直跑」，又会在广告无填充的日子里反复
拉起模拟器。所以用 attempts 封顶。
"""
import json
import os
import time

from .config import CONFIG, log


def load():
    """读状态文件，返回 dict；不存在/为空/损坏 → {}。"""
    try:
        with open(CONFIG["state_file"], encoding="utf-8") as f:
            raw = f.read().strip()
    except Exception:
        return {}
    if not raw:
        return {}
    try:
        st = json.loads(raw)
        return st if isinstance(st, dict) else {}
    except Exception:
        return {}


def save(**kw):
    """合并写入状态文件（未指定的字段保持原值）。

    注意：一旦传入的 date 和文件里的日期不同，说明跨天了 —— 旧记录的所有字段
    （all_done / attempts / progress）立即作废，绝不继承。否则「昨天已看完」
    会顺着 merge 漏进今天，把当天的额度直接吞掉。
    """
    st = load()
    if kw.get("date") and st.get("date") and kw["date"] != st["date"]:
        st = {}
    st.update(kw)
    st["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        os.makedirs(os.path.dirname(CONFIG["state_file"]), exist_ok=True)
        with open(CONFIG["state_file"], "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log("  ! 写入状态文件失败: %s" % e)
    return st


def date_str():
    return time.strftime("%Y-%m-%d")


def today():
    """取「今天」的状态；文件里是别的日期（昨天）→ 当作全新的一天，不继承。"""
    st = load()
    if st.get("date") != date_str():
        return {"date": date_str(), "all_done": False, "attempts": 0, "progress": None}
    st.setdefault("all_done", False)
    st.setdefault("attempts", 0)
    return st


def bump_attempt():
    """当天尝试次数 +1，立即落盘 —— 中途断电也照样算数，不会被无限重试。"""
    return save(date=date_str(), attempts=int(today().get("attempts", 0)) + 1)
