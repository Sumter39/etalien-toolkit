# 外星仔加速器 · PC 端时长守护

> 目标：退出客户端、关机、睡眠、离开电脑时，时长自动停扣。
> 做法：常驻一个守护进程，在特定时刻替你调一次暂停接口。

---

## 一、为什么需要外部干预

### 关掉客户端不会停止计时

```
12:42:10  加速中，客户端在跑
          ↓ 60 秒  →  扣 62 秒
12:43:13  taskkill /F 强杀 etalien.exe
          ↓ 150 秒 →  扣 154 秒   ← 客户端已经死透，照扣
```

扣费在服务端跑，与本地进程无关。

### 免费账号没有自动暂停

官方 wiki 原话：「时长需要手动暂停，退出加速器前请务必【手动暂停计时】」。

代码里跟 VIP 绑死的那套（`pcVipState` / `vip_time_pause` /「时长保护」）才是自动的，
免费账号走的是手动路径。

---

## 二、接口是怎么挖出来的

没装代理，也没改系统。token 明文躺在客户端进程内存里，直接读。

| 步骤 | 做法 | 收获 |
|---|---|---|
| 静态分析 | 翻 `data/app.so`（12 MB Dart AOT 快照） | 路由表、域名、协议类型 |
| 内存扫描 | 提权读 `etalien.exe` 内存，捞 HTTP/2 报文 | 鉴权头、设备 ID、完整请求格式 |

挖到的东西：

```
鉴权：authorization: <裸 token>          ← 不是 JWT，就是一段字符串
描述：x-eta: os=2&ver=1.24.11&dvc=<设备ID>&ch=default   ← 静态串，不是动态签名
协议：HTTPS + protobuf（不是 JSON），全部 POST
签名：sig 参数非强制，不带照样 200
```

接口清单：

| 接口 | 用途 |
|---|---|
| `/v2/account/remain/duration` | 读剩余时长 |
| `/v2/account/update/pause/state` | 上报暂停状态 |
| `/v2/account/pc/heartbeat` | 客户端心跳 |
| `ws://msg.et-api.com:8848/ws` | 常驻长连接（明文 WebSocket） |
| `https://system.et-api.com` | 另一套网关，带 `sig=&nonce=&ts=&ver=2023-08-28` HMAC 签名 |

### 响应报文的统一结构

所有接口（含错误）共用同一个 protobuf 骨架：

```
field1 = HTTP 状态码    field2 = 状态短语(Unauthorized)
field3 = 原因文本        field4 = 服务器时间戳
```

所以读 `field1` 和读 HTTP 状态码等价。

---

## 三、暂停接口的语义

请求体是 protobuf，就一个字段 `field1`。

`1` = 已暂停，`0` = 加速中。客户端上报的是状态，不是开关。

| 传值 | 结果 |
|---|---|
| `field1=1` | HTTP 200，已暂停 |
| `field1=0` | HTTP 200，加速中 |
| 重复传当前值 | HTTP 500 `can not update same pause state` |

那个 500 不是错误，反而可以用来探测当前状态。重复调用是无害的，
所以守护进程不必先查状态，直接发就行。

实测：

```
POST /v2/account/update/pause/state   field1=1  →  HTTP 200
暂停后采样 75 秒 → 时长 0 变化  ✅
（此时客户端进程已经杀掉，纯服务端状态）
```

---

## 四、覆盖的四个场景

`scripts/watchdog.py` 常驻后台，隐藏窗口，无界面。

| 场景 | 怎么感知 | 实测响应 |
|---|---|---|
| 关机 / 重启 / 注销 | `WM_QUERYENDSESSION` | 117 ms |
| 睡眠 / 休眠 / 合盖 | `WM_POWERBROADCAST` | 79 ms |
| 退出客户端 | 轮询进程表 | 141 ms |
| 锁屏 / 键鼠空闲超阈值 | `LogonUI.exe` + `GetLastInputInfo` | 126 ms |

关机只给 5 秒窗口，这里都在一百多毫秒，余量充足。

**不需要管理员权限**：暂停是纯服务端状态，本进程只发一个 HTTPS 请求，
不碰客户端进程，不触发 UIPI。

**要建一个隐藏的真窗口**：`WM_POWERBROADCAST` 只广播给顶层窗口，
message-only 窗口收不到。窗口保持隐藏，不进任务栏、不进 Alt+Tab。

---

## 五、两个防误伤设计

1. **全屏应用豁免**：前台有铺满整屏的窗口时不判空闲。用手柄打游戏不产生键鼠输入，
   光看空闲秒数会误杀。最大化的普通窗口高度会矮一截（让出任务栏），不会被误判。
2. **保守的进程检测**：查询失败时按「客户端还在运行」处理，宁可漏触发，不可误触发。

---

## 六、安装与使用

```bash
cd <项目目录>
export PYTHONIOENCODING=utf-8

PY=python                              # 或你的虚拟环境解释器

$PY scripts/watchdog.py --install      # 装开机自启（写注册表 Run 键）
$PY scripts/watchdog.py                # 前台跑（Ctrl+C 停）
$PY scripts/watchdog.py --status       # 看状态 + 剩余时长
$PY scripts/watchdog.py --pause-now    # 手动暂停一次
$PY scripts/watchdog.py --stop         # 停掉守护
$PY scripts/watchdog.py --idle-min 30  # 改空闲阈值（默认 15 分钟）
$PY scripts/watchdog.py --uninstall    # 移除自启

$PY scripts/etapi.py duration          # 查剩余时长（不用开客户端、不用开模拟器）
```

### token 失效了怎么办

```bash
$PY scripts/etapi.py scan
```

一条命令，不需要手动开管理员 CMD。它会：

