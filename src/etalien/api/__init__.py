# -*- coding: utf-8 -*-
"""接口层门面。

调用方 import 这里就够了，不必关心内部拆成了几个文件：
    proto       编解码
    creds       凭据读写
    transport   发请求 + DNS 准备
    endpoints   高层只读接口
    android     模拟器端读取
    memscan     跨进程读内存抓凭据
"""
from .android import (ANDROID_PKG, TokenExpired, ad_config, read_progress,
                      read_token, read_ver, remain_duration, wake_app)
from .creds import (ANDROID_OS, CRED_FILE, DVC_FILE, PC_OS, PC_VER, cred_rank,
                    load_cred, load_creds, load_dvc, load_token, prefer_cred,
                    save_cred, x_eta, xeta_of)
from .endpoints import check_cred, pause_state
from .memscan import SCAN_RESULT, client_pid, is_admin, scan, scan_elevated, scan_job
from .proto import decode, field, fmt_dur, varint
from .transport import BASE, call, install_dns_timeout, prewarm
