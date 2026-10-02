# -*- coding: utf-8 -*-
"""高层只读接口：探活与「暂停态」判读。"""
from .creds import load_cred, load_dvc, xeta_of
from .proto import decode
from .transport import call


def check_cred(src=None, timeout=10):
    """探活：拿凭据打一次只读接口。返回 dict：ok / token / os / ver / src / status。

    ok 是三态，调用方必须按三态处理：

        True    200，这份凭据能用
        False   401，这份凭据明确失效（换掉 / 重新登录）
        None    未知 —— 没网、超时、5xx 等。不能当成失效

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


def pause_state(timeout=15):
    """读服务端真实的「暂停 / 加速」状态，返回 (剩余秒数, 是否暂停中)。

    以 field4 为准，不看界面文案 —— 文案会随版本变，余额那行还可能滚出可视区，
    「读不到」最容易被误当成「已完成」。读不到时返回 (None, None)，
    调用方不要据此推断状态，既不算成功也不算失败。

    field4 只在暂停时才出现：proto3 默认值不序列化，实测加速态下这个字段
    压根不存在（不是 0）。判定必须写成 == 1，「缺失」与「值为 0」要分开对待。
    """
    st, raw = call("/v2/account/remain/duration", b"", timeout=timeout)
    if st != 200:
        return None, None
    fs = {f: v for f, k, v in decode(raw) if k == "varint"}
    return fs.get(1), fs.get(4) == 1
