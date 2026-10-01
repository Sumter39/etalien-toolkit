# etalien-toolkit

外星仔加速器（ETAlien Booster）的自动化工具，两个部分互不依赖，可以只用其中一个：

- `scripts/checkin.py`：在 MuMu 模拟器里自动刷满当天「看广告领时长」的额度。
- `scripts/watchdog.py`：PC 端在退出客户端、关机、睡眠、长时间没操作时自动暂停计时。

## 免责声明

- 仅供个人学习自用。自动化操作可能违反服务条款，用自己账号，后果自负。
- 不用于商业用途。
- 仓库里没有任何真实凭据。token 和设备 ID 要自己跑一遍抓取流程获得。

---

## 一、模拟器端：自动刷广告额度

### 原理

发奖判定不在广告页上，在宿主 App 里。实测广告 Activity 存活够时间（15 秒就够），
把宿主主界面拉回前台就会发奖。所以整个流程不需要识别广告内容，也不用点任何按钮：

```
点「看广告 领时长」→ 等广告 Activity 起来 → 原地停 15 秒 → 拉回主界面 → 查后端时长核对
```

这条规律不依赖具体广告 UI。实测快手、倍孜、穿山甲三家的广告页结构完全不同，
同一套逻辑都能过。

不走协议层调接口的原因：补发接口已被砍掉、认证换成了 token，而且发奖由服务端
按播放记录校验，客户端伪造没有用。

核对一律以后端可暂停时长的增量为准，不看 UI 提示。实测出现过提示领取失败、
时长却确实增加了的情况。

### 额度

| 档位 | 次数 | 单次收益 |
| --- | --- | --- |
| 阶段一 | 9 次 | 20 分钟 |
| 阶段二 | 3 次 | 30 分钟 |
| 加油包 | ≥7 次 | 10 分钟 |

满额约 19 轮，跑完 20 分钟左右，换到约 4 小时可暂停时长。

### 状态与去重

额度 0 点重置，跨天判断很简单。麻烦的是同一天只刷一半：刷到第 12 轮时关机、
脚本被打断、广告临时没填充。只记「今天跑没跑过」的话，剩下的额度就补不回来了。

所以 `output/state.json` 记两个维度：最后一次跑的日期，以及那天是否全部看完。
今天跑完就秒退；今天没跑完，下次登录接着补；不是今天则必跑。

- 开跑前先落盘尝试次数，中途断电也算一次，不会无限重试（上限 6 次/天，`--force` 可无视）。
- 是否看完有两路判定：主按钮文案，或三档进度数据（`已完成` / `N/N`）。实测存在文案没刷新的情况。
- 跨天时旧字段全部作废，不继承。
- 凌晨 0-6 点看到「已看完」不可信，可能是前一天的额度还没刷新，一律按未看完处理。

---

## 二、PC 端：退出／关机自动暂停

### 为什么要外部干预

两件事实测下来都不成立：

- 关掉客户端不会停止计时。强杀 `etalien.exe` 后仍然 1:1 扣时长。扣费在服务端跑，跟本地进程无关。
- 免费账号没有自动暂停。官方 wiki 的原话是「时长需要手动暂停，退出加速器前请务必手动暂停计时」，
  自动暂停是 VIP 功能。

所以只能从外部，在合适的时机替你调一次暂停接口。

### 实现

常驻一个无界面的守护进程，覆盖四种情况：

| 场景 | 感知方式 | 实测响应 |
| --- | --- | --- |
| 关机 / 重启 / 注销 | `WM_QUERYENDSESSION` | 117 ms |
| 睡眠 / 休眠 / 合盖 | `WM_POWERBROADCAST` | 79 ms |
| 退出客户端 | 轮询进程表 | 141 ms |
| 锁屏 / 键鼠空闲超阈值 | `LogonUI.exe` + `GetLastInputInfo` | 126 ms |

关机只给 5 秒窗口，上面这些响应时间够用。守护进程不需要管理员权限，
它只发一个 HTTPS 请求，不碰客户端进程。

两个防误伤的考虑：

- 前台有铺满整屏的窗口时不判空闲。用手柄玩游戏不产生键鼠输入，只看空闲秒数会误判。
- 进程查询失败时按「客户端还在运行」处理，宁可漏触发也不误触发。

### 暂停接口

请求体是 protobuf，只有一个字段 `field1`：`1` 表示已暂停，`0` 表示加速中。
注意它是状态不是开关。重复传当前值会返回 500 `can not update same pause state`，
这不算错误。因为重复调用无害，守护进程不需要先查状态。

---

## 三、快速开始

### 前置

