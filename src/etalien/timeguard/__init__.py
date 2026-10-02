# -*- coding: utf-8 -*-
"""时长守护 —— 在你离开或系统要停的时候，替你调一次暂停接口。

命中以下任一情况时，向服务端上报一次暂停（POST /v2/account/update/pause/state，
field1=1）—— 这些场景下服务端仍在扣时长：

    ① 关机 / 重启 / 注销    WM_QUERYENDSESSION
    ② 睡眠 / 休眠 / 合盖    WM_POWERBROADCAST(PBT_APMSUSPEND)
    ③ 客户端进程退出        轮询 etalien.exe，检测「在 → 不在」
    ④ 锁屏 / 键鼠空闲超时

触发路径只有两条：①② 走窗口消息循环（主线程），③④ 走 poll_loop（子线程）。
关机 / 睡眠只认窗口消息这一条 —— 曾经并列过 SM_SHUTTINGDOWN 轮询、WTS 会话通知、
内核电源通知三条，实测全部比 WM_QUERYENDSESSION 晚约 190 毫秒，抢不到任何提前量。

窗口：WM_POWERBROADCAST 只广播给顶层窗口，message-only 窗口收不到，所以必须建一个
真正的顶层窗口，但保持隐藏（不 Show），不进任务栏也不进 Alt+Tab。

一次上报未必发得出去（关机时系统只等几秒，网络也可能不通），所以失败必须留痕：
pause(fast=True) 在发请求之前把欠账写进 output/pending_pause.json，成功才撤；
开机时 startup_check() 读它补发。没成功的还会被 poll_loop 按退避重试。
"""
