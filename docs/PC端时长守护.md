# 外星仔加速器 · PC 端时长守护

> 目标：退出客户端、关机、睡眠、离开电脑时，时长自动停扣。
> 做法：常驻一个守护进程，在特定时刻替你调一次暂停接口。

---

## 一、计时规则

计时在服务端跑，与本地进程无关：

```
12:42:10  加速中，客户端在跑
          ↓ 60 秒  →  扣 62 秒
12:43:13  taskkill /F 强杀 etalien.exe
          ↓ 150 秒 →  扣 154 秒   ← 客户端已经退出，照扣
```

免费账号的时长需要手动暂停（官方 wiki：「退出加速器前请务必【手动暂停计时】」）；
自动暂停那套（`pcVipState` / `vip_time_pause` /「时长保护」）与 VIP 绑定。

---

## 二、接口是怎么挖出来的

token 明文躺在客户端进程内存里，直接读。

| 步骤 | 做法 | 收获 |
|---|---|---|
| 静态分析 | 翻 `data/app.so`（12 MB Dart AOT 快照） | 路由表、域名、协议类型 |
| 内存扫描 | 提权读 `etalien.exe` 内存，捞 HTTP/2 报文 | 鉴权头、设备 ID、完整请求格式 |

挖到的东西：

```
鉴权：authorization: <裸 token>          ← 直接是一段字符串
描述：x-eta: os=2&ver=1.24.11&dvc=<设备ID>&ch=default   ← 静态串
协议：HTTPS + protobuf，全部 POST
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

返回 500 代表状态没变，可以直接用它探测当前状态。重复调用是无害的，
所以守护进程直接发就行，不必先查状态。

实测：

```
POST /v2/account/update/pause/state   field1=1  →  HTTP 200
暂停后采样 75 秒 → 时长 0 变化  ✅
（此时客户端进程已经杀掉，纯服务端状态）
```

---

## 四、覆盖的四个场景

`src/scripts/guard.py` 常驻后台，隐藏窗口，无界面。

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

## 五、两条保守的判定规则

1. **全屏应用豁免**：前台有铺满整屏的窗口时不判空闲（手柄打游戏不产生键鼠输入，只看
   空闲秒数不准）。最大化的普通窗口高度会矮一截（让出任务栏），不受影响。
2. **进程检测**：查询失败时按「客户端还在运行」处理。

---

## 六、安装与使用

```bash
cd <项目目录>
export PYTHONIOENCODING=utf-8

PY=python                              # 或你的虚拟环境解释器

$PY src/scripts/guard.py --install      # 装开机自启（写注册表 Run 键）
$PY src/scripts/guard.py                # 前台跑（Ctrl+C 停）
$PY src/scripts/guard.py --status       # 看状态 + 剩余时长
$PY src/scripts/guard.py --pause-now    # 手动暂停一次
$PY src/scripts/guard.py --stop         # 停掉守护
$PY src/scripts/guard.py --idle-min 30  # 改空闲阈值（默认 15 分钟）
$PY src/scripts/guard.py --uninstall    # 移除自启