1. 发现自己不是管理员，自动提权重新拉起自己
2. 提权子进程读 `etalien.exe` 内存，用正则捞 `Authorization:` 头
3. 结果写临时文件回传给父进程，由父进程落盘
4. 顺手校验一次接口，并对比新旧 token 是否变化

实测输出：

```
当前不是管理员，自动提权抓取…（若弹出 UAC 提示请点「是」）
扫描 281.3 MB / 0.4s（提权）
token : ************************…          ← 只回显前 40 位
dvc   : **********************             ← 设备 ID
ver   : 1.24.11
状态  : 与上次相同（token 没变）
校验  : HTTP 200 OK（剩余时长已取到）
```

前置条件是客户端在跑且已登录；没跑会直接提示。

> 实现要点：普通进程 `OpenProcess` 会被内核直接拒绝（客户端是 `requireAdministrator`），
> 提权走 `ShellExecuteW(None,"runas",...)`；子进程用 `pythonw.exe` 拉起，不闪黑框，
> 因此子进程里不能 print，只能写文件。

### 自启项

```
HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\Run\EtalienTimeGuard
```

任务管理器 →「启动」标签里能看到、能禁用。零权限，不经过 shell。

### 凭据

`output/token.txt`、`output/device.txt`、`config.json` 三者都在 `.gitignore` 里，
仓库不含任何真实凭据，代码里也没有兜底默认值。守护进程启动时自检一次，失效会写进日志。

---

## 七、踩坑

| 坑 | 处理 |
|---|---|
| 普通权限 `OpenProcess` 返回 0，`err=5` | 客户端是 `requireAdministrator`。`etapi.py scan` 会自动提权重抓 |
| 提权子进程 print 报错 | 子进程用 `pythonw.exe` 拉起，没有 stdout。抓取逻辑一律不 print，只写文件 |
| `ShellExecuteW runas` 的引号地狱 | 直接传 `"<脚本路径>" scan --elevated --result "<结果路径>"`，别用 `-c` 塞代码 |
| `field1` 语义搞反 | `1` 是暂停、`0` 是加速，别按开关的直觉写 |
| 重复上报返回 500 | 正常，代表状态没变，可放心幂等调用 |
| `WM_POWERBROADCAST` 收不到 | 必须是真顶层窗口，message-only 窗口不接收广播 |
| Flutter 界面抓不到控件 | `PrintWindow` 对 GPU 渲染无效，截图要用 `ImageGrab` + 窗口置顶 |
| 启动文件夹的 `.cmd` 静默失效 | 路径被写成正斜杠，`start` 不认。改用注册表 Run 键 |
| 内存里的中文串挖不出来 | Dart AOT 做了处理，只能从英文侧（类名、路径）找线索 |

---

## 八、token 会过期吗

**会，但过期时刻只有服务端知道，客户端自己也不知道。**

### 证据链

| # | 观察 | 说明 |
|---|---|---|
| 1 | token 是 134 字符 base64url，解码后 100 字节密文 | 不是 JWT（无 `.` 分段），本地解析不出 `exp` |
| 2 | 篡改任意一位 → `401 cipher: message authentication failed` | 带 MAC 的自包含凭据（AES-GCM 一类），不是随机 session id |
| 3 | 格式错 → `401 invalid auth token` | 服务端先解密再校验，两类失败分得清 |
| 4 | app.so 里有 `get:tokenExpired` getter | 客户端确实有「过期」这个状态位 |
| 5 | 路由表里没有任何 refresh / renew 接口 | 只有 `/v2/account/login`，过期后不能静默续期 |
| 6 | 提权读内存，全局搜 `expireTime` / `expiresDate` / `tokenExpired` | 命中的全是 Dart 类名字符串池、证书 Pin Rules、WebView Cookie，没有任何一处存着过期时间 |
| 7 | 内存里 token 明文只出现在 3 个地方 | 全是 HTTP/2 请求头缓存（`Authorization: <token>`），旁边没有时间字段 |
| 8 | 本地 Hive 库（`user_login_info.hive`）是 `HiveAesCipher` 加密 | 密钥运行时才取（`fetchEncryptKey`），本地读不出 |

第 4、6 条合起来是关键：客户端手里只有「过期了没有」这个布尔值，没有「什么时候过期」。
它是被动的，撞上 401 才知道。这个信息在客户端侧根本不存在。

### 实测存活时长

```
登录态落盘（user_login_info.hive 写入）
   │
   ├─ 11 小时 23 分后   客户端重启，该文件未被改写 ⇒ 复用了同一个 token
   │
   └─ 再 47 分后        用这把 token 请求 duration → HTTP 200
                        ⇒ 至少存活 12 小时 10 分，且跨过一次客户端重启
```

**不是会话级 token**，重启不换新，至少按「天」计。

### 过期的表现

```
HTTP 401   field1=401  field2=Unauthorized
           field3=invalid auth token   （或 cipher: message authentication failed）
```

### 对守护进程的影响

`watchdog.pause()` 只区分 `200` 和 `500`，401 会落到兜底分支，结果是：

- 日志里只有一行 `✗ 暂停失败 HTTP 401`，看不出是 token 失效
- 没有任何告警，可能几天后才发现时长一直在被扣

自愈的原料已经就绪：`etapi.py scan` 会自动提权重抓，0.4 秒完成。

> 待办（尚未实施）：给 `pause()` 加 401 分支，标记 `token-expired`、
> 写醒目日志，并自动调 `scan` 重抓后重试一次。

---

## 九、其它风险

- 不要手动改 `resolution_mode=custom`（模拟器那边的事，与本文无关，一并提醒）。
- 多设备登录可能顶号。服务端状态是账号级的，本方案不涉及并发登录，风险低。
- 接口随时可能改，改了就重新挖一次，方法在第二节。
