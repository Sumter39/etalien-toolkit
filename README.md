# etalien-toolkit

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-Windows-0078D6.svg)]()
[![Python](https://img.shields.io/badge/Python-3.8%2B-3776AB.svg)]()

外星仔加速器（ETAlien Booster）的自动化工具，两个部分互不依赖，可以只用其中一个：

- **模拟器端**：在 MuMu 模拟器里自动刷满当天「看广告领时长」的额度。
- **PC 端**：在退出客户端、关机、睡眠、长时间没操作时自动暂停计时。

## 目录

- [免责声明](#免责声明)
- [模拟器端](#模拟器端自动刷广告额度)
- [PC 端](#pc-端退出关机自动暂停)
- [快速开始](#快速开始)
- [目录结构](#目录结构)
- [技术要点](#技术要点)
- [已知限制](#已知限制)
- [License](#license)

## 免责声明

- 仅供个人学习自用。自动化操作可能违反服务条款，用自己账号，后果自负。
- 不用于商业用途。
- 仓库里没有任何真实凭据。token 和设备 ID 要自己跑一遍抓取流程获得。

## 模拟器端：自动刷广告额度

### 原理

发奖判定发生在宿主 App 里：实测广告 Activity 存活够时间（15 秒就够），把宿主主界面
拉回前台就会发奖。流程里没有任何一步需要看广告内容：

```
点「看广告 领时长」→ 等广告 Activity 起来 → 原地停 15 秒 → 拉回主界面 → 查后端时长核对
```

这条规律不依赖具体广告 UI。实测快手、倍孜、穿山甲三家的广告页结构完全不同，
同一套逻辑都能过。发奖由服务端按广告播放记录校验，脚本走的是「真人看广告」这条正常链路。

核对**全走后端接口，不读界面**。界面会滚动、会漏渲染，实测还出现过提示领取失败、
时长却确实增加了的情况，而后端字段是权威的。所以：

| 判什么 | 数据来源 |
| --- | --- |
| 单轮成没成 | `POST /v2/account/pc/ad/config` 的 `watchCnt` 增加（可暂停时长净增作旁证） |
| 今天完没完 | 三档 `watchCnt` 都 ≥ 该档条目数 |

`watchCnt` 是服务端按用户算的今日已完成次数，条目数就是该档的总次数 —— App 自己
算界面上那个 `N/M` 用的就是这两个字段。接口读不到时**不做任何推断**，直接停。

跑完或异常退出都会把 MuMu **彻底关掉**：先关 Android 实例，再关主程序。MuMu 主程序
（多开器）是常驻组件，所以两步都要做。主程序被关掉后，下次运行会自动以管理员身份重新
拉起（`MuMuManager` 靠主程序的 IPC 启动实例）。调试时想留着模拟器，加 `--keep-emulator`。

### 额度

| 档位 | 次数 | 单次收益 |
| --- | --- | --- |
| 阶段一 | 9 次 | 20 分钟 |
| 阶段二 | 3 次 | 30 分钟 |
| 加油包 | 9 次 | 10 分钟 |

满额 21 轮，换到 `9×20 + 3×30 + 9×10 = 360 分钟` 可暂停时长。档位定义直接读自
`/v2/account/pc/ad/config`（条目个数与奖励秒数）。

### 状态与去重

额度 0 点重置，跨天判断很简单。为覆盖「同一天只刷一半」的情况（刷到一半关机、
脚本被打断、广告临时没填充），`output/adwatch_state.json` 记两个维度：最后一次跑的日期，
以及那天是否全部看完。今天跑完就秒退；今天没跑完，下次登录接着补；不是今天则必跑。

- 开跑前先落盘尝试次数，中途断电也算一次，不会无限重试（上限 6 次/天，`--force` 可无视）。
- 是否看完由接口判定：三档 `watchCnt` 都 ≥ 各自条目数才算刷满。任何一档没读出来、
  进度数字不认识、或没满，都不算 —— **读不到不等于已完成**。
- 跨天时旧字段全部作废，不继承。
- 额度确认在 0 点重置。实测某天 `00:18` 起始读到 `阶段一 0/9`，当天 `02:31` 刷满
  `9/9  3/3  9/9`。所以任何时刻看到三档全满，就是当天真刷满了，直接认 ——
  不必再区分「今天刷过了」和「服务端还没清零」。

## PC 端：退出／关机自动暂停

### 计时规则

计时在服务端跑，跟本地进程无关：关掉客户端也照扣（实测强杀 `etalien.exe` 之后仍然 1:1 扣时长）。
按官方说明，免费账号的时长需要手动暂停，自动暂停是 VIP 功能。

守护进程做的就是这件事：在合适的时机替你调一次暂停接口。

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

两条保守的判定规则：

- 前台有铺满整屏的窗口时不判空闲（手柄玩游戏不产生键鼠输入，只看空闲秒数不准）。
- 进程查询失败时按「客户端还在运行」处理。

### 暂停接口

请求体是 protobuf，只有一个字段 `field1`：`1` 表示已暂停，`0` 表示加速中。
它是状态不是开关：重复传当前值会返回 500 `can not update same pause state`，
代表状态没变，可以拿来探测当前状态。因为重复调用无害，守护进程直接发即可，不必先查状态。

## 快速开始

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
`mumu_vmindex`（实例编号，多开器里能看到，通常 `0`）、`serial`（MuMu 新版是
`127.0.0.1:16384`，老版是 `127.0.0.1:7555`）、`oaid`（任意 UUID）、`venv`
（Python 虚拟环境根目录，`guard` 要靠它挂 `pywin32`）。

`config.json` 已被 `.gitignore` 忽略。必填项没填会直接报错并指出缺哪一项。

### 模拟器端

```bash
python src/scripts/adwatch.py --dry-run   # 先只探测界面，确认能找到入口
python src/scripts/adwatch.py             # 正式跑，刷满今天额度
python src/scripts/adwatch.py --state     # 看今天跑到哪、刷完没（不连模拟器）
python src/scripts/adwatch.py --install   # 装开机自启
```

同一天重复触发是安全的：刷满了秒退，没刷满就接着补。

### PC 端

```bash
python src/tools/etapi.py scan          # 从客户端内存抓 token（会自动提权）
python src/tools/etapi.py duration      # 查剩余时长
python src/tools/etapi.py sim           # 模拟器端：读 App token + 今日各档进度
python src/scripts/guard.py             # 前台跑守护进程，Ctrl+C 停
python src/scripts/guard.py --status    # 看运行状态
python src/scripts/guard.py --install   # 装开机自启
```

`scan` 需要提权（客户端 manifest 是 `requireAdministrator`）。UAC 关闭时静默完成，
正常开启会弹一次确认框。

`sim` 是从模拟器侧看数据的入口（要模拟器在跑）：token 直接读 App 的
SharedPreferences，进度读服务端接口，用来核对脚本判定结果。

## 目录结构

```
etalien-toolkit/
├── README.md
├── LICENSE                    # MIT
├── requirements.txt
├── config.example.json        # 配置模板，复制成 config.json 再填
├── .gitignore
├── docs/
│   ├── 方案与实施步骤.md       # 模拟器端：原理、环境、排查
│   ├── PC端时长守护.md         # PC 端：接口逆向、守护进程、token 调查
│   └── 模拟器安装指南.md       # MuMu 安装 + OAID 配置
├── src/
│   ├── etalien/               # 库：两个入口共用
│   │   ├── __init__.py        # ROOT / SRC_DIR / OUT_DIR
│   │   ├── config.py          # 读取 config.json
│   │   ├── logs.py            # 日志
│   │   ├── procs.py           # 子进程封装（隐藏控制台窗口）
│   │   ├── api/               # 接口层：proto / creds / transport / endpoints
│   │   ├── adwatch/           # 模拟器端：emulator / adb / ui / progress / runner
│   │   └── timeguard/         # PC 端守护：window / poller / pauser / service
│   ├── scripts/               # 两个入口
│   │   ├── adwatch.py         # 模拟器端签到
│   │   └── guard.py           # PC 端守护进程
│   ├── tools/                 # 手动工具
│   │   ├── etapi.py           # 接口客户端：scan / duration / sim …
│   │   └── probe.py           # 环境自检 + 界面探测
│   └── test/                  # 离线单元测试
└── output/                    # 运行时生成，已被忽略（含凭据、设备 ID、日志）
```

### 跑测试

```bash
python -m unittest discover -s src/test -t src -v
```

`-t src` 不能省：用例内一律用 `from ..helpers import …` 的相对导入，
需要把 `src` 作为顶层包才能解析。

## 技术要点

**抓 token** 靠自提权后跨进程读内存，token 明文就躺在客户端进程的内存里。子进程用
`pythonw.exe` 拉起，结果写临时 JSON 回传，由父进程落盘。细节见
[docs/PC端时长守护.md](docs/PC端时长守护.md) 第六节。

**两条关键接口约定**

- `x-eta` 里的 `os` / `ver` 要与 token 的来源端配对：PC 端 `os=2`、App 端 `os=1`，
  配错一律回 `401 token expired`（不是权限问题）。`ver` 取客户端**实际版本**：
  App 端由 `android_ver()` 现读 `dumpsys package`，PC 端由 `scan` 从内存抓，
  客户端升级后自动跟上。
- `dvc` 用本机那一个，与 token 的来源端无关：`etapi.py scan` 抓出来落在
  `output/device.txt`，模拟器端复用同一份。服务端会校验它**属于当前账号**，
  填占位值或拿 App 的 `android_id` 都会回 `400 invalid device id`。

**token 会过期，续期借客户端的手。** 它是带 MAC 的自包含密文（不是 JWT，本地解不出 `exp`），
而路由表里的 `/v2/account/refresh/token` 要客户端签名（`sig`）。所以脚本收到 401 时把占用
该 token 的那一端摇醒，让它按自己的规则刷新，再把新凭据读回来
（模拟器端是冷启动 App，见 `etalien.api.read_progress(auto_renew=True)`）。

两端凭据统一存 `output/cred.json`，**连来源端一起存**（`os` / `ver`）—— 光有 token 不知道该配
哪组 `x-eta`。取用时**挑保存时间最新的那份**（`cred_rank()`）—— 谁最后被刷新过，谁最可能还有效：
`pc-client` 由 `etapi.py scan` 刷新，`android-app` 每跑一次 adwatch 都**复写**一遍
（`saved` / `ts` 就是「谁最新」的依据，所以每次都刷新时间戳；App 自己带续期逻辑，活得久些）。
两份 token 的权限是**账号级**的、不分端：实测拿 App 那份
去调 PC 端的只读接口和暂停写接口都能过（写接口回 `500 can not update same`，不是 401），
`os`/`ver` 只是客户端标识，服务端不拿它卡鉴权 —— 所以借 App 的手续来的 token，PC 端照样能用。

两份都失效要补新的时，**先试 `etapi.py scan`**（提权读 PC 客户端内存，
约 1 秒、不启模拟器）—— 扫不到会隔 5 秒重扫，共 3 次（编排在
`timeguard/creds.renew_cred()`，不是 scan 自身的行为）。三次都空才启模拟器借 App 的手
（约 1 分钟），补 App 那份的代价高在这一步。
`pause()` 撞上 401 会**就地换一份重试**，不等半小时的周期复检；两份都不行才以 `token-expired`
落进状态文件。判定失效**只认 401**：探活返回「没网 / 超时 / 5xx」算「判断不了」，不报警也不去续期。
关机 / 注销 / 睡眠走 `fast` 路径，只发一次、不换凭据，把请求压在系统等待阈值以内。
PC 客户端不提供自动续期，token 过期后需要重新登录；模拟器端 App 自带续期逻辑，所以补凭据走 App。

实测寿命约 23 小时（10-01 01:24:35 签发 → 10-02 00:15 失效），跨过一次客户端重启。
失效表现是 `HTTP 401` + `token expired` / `invalid auth token`。

**自启写注册表 Run 键**（`HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\Run`）。零权限、
不经过 shell、链路最短，登录即触发，任务管理器「启动」标签里能看到、能禁用。
装的是**基础解释器**下的 `pythonw.exe`（不是 venv 里的转发脚本）—— venv 的 `pythonw.exe`
会为子进程建一个随转发器退出的作业对象，关机时转发器先被结束，子进程会被连带杀掉，
日志一个字节都写不出。

**关机路径的三层兜底。** 关机回调只给几秒，请求可能来不及发出，而服务端照扣时长。所以：
① 发请求前先把这次暂停记进 `output/pending_pause.json`，发成功才删；
② 开机时读这个文件补发；
③ 轮询里按 5 秒起、每次翻倍的间隔重试，最长 5 分钟，没成功就不算完成。

## 已知限制

1. PC 端接口是从客户端内存里逆出来的，官方一改就失效，需要重新挖（方法见 `docs/PC端时长守护.md` 第二节）。
2. 模拟器端依赖「广告停留够时间就发奖」这条规律。它不依赖具体 UI，抗改版能力较强，但发奖规则若变仍需重新实测。
3. 锁屏和关机场景的实机验证还不完整（开发环境里没法真的锁屏）。
4. 多设备登录可能顶号。服务端状态是账号级的，本方案不涉及并发登录，风险低。
5. 自动化操作可能被风控。用自己账号，自行判断。

## License

[MIT](LICENSE)