$PY src/tools/etapi.py duration          # 查剩余时长（直接用 cred.json 里的 token 查）
```

### 凭据失效时怎么补

两条路，**先试第一条**（`guard` 自动补凭据时也是这个顺序）：

**① 提权重抓 PC 客户端的 token**（约 1 秒）：

```bash
$PY src/tools/etapi.py scan
```

一条命令搞定，提权自动完成。它会：

1. 发现自己不是管理员，自动提权重新拉起自己
2. 提权子进程读 `etalien.exe` 内存，用**大小写不敏感**的正则捞 `authorization:` 头
3. 结果写临时文件回传给父进程，由父进程落盘
4. 顺手校验一次接口，并对比新旧 token 是否变化

**② 第 ① 条走不通时**（客户端没开，或内存里没有现成的 token），
启模拟器借 App 的手续一份 —— App 带 `refresh/token` 逻辑，脚本跑完顺手把新 token
存进 `output/cred.json`：

```bash
$PY src/scripts/adwatch.py --renew-token
```

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

前置条件是客户端在跑**且登录态还在**；没跑会直接提示。客户端的续期能力见第八节 ——
它换不出新 token，只能重新登录，所以这条路走不通时转到第 ② 步。

> 实现要点：客户端是 `requireAdministrator`，读它的内存要先提权
> （`ShellExecuteW(None,"runas",...)`）；子进程用 `pythonw.exe` 拉起，不闪黑框，
> 子进程没有 stdout，所以抓取逻辑一律写文件。

### 自启项

```
HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\Run\EtalienTimeGuard
```

任务管理器 →「启动」标签里能看到、能禁用。零权限，不经过 shell。

### 凭据

`output/cred.json`（token + 来源端 os/ver）、`output/device.txt` 与 `config.json`
都在 `.gitignore` 里，仓库不含任何真实凭据，因机器而异的值一律从 `config.json` 现读。
守护进程启动时自检一次，之后每 30 分钟复检，状态变化才写日志。

---

## 七、实现要点

| 事项 | 做法 |
|---|---|
| 读客户端内存要提权 | 客户端是 `requireAdministrator`。`etapi.py scan` 自动提权重抓 |
| 提权子进程没有 stdout | 子进程用 `pythonw.exe` 拉起。抓取逻辑一律写文件，不 print |
| `ShellExecuteW runas` 的参数传递 | 直接把脚本路径和参数当成命令行串传进去：`"<脚本路径>" scan --elevated --result "<结果路径>"` |
| `field1` 的取值 | `1` 是暂停、`0` 是加速（它是状态，不是开关） |
| 重复上报返回 500 | 代表状态没变，幂等调用即可 |
| `WM_POWERBROADCAST` 的接收条件 | 必须是真顶层窗口，message-only 窗口收不到广播 |
| Flutter 界面抓控件 | `PrintWindow` 对 GPU 渲染无效，截图用 `ImageGrab` + 窗口置顶 |
| 自启位置 | 注册表 Run 键：`pythonw.exe` + 脚本路径，登录即触发 |
| 内存里的中文串 | Dart AOT 做了处理，从英文侧（类名、路径）找线索 |

---

## 八、token 的生命周期

**token 会过期，过期时刻由服务端掌握。**

### 证据链

| # | 观察 | 说明 |
|---|---|---|
| 1 | token 是 134 字符 base64url，解码后 100 字节密文 | 不是 JWT（无 `.` 分段），本地解析不出 `exp` |
| 2 | 篡改任意一位 → `401 cipher: message authentication failed` | 带 MAC 的自包含凭据（AES-GCM 一类），不是随机 session id |
| 3 | 格式错 → `401 invalid auth token` | 服务端先解密再校验，两类失败分得清 |
| 4 | app.so 里有 `get:tokenExpired` getter | 客户端确实有「过期」这个状态位 |
| 5 | 有 `/v2/account/refresh/token`，但要 `sig` 签名 | 缺签名回 `403 invalid sign, check the "sig" parameter`；算法在客户端里 ⇒ 续期由客户端和 App 完成，脚本只做搬运 |
| 6 | 提权读内存，全局搜 `expireTime` / `expiresDate` / `tokenExpired` | 命中的全是 Dart 类名字符串池、证书 Pin Rules、WebView Cookie，没有任何一处存着过期时间 |
| 7 | 内存里 token 明文只出现在请求头缓存里 | 全是 HTTP 请求头缓存，旁边没有时间字段。实测同一时刻 5 处：`authorization:` 2 处 + `Authorization:` 3 处 —— 大小写两种都有，所以搜它必须 `re.I` |
| 8 | 本地 Hive 库（`user_login_info.hive`）是 `HiveAesCipher` 加密 | 密钥运行时才取（`fetchEncryptKey`），本地读不出 |

第 4、6 条合起来说明：客户端手里只有「过期了没有」这个布尔值，没有「什么时候过期」——
撞上 401 才知道，所以续期必须由客户端发起。

### 实测存活时长

```
10-01 01:24:35   签发
   │
   ├─ 跨过一次客户端重启，token 没换 ⇒ 不是会话级
   │
   └─ 10-02 00:15  拿同一把请求 duration → HTTP 401 token expired
                   ⇒ 寿命约 22 小时 51 分，按「一天」算
```

### 过期的表现

```
HTTP 401   field1=401  field2=Unauthorized
           field3=token expired      （或 invalid auth token / cipher: message authentication failed）
