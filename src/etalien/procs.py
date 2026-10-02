# -*- coding: utf-8 -*-
"""跑外部命令的统一封装。

pythonw.exe 是 GUI 子系统、没有控制台；它起的 adb / MuMuManager / tasklist
这些控制台程序，Windows 会给每个子进程新配一个控制台窗口 —— 一轮下来满屏闪。
CREATE_NO_WINDOW 抑制掉（第三方库 adbutils 内部也是这么做的）。

所有起子进程的地方都走这里，别直接调 subprocess.run —— 漏一处就闪一处。
"""
import subprocess

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run(cmd, **kw):
    """跑外部命令并等它结束，一律不弹控制台窗口。"""
    kw.setdefault("creationflags", _NO_WINDOW)
    return subprocess.run(cmd, **kw)


def capture(cmd, timeout=30):
    """跑命令并收下输出，返回 (退出码, stdout, stderr)；起不来时退出码给 -1。"""
    try:
        r = run(cmd, capture_output=True, timeout=timeout)
        out = (r.stdout or b"").decode("utf-8", "ignore")
        err = (r.stderr or b"").decode("utf-8", "ignore")
        return r.returncode, out.strip(), err.strip()
    except Exception as e:
        return -1, "", str(e)
