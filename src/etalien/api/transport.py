# -*- coding: utf-8 -*-
"""HTTP 通道：发一次请求。

显式不走系统代理：api.et-api.com 在国内，直连实测 0.1 秒；而系统代理一旦开着但
代理软件没运行（本机就出现过），请求会一律 WinError 10061 连接被拒绝，
跟网络好坏无关。
"""
import gzip
import urllib.error
import urllib.parse
import urllib.request

from .creds import load_cred, load_dvc, load_token, x_eta, xeta_of

BASE = "https://api.et-api.com"

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call(path, body=b"", token=None, dvc=None, timeout=20, extra=None, xeta=None):
    """POST 一个接口。

    token/dvc 缺省从 output/ 读；xeta 缺省跟着凭据的来源端走 —— cred.json
    里存了 os/ver，配错会一律回 401。

    返回值是 (HTTP 状态码, 原始响应体)。连不上时状态码为 None，响应体里放异常文本，
    调用方要能区分「None」和「某个 4xx」。
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
        "user-agent": "Dart/3.10 (dart:io)",
        "content-type": "application/x-protobuf",
        "accept": "application/x-protobuf",
        "accept-encoding": "gzip",
    }
    if extra:
        headers.update(extra)
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with _opener.open(req, timeout=timeout) as r:
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
        return None, ("%s: %s" % (type(e).__name__, e)).encode()
