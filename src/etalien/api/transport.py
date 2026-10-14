# -*- coding: utf-8 -*-
"""HTTP 通道：发一次请求，以及为关机路径做的 DNS 准备。

显式不走系统代理：api.et-api.com 在国内，直连实测 0.1 秒；而系统代理一旦开着但
代理软件没运行（本机就出现过），请求会一律 WinError 10061 连接被拒绝，
跟网络好坏无关。关机路径更不该多这么一跳。

关机时 Windows 先把网络组件拆掉，那一刻现场解析能卡十几秒 —— socket 的 timeout 只管
connect/send/recv，管不到 getaddrinfo —— 进程会被判成「阻止关机」直接杀掉。所以解析
必须提前做，而且得真正接进请求路径：urllib 自己不会来查我们的缓存，只能把
socket.getaddrinfo 换掉。
"""
import gzip
import socket
import urllib.error
import urllib.parse
import urllib.request

from .creds import load_cred, load_dvc, load_token, x_eta, xeta_of

BASE = "https://api.et-api.com"

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_REAL_GETADDRINFO = socket.getaddrinfo
_PATCHED = False

# 预热好的 host -> (解析时的参数, getaddrinfo 结果)。参数一并存着，
# 免得把 443 的答案拿去顶别的端口。见 prewarm()。
_DNS_CACHE = {}


def _install_cache():
    """把 socket.getaddrinfo 换成「缓存优先」的版本，只装一次。

    换的是 socket 模块里那个全局名字：urllib 内部走的就是它，只在 call() 里绕是绕
    不开的。缓存没命中时照旧走真实解析，行为不变。
    """
    global _PATCHED
    if _PATCHED:
        return

    def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        hit = _DNS_CACHE.get(host)
        if hit and hit[0] == (port, family, type, proto, flags):
            return hit[1]
        return _REAL_GETADDRINFO(host, port, family, type, proto, flags)

    socket.getaddrinfo = getaddrinfo
    _PATCHED = True


def prewarm(host=None):
    """把 BASE 的主机名解析好存进缓存，返回是否成功。

    守护进程启动时调一次，之后由轮询定期重试 —— 开机自启时网络往往还没就绪，
    第一次必然失败，得有人来补。
    """
    _install_cache()
    host = host or urllib.parse.urlsplit(BASE).hostname
    args = (443, 0, socket.SOCK_STREAM, 0, 0)
    try:
        _DNS_CACHE[host] = (args, _REAL_GETADDRINFO(host, *args))
        return True
    except Exception:
        return False


def dns_ready(host=None):
    """BASE 的主机名是否已缓存过。"""
    host = host or urllib.parse.urlsplit(BASE).hostname
    return bool(_DNS_CACHE.get(host))


def forget_dns(host=None):
    """丢掉该主机的缓存，下次请求重新解析。

    连接层失败时调用：CDN 换过地址的话，留着旧地址会一直连不上。
    """
    host = host or urllib.parse.urlsplit(BASE).hostname
    _DNS_CACHE.pop(host, None)


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
        forget_dns()            # 连接层失败，缓存的地址可能已经不对了
        return None, ("%s: %s" % (type(e).__name__, e)).encode()