```

### 续期借客户端的手

**客户端和 App 都有合法的续期逻辑** —— `spUtils.xml` 里的 `LAST_REFRESH_TOKEN_TIME`
就是 App 自己写的。所以让「有续期能力的那一端」产出凭据，脚本只做搬运：

| 端 | 续期方式 | 依据 |
|---|---|---|
| 安卓 App | **按需自动刷新** | 存活期间它自己会改写 `LAST_REFRESH_TOKEN_TIME`；token 还新鲜时冷启动不会动它，说明是「按需」而非「定时」 |
| PC 客户端 | **需重新登录** | 实测：token 过期后重启客户端，2 分钟内内存里始终抓不到 token |

于是：

1. **模拟器端**：每次跑 adwatch 都从 App 现读 token（App 自己会续），读一次就**复写**一次
   `output/cred.json`，**连来源端一起存**（`os=1` + 现读的 `ver`）—— 光有 token
   不知道该配哪组 x-eta，配错一律 401。`ver` 由 `android_ver()` 从
   `dumpsys package` 现读，**不写死**：写死的话 App 一升级就全线 401。
   **不看 token 变没变**：`saved` / `ts` 就是
   「谁最新」的依据，跳过写入会让这份的时间戳停在旧值，选凭据时就轮不到它。
2. **PC 端**：`guard` 读 `cred.json`，**按保存时间从新到旧探活** —— 谁最后被刷新过
   谁最可能还有效。哪份通过用哪份，选中的会被钉住，后续请求按它的 os/ver 拼 x-eta。
3. 撞上 401：模拟器端冷启动 App 逼它续期后再读一次；PC 端换另一份。都不行就打醒目日志。

### 守护进程的自愈策略

- **探活节奏**：启动时 + 每 30 分钟复检一次凭据（`resolve_cred()`），状态变化才写日志
  （可用→不可用打 `⚠ 凭据不可用：…`，恢复时打 `✓ 凭据可用：…`）。复检被节流跳过时
  沿用上次结论，所以 `--status` 看到的始终是最近一次真实探活的结果。
- **补凭据**：自检发现两份都废 → 第一次轮询就立刻补，不干等 30 分钟。顺序是先 PC 后 App：
  **① `etapi.py scan`**（提权读 PC 客户端内存，约 1 秒、**不启模拟器**）—— 客户端没在运行时
  直接跳过这一步；扫不到就隔 5 秒重扫，共 3 次（token 明文只在请求头缓冲区里存活，
  隔一会儿才有机会扫到）。走不通才 **② `adwatch.py --renew-token`**（启模拟器借 App 的手，
  约 1 分钟）。
- **失效判定**：只有 401 才算凭据失效。`check_cred()` 返回三态：`True` 可用 / `False`
  明确失效 / `None` 判断不了（没网、超时、5xx）。判断不了时既不报警也不去续期。
- **就地重试**：`pause()` 撞上 401 会就地换凭据重试，不等周期复检 —— 暂停是关机 / 休眠
  这类关键时刻才触发的，等不起。**关机 / 注销 / 睡眠走 `fast` 路径**：只发一次请求，
  401 也不换凭据，超时收紧到 `fast_timeout`（2 秒），把请求压在 Windows 判「未响应」的
  5 秒阈值以内。
- **状态可见**：两份凭据全过期时打醒目日志，并以 `token-expired` 落进状态文件（`--status`
  可见）；「末次结果」把 `no-token`（没有凭据文件）和 `net-error`（网络 / 连接失败）
  分开显示。

手动恢复两条路：跑一次 `src/tools/etapi.py scan`（客户端在运行、登录态还在时），或跑一次 adwatch
（它会把 App 端的新 token 存下来）。

> 待验证：token 真正到期时，App 冷启动能否换出新 token。这需要等真实的过期时刻
> （伪造的坏 token 只会得到 `invalid auth token`，App 并不知道我们在用它）。

---

## 九、其它风险

- `resolution_mode` 保持 `phone.1`（模拟器那边的事，与本文无关，一并提醒）。
- 多设备登录可能顶号。服务端状态是账号级的，本方案不涉及并发登录，风险低。
- 接口随时可能改，改了就重新挖一次，方法在第二节。
