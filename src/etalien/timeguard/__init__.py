# -*- coding: utf-8 -*-
"""时长守护 —— 在你离开或系统要停的时候，替你调一次暂停接口。

命中以下任一情况时，向服务端上报一次暂停（POST /v2/account/update/pause/state，
field1=1）—— 这些场景下服务端仍在扣时长：

    ① 关机 / 重启 / 注销    WM_QUERYENDSESSION
    ② 睡眠 / 休眠 / 合盖    WM_POWERBROADCAST(PBT_APMSUSPEND)
    ③ 客户端进程退出        轮询 etalien.exe，检测「在 → 不在」
    ④ 锁屏 / 键鼠空闲超时

触发路径只有两条：①② 走窗口消息循环（主线程），③④ 走 poll_loop（子线程）。
关机 / 睡眠只认窗口消息这一条。

窗口：WM_POWERBROADCAST 只广播给顶层窗口，message-only 窗口收不到，所以必须建一个
真正的顶层窗口，但保持隐藏（不 Show），不进任务栏也不进 Alt+Tab。
"""
