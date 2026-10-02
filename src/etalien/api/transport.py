# -*- coding: utf-8 -*-
"""HTTP 通道：发一次请求，以及为关机路径做的 DNS 准备。

显式不走系统代理：api.et-api.com 在国内，直连实测 0.1 秒；而系统代理一旦开着但
代理软件没运行（本机就出现过），请求会一律 WinError 10061 连接被拒绝，
跟网络好坏无关。关机路径更不该多这么一跳。
"""
import gzip
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request

from .creds import load_cred, load_dvc, load_token, x_eta, xeta_of

BASE = "https://api.et-api.com"

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 预热好的主机名 -> [(family, type, proto, canonname, sockaddr), ...]。
# 关机路径里 getaddrinfo 是唯一不受 socket timeout 控制的环节（它是阻塞的系统调用，
# timeout 只覆盖 connect/send/recv），而 Windows 关机时会先拆网络组件，解析可能长挂。
# 命中缓存就完全跳过解析 —— 见 prewarm()。
_DNS_CACHE = {}


def prewarm(host=None):
    """提前把 BASE 的主机名解析好存进缓存。

    guard 启动时调一次：那时网络组件还完整，解析必定又快又成。之后哪怕关机时
    DNS 已经不可用，call() 也能从缓存里直接拿到地址，不必在只剩几秒的回调里
    去撞一个可能永远不返回的系统调用。
    """
    host = host or urllib.parse.urlsplit(BASE).hostname
    try:
        _DNS_CACHE[host] = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        return True
    except Exception:
        return False


def install_dns_timeout(timeout=2.0):
    """给 DNS 解析套一层墙钟超时 —— urlopen(timeout=) 管不到它。

    getaddrinfo() 是阻塞系统调用，socket 的 timeout 只覆盖 connect/send/recv。
    Windows 关机时先拆网络组件、再通知程序，此时解析可能长时间不返回：
    实测把解析卡住 8 秒，一个设了 2 秒超时的请求就真跑了 8.17 秒。而关机回调里
    系统只给几秒（判「未响应」是 5 秒），等不起 —— 超时就抛，让上层快速失败，
    转去走「落盘欠账 + 开机补发」那条路。

    用子线程卡表是因为阻塞中的 getaddrinfo 没法取消；超时后那个线程只能放着
    （daemon，随进程一起走）。只在长驻进程里装一次，进程退出即失效。
    """
    orig = socket.getaddrinfo
    if getattr(orig, "_eta_guarded", False):
        return

    def guarded(host, port, *a, **k):
        cached = _DNS_CACHE.get(host)
        if cached:
            return cached
        box = {}

        def work():
            try:
                box["r"] = orig(host, port, *a, **k)
            except BaseException as e:
                box["e"] = e

        t = threading.Thread(target=work, daemon=True)
        t.start()
        td = getattr(guarded, "_eta_timeout", timeout)
        t.join(td)
        if t.is_alive():
            raise socket.gaierror("DNS 解析超时（%.1fs，%s）" % (td, host))
        if "e" in box:
            raise box["e"]
        return box["r"]

    guarded._eta_guarded = True
    guarded._eta_timeout = timeout
    socket.getaddrinfo = guarded


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