1. Windows 10 / 11 + Python 3.8+
2. MuMu 模拟器，安装与 OAID 配置见 [docs/模拟器安装指南.md](docs/模拟器安装指南.md)
3. 模拟器里装好外星仔并登录
4. PC 端装好外星仔客户端并登录

```bash
pip install -r requirements.txt
cp config.example.json config.json      # Git Bash
copy config.example.json config.json    # CMD
```

然后填 `config.json`：`mumu_manager`（`MuMuManager.exe` 的完整路径）、`adb_path`、
`oaid`（任意 UUID）、`serial`（MuMu 新版是 `127.0.0.1:16384`，老版是 `127.0.0.1:7555`）。

`config.json` 已被 `.gitignore` 忽略。必填项没填会直接报错并指出缺哪一项，
不会悄悄用默认值跑出看起来正常、其实错了的结果。

### 模拟器端

```bash
python scripts/checkin.py --dry-run   # 先只探测界面，确认能找到入口
python scripts/checkin.py             # 正式跑，刷满今天额度
python scripts/checkin.py --state     # 看今天跑到哪、刷完没（不连模拟器）
python scripts/checkin.py --install   # 装开机自启
```

同一天重复触发是安全的：刷满了秒退，没刷满就接着补。

### PC 端

```bash
python scripts/etapi.py scan          # 从客户端内存抓 token（会自动提权）
python scripts/etapi.py duration      # 查剩余时长
python scripts/watchdog.py            # 前台跑守护进程，Ctrl+C 停
python scripts/watchdog.py --status   # 看运行状态
python scripts/watchdog.py --install  # 装开机自启
```

客户端 manifest 是 `requireAdministrator`，普通进程 `OpenProcess` 会被内核直接拒绝，
所以 `scan` 需要提权。UAC 关闭时静默完成，正常开启会弹一次确认框。

---

## 四、目录结构

```
etalien-toolkit/
├── README.md
├── requirements.txt
├── config.example.json        # 配置模板，复制成 config.json 再填
├── .gitignore
├── docs/
│   ├── 方案与实施步骤.md       # 模拟器端：原理、环境、踩坑
│   ├── PC端时长守护.md         # PC 端：接口逆向、守护进程、token 调查
│   └── 模拟器安装指南.md       # MuMu 安装 + OAID 配置
├── scripts/
│   ├── checkin.py             # 模拟器端主脚本
│   ├── watchdog.py            # PC 端守护进程
│   ├── etapi.py               # PC 端 API 客户端
│   ├── probe.py               # 环境自检 + 界面探测
│   └── etconfig.py            # 读取 config.json
└── output/                    # 运行时生成，已被忽略（含 token、设备 ID、日志）
```

---

## 五、其他说明

**抓 token** 靠自提权后跨进程读内存：客户端以管理员权限运行，普通进程 `OpenProcess`
会被内核拒绝，token 明文就躺在它的内存里。子进程用 `pythonw.exe` 拉起，结果写临时
JSON 回传，由父进程落盘。细节见 [docs/PC端时长守护.md](docs/PC端时长守护.md) 第六节。

**token 会过期**，但过期时刻连客户端自己都不知道。它是带 MAC 的自包含密文，不是 JWT，
本地解不出 `exp`；客户端只有 `tokenExpired` 一个布尔值，撞上 401 才知道。路由表里没有
任何 refresh 接口，过期只能重新登录。实测至少存活 12 小时且跨过一次客户端重启。
失效表现是 `HTTP 401` + `field3=invalid auth token`，重抓一条命令就行。

**自启一律写注册表 Run 键**（`HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\Run`）。
不用 `schtasks.exe`（部分环境被黑名单拦）、不用 PowerShell/COM（容易 `E_ACCESSDENIED`）、
不用启动文件夹的 `.cmd`（路径容易写成正斜杠，`start` 不认，自启会静默失效）。

---

## 六、已知限制

1. PC 端接口是从客户端内存里逆出来的，官方一改就失效，需要重新挖（方法见 `docs/PC端时长守护.md` 第二节）。
2. 模拟器端依赖「广告停留够时间就发奖」这条规律。它不依赖具体 UI，抗改版能力较强，但发奖规则若变仍需重新实测。
3. 锁屏和关机场景的实机验证还不完整（开发环境里没法真的锁屏）。
4. 多设备登录可能顶号。服务端状态是账号级的，本方案不涉及并发登录，风险低。
5. 自动化操作可能被风控。用自己账号，自行判断。

---

## 七、License

未指定，默认保留所有权利。可以读、可以自己改着用，不代表可以再分发或商用。
