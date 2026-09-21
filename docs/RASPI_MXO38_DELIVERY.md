# 树莓派 + R&S MXO38 接入交付文档

| 项 | 内容 |
|---|---|
| 版本 | v1.2 |
| 日期 | 2026-09-20 |
| 交付内容 | 示波器 USB 接入树莓派 → 树莓派经 WiFi 向 PC 提供：① 采集与数据落盘（主用途，仪器独占） ② **按需**开启的网页实时看波形 |
| 文档位置 | `F:\ZYS_HYBASE\automation\RASPI_MXO38_DELIVERY.md`（树莓派副本：`/root/RASPI_MXO38_DELIVERY.md`） |

---

## 1. 一句话说明

示波器用 USB 线挂在树莓派上，树莓派通过 WiFi 连到 PC 所在的网络。**PC 上一条命令，就能让树莓派去操作示波器采集波形，采到的数据直接落进 PC 自己的硬盘**；**PC 浏览器再打开一个网址，就能实时看到示波器的屏幕和测量值**。都不需要在 PC 上装任何驱动、也不需要把示波器搬到 PC 旁边。

---

## 2. 系统架构

```
   R&S MXO38  ──USB / USBTMC──►  树莓派 (Raspberry Pi 4B)  ──WiFi (UESTC)──►  work-pc
                                        │  ▲                                     │
                                        │  └──── HTTP :8080（实时画面/波形）───────┘
                                        │                                        │
                                        └────────  SMB 网络盘挂载  ──────────────┘
                                        /mnt/scope_results  ←→  F:\ZYS_HYBASE\automation\scope_results
```

| 角色 | 硬件/系统 | 地址与关键参数 |
|---|---|---|
| 示波器 | R&S MXO38，固件 2.8.2.0，SN `1335.2050k08/101400`，选件 B282/B285 | USB 设备 `0aad:0197`，USBTMC 节点 `/dev/usbtmc0` |
| 树莓派 | Raspberry Pi 4B Rev 1.5，Raspbian 12 (bookworm)，armhf 用户态 + 6.12 内核 | wlan0 静态 **172.16.102.132/22**（网关 172.16.100.1）；eth0 静态 192.168.2.201/24（当前闲置，无链路） |
| PC | work-pc（Windows），用户 `remote` | 172.16.102.207 |

**数据通道共四条**：

1. **控制通道**：PC ──SSH──► 树莓派 ──USB(USBTMC)──► 示波器（SSH 密钥已配好，免密）
2. **数据通道**：树莓派 ──SMB──► `F:\ZYS_HYBASE\automation\scope_results`（已挂载到 `/mnt/scope_results`）
3. **反向推文件**：PC ──scp──► 树莓派（`work-pc-to-pi` 密钥已授权）
4. **实时画面通道**：PC 浏览器 ──HTTP 8080──► 树莓派（网页镜像示波器屏幕、推实时数据波形）

---

## 3. 现在能实现哪些功能（能力清单）

| # | 能力 | 入口 | 状态 |
|---|---|---|---|
| 1 | 与示波器建立 VISA 会话，收发任意 SCPI 命令 | `visa_query.py` | ✅ 已验证 |
| 2 | 枚举 VISA 资源（自动找到 USB 上的仪器） | `visa_probe.py` | ✅ 已验证 |
| 3 | 读取 `*IDN?` / `*OPT?` / 错误队列等身份与状态 | `visa_query.py` | ✅ 已验证 |
| 4 | **单次波形采集**（任意点数、CSV 带正确时间轴） | `scope_capture_usb.py` / `scope-capture` | ✅ 已验证 |
| 5 | 采集数据**直接写入 PC 硬盘** | 挂载点 `/mnt/scope_results` | ✅ 已验证（MD5 双向一致） |
| 6 | 从 PC 往树莓派推文件/脚本 | `scp`（免密） | ✅ 已验证 |
| 7 | 查询示波器全部 SCPI 命令（3256 条） | `/root/scpi_headers.txt` | ✅ 已缓存 |
| 8 | 采集前后**自动还原示波器原有设置**，不污染现场状态 | 采集脚本内置 | ✅ 已验证 |
| 9 | 采集完数据在 PC 侧直接可见/可分析 | Windows 资源管理器 / Python / MATLAB | ✅ 已验证 |
| 10 | **网页实时看示波器屏幕**（整屏 1920×1080 镜像，约 1 帧/秒，**按需启动**） | `scope-web on` → 浏览器 `http://172.16.102.132:8080/` | ✅ 已验证（实测 1.0–1.1 fps） |
| 11 | **网页实时看测量值**（示波器自己算的峰峰值/平均值/幅度/有效值…） | 同上，右侧面板 | ✅ 已验证（约 2 次/秒） |
| 12 | **网页实时数据波形**（SCPI 采样点自绘曲线，带网格与量纲） | 同上，"开启数据波形" | ✅ 已验证（5000 点约 4 帧/秒） |
| 13 | **网页一键把当前画面（+CSV）存进 PC 硬盘** | 同上，"保存快照到 PC" | ✅ 已验证（PNG 1920×1080 + CSV 5000 点） |
| 14 | **一次性截屏**：抓一张屏幕存 PC，不留后台进程、不占仪器 | `scope-shot` | ✅ 已验证（0.92 s / 185676 字节） |
| 15 | 临时把示波器让给其他采集脚本，用完自动收回 | 网页按钮 / `scope-capture`、`scope-shot` 自动完成 | ✅ 已验证 |

**暂不具备（可按需扩展）**：

- 真·流式采集/长时间连续记录（数据波形是 4 Hz 级刷新，不是无间隙记录；需要的话可用分段采集或 `EXPort` 落盘方案）
- 多通道同步采集（脚本支持 `-c` 指定通道，可循环调用；网页会把所有已打开通道都画出来）
- 仪器设置文件（`*SAV`/`MMEM`）批量导出
- SRQ 中断/事件回调（VXI-11 中断通道未验证）
- 通过以太网访问示波器（相关"路由模式"配置仍在树莓派上，但 eth0 当前无链路，处于闲置）
- 屏幕录制/视频导出（当前是 1 帧/秒的图片流，不是视频）

---

## 4. 快速上手

### 4.1 最常用的三件事

**① 采一段数据（自动化测试主用途，在 work-pc 上执行一条命令）**：

```powershell
ssh root@172.16.102.132 "scope-capture -c 2 -p 50000 -u A"
```

执行后：树莓派操作示波器采集 CH2 的 50000 个点，CSV 直接出现在
`F:\ZYS_HYBASE\automation\scope_results\` 下。（`-u A` 只是给列名标上单位，见 6.2）

**② 只想看一眼波形（一次性截图，不留后台进程）**：

```powershell
ssh root@172.16.102.132 "scope-shot"
```

**③ 想盯着看实时波形（按需开网页，看完记得关）**：

```powershell
ssh root@172.16.102.132 "scope-web on"      # 然后浏览器开 http://172.16.102.132:8080/
ssh root@172.16.102.132 "scope-web off"     # 交还给自动化测试
```

### 4.2 三步自检（出问题时按顺序跑）

```powershell
# ① 树莓派通不通
ssh root@172.16.102.132 "hostname; df -h /mnt/scope_results | tail -1; scope-web status"

# ② 示波器认不认得到
ssh root@172.16.102.132 "python3 /root/visa_query.py '*IDN?'"

# ③ 能不能采
ssh root@172.16.102.132 "scope-capture -c 2 -p 10000"
```

---

## 5. 实时波形网页（在网络上直接看波形）

树莓派上常驻了一个很小的网页服务 `scope-live`，**PC 用浏览器打开就能实时看到示波器的画面**，不用装任何客户端软件、不用装 VISA、也不用坐到示波器旁边。

### 5.1 怎么打开（**按需启动，默认是关的**）

考虑到示波器大部分时间要留给自动化测试独用，网页服务**不随开机启动、平时也不运行**。
需要观察时，在 work-pc 上执行一条命令把它开起来：

```powershell
ssh root@172.16.102.132 "scope-web on"
```

然后浏览器访问（work-pc、手机、平板都行，同一网络即可）：

```
http://172.16.102.132:8080/
```

看完交还给自动化：

```powershell
ssh root@172.16.102.132 "scope-web off"
```

打开即用，无需登录。页面左边是示波器屏幕的实时镜像，右边是示波器自身的测量值和采集状态。

### 5.2 只想看一眼？用一次性截图，别开服务

如果只是偶尔想确认一下屏幕，用这条命令更省事——**抓一张就走，不留任何后台进程，也不占用仪器**：

```powershell
ssh root@172.16.102.132 "scope-shot"
```

输出示例：

```
仪器 : Rohde&Schwarz,MXO38,1335.2050k08/101400,2.8.2.0
截图 : 耗时 0.92 s, 185676 字节, 1920x1080
保存 : /mnt/scope_results/screen_20260920-110354.png
Windows: F:\ZYS_HYBASE\automation\scope_results\screen_20260920-110354.png
```

补一张 `-f` 前缀，例如 `scope-shot -f 调参前`。

### 5.3 页面上有什么

| 区域/按钮 | 内容 | 刷新率 | 说明 |
|---|---|---|---|
| 示波器屏幕 | 整屏 PNG 镜像（1920×1080，和前面板看到的一致） | 约 1 帧/秒 | **不改示波器任何设置**，最忠实 |
| 测量值 | 示波器面板上已启用的测量项（峰峰值/平均值/幅度/有效值…） | 约 2 次/秒 | 数值就是示波器自己算的 |
| 采集状态 | 采样率 / 采样间隔 / 采集类型 / 时间窗 / 各通道档位与偏置 | 约 1 次/秒 | 只读 |
| 数据波形 | 用 SCPI 读回采样点，网页自己画曲线（带网格、自动量纲） | 约 4 帧/秒 | **会临时改 `ACQ:POIN`**，关闭即恢复 |
| 保存快照到 PC | 把当前画面存成 PNG，数据波形开着时同时存一份 CSV | 点一下 | 直接落进 `F:\ZYS_HYBASE\automation\scope_results\` |
| 让出设备 2 分钟 | 主动断开 VISA 会话，把示波器让给别的程序 | 点一下 | 见 5.5 |
| 新窗口看大图 | 在新标签页打开当前整屏 PNG | — | 想看细节时用 |
| 关闭屏幕镜像 | 只留数据波形时关掉镜像，数据波形可以跑得更快 | — | 两个视图可独立开关 |

**两种实时视图的区别**：屏幕镜像是"看示波器"，采样数据是"拿数据自己画"。
屏幕镜像完全不动仪器；数据波形为了把点数降下来（否则一次要读 38 MB、17 秒），
会把记录长度临时切成手动模式，**示波器屏幕上的时间窗会跟着变短**（5000 点 ≈ 260 µs），
点"关闭数据波形"就恢复原样，页面上也有明确提示。

### 5.4 服务管理

**日常只用这三条**（在树莓派上，或 `ssh root@172.16.102.132 "..."` 远程执行）：

```bash
scope-web on        # 开启实时波形网页（会打印网址和状态）
scope-web off       # 关掉并把示波器交还给自动化测试
scope-web status    # 看服务在不在、连没连上示波器、帧率、测量项
scope-web restart   # 重启
```

底层就是 systemd 服务，需要时也可直接用：

```bash
systemctl status scope-live       # 看状态与最近日志
systemctl stop scope-live         # = scope-web off（会先把示波器设置恢复原样再退出）
journalctl -u scope-live -f       # 实时看日志
```

- **开机自启已关闭**（`disabled`）：树莓派重启后网页服务不会自己起来，示波器始终留给自动化测试；
  若哪天想改回常驻：`systemctl enable --now scope-live`（再用 `disable` 取消）。
- 示波器没开机 / USB 没插时，服务不会退出，而是每 2 秒重试连接，插上即自动恢复；
- 启动参数（端口、默认点数等）写在 `/etc/systemd/system/scope-live.service` 的 `ExecStart` 里，
  例如 `--port 8080 --points 5000`，改完 `systemctl daemon-reload && systemctl restart scope-live`。

### 5.5 重要：R&S VISA 对这台仪器是**独占**的

实测：网页服务连着示波器时，别的进程再打开同一个 VISA 资源会直接报
`0xBFFF0011 Insufficient location information...`。因此：

- 页面上的 **"让出设备 2 分钟"** 按钮可让服务临时断开，期间能在树莓派上跑任何采集脚本，时间到自动重连；
- `scope-capture` 已内置自动交接：它先让网页服务让出设备，采完立刻收回，**直接跑就行，不会打不开仪器**；
- 手工写的脚本若报这个错，先 `systemctl stop scope-live`（或点让出按钮）再跑。

### 5.6 页面背后的接口（方便二次开发 / 接进别的系统）

| 接口 | 作用 |
|---|---|
| `GET /frame.png` | 最新一帧屏幕 PNG |
| `GET /stream.mjpg` | MJPEG 推流（浏览器 `<img>` 直接显示） |
| `GET /wave.json` | 数据波形（时间轴 + 采样值 + 统计，已抽稀保尖峰） |
| `GET /api/status` | 连接状态、测量值、采集参数、通道设置、帧率（JSON） |
| `GET /api/mode?screen=1&data=1&points=5000` | 开关两个视图、设点数 |
| `GET /api/capture` | 保存当前画面（+CSV）到 PC 硬盘 |
| `GET /api/pause?seconds=120` | 让出设备 N 秒；`seconds=0` 表示立刻收回 |

带宽占用很小：屏幕镜像约 1.5 Mbit/s（187 KB/帧），WiFi 上毫无压力。

---

## 6. 详细用法

### 6.1 发任意 SCPI 命令

```bash
python3 /root/visa_query.py "*IDN?" "CHAN2:SCAL?" "ACQ:SRAT?" "SYST:ERR?"
```

输出示例：

```
资源: USB0::0x0AAD::0x0197::1335.2050k08-101400::INSTR
*IDN?                        -> Rohde&Schwarz,MXO38,1335.2050k08/101400,2.8.2.0
CHAN2:SCAL?                  -> 0.5
ACQ:SRAT?                    -> 19230769.2308
SYST:ERR?                    -> 0,"No error"
```

一次可传多个命令，逐条回显；不支持的命令会显示"不支持/超时"，不会中断后续命令。

### 6.2 采集波形

```bash
scope-capture [选项]
  -c, --channel N   通道号（默认 2）
  -p, --points N    记录长度，会临时改示波器设置，采完自动还原（默认 20000）
  -o, --outdir DIR  输出目录（scope-capture 默认 /mnt/scope_results）
  -u, --unit U      单位标注，如 V / A / mA（默认 auto，只写"通道单位"，见下）
      --keep-acq    不改示波器设置，直接读当前记录（记录可能非常大，慎用）
```

> **单位说明**：SCPI 没有查询通道单位的命令（`:CHANnel<n>:UNIT?` 在 MXO38 上不支持），
> 返回的数值已经按通道自己的单位折算——**电压探头是 V，电流探头是 A**。
> 当前示波器 CH2 接的是电流探头（500 mA/div、20 MHz 带宽），所以数值是**安培**。
> 想把单位标清楚就加 `-u A`（或 `-u mA`），CSV 列名会变成 `value_A`，`# 单位:` 注释也一并写明。
> 另外 `scope-capture` 会在运行时自动让网页服务让出示波器，无需手工干预。

直接调用（不经 wrapper）：

```bash
python3 /root/scope_capture_usb.py -c 2 -p 100000 -o /mnt/scope_results
```

一次典型输出：

```
仪器 : Rohde&Schwarz,MXO38,1335.2050k08/101400,2.8.2.0
CH2 : 0.5 /div, offset 0.705 A   (单位：A)
原始记录长度 : 9615386  记录模式: AUTO  原始格式: REAL,32
采集 : 50000 点, 19.231 MSa/s, 时基 0.5 s, dt = 5.200e-08 s
读取 : 50000 点 / 200000 字节, 耗时 1.0 s
时间轴 : xstart=0.2487s, xstop=0.2513s, 跨度=0.0026s, dt=5.2e-08s (header 点数 50000, 每点值数 1)
错误队列 : 干净
统计 : min -0.016107 A, max +0.013276 A, 平均 -0.001897 A, 峰峰 0.029383 A
CSV : /mnt/scope_results/mxo38_ch2_20260920-103914.csv (1637753 字节)
```

### 6.3 输出文件格式

文件名：`mxo38_ch<通道>_<年月日-时分秒>.csv`

```csv
# Rohde&Schwarz,MXO38,1335.2050k08/101400,2.8.2.0
# channel=CH2 scale=0.5/div offset=0.705 points=50000 srate=19230769.23Sa/s xstart=0.2486999839996021s unit=A
time_s,value_A
2.486999840e-01,-1.909892308e-04
...
```

- 前两行是 `#` 注释，记录仪器身份、采集条件与单位，便于溯源；
- 数据两列：时间（秒）与通道值（**已按探头折算，无需再乘 V/div 或 A/div**）；
  不带 `-u` 时列名是 `time_s,value`，注释里写"通道单位（电压探头 V，电流探头 A）"；
- 可直接被 Excel / Python(pandas) / MATLAB 读取。

读取示例：

```python
import pandas as pd
df = pd.read_csv(r"F:\ZYS_HYBASE\automation\scope_results\mxo38_ch2_20260920-103914.csv", comment="#")
print(df.describe())
```

### 6.4 从 PC 往树莓派推文件

```powershell
scp D:\some\script.py root@172.16.102.132:/root/
scp -r D:\some\folder   root@172.16.102.132:/root/
```

已配置免密（work-pc 的公钥 `work-pc-to-pi` 已装到树莓派 root 账号）。

---

## 7. 关键技术细节（二次开发必读）

| 主题 | 结论 |
|---|---|
| VISA 资源地址 | `USB0::0x0AAD::0x0197::1335.2050k08-101400::INSTR` |
| VISA 实现 | R&S VISA 5.12.9，库文件 `/usr/lib/librsvisa.so`，无 Python 绑定，故用 `ctypes` 封装（`/root/rsvisa.py`） |
| 安装包架构 | **必须 armhf**。本机 `dpkg --print-architecture = armhf`（32 位用户态 + 64 位内核），arm64/amd64 的包不可用 |
| 改记录长度 | `ACQ:POIN` 在 `ACQ:POIN:MODE = AUTO` 时是**只读**的；必须先 `ACQ:POIN:MODE MAN`，再 `ACQ:POIN <n>` |
| 采样率与跨度 | `ACQ:SRAT?` 是可信的实际采样率（19.23 MSa/s → dt = 52 ns）。**点数决定记录跨度**：20000 点≈1.04 ms，50000 点≈2.6 ms，100000 点≈5.2 ms |
| 时间轴计算 | 用 `CHAN<n>:DATA:HEADer?` 返回的 `XStart, XStop, 点数, 每点值数`：`dt = (XStop-XStart)/(N-1)`。**不要**用 `TIM:RANG` 推算 |
| 数据格式 | `FORMat:DATA` = `REAL,32`，二进制块传输，数值已按**通道自身单位**折算（电压探头 V、电流探头 A）；SCPI 查不到单位，故采集脚本用 `-u` 标注 |
| 屏幕截图 | `HCOPy:DATA?` 直接返回整屏 PNG 二进制块（本机型 `HCOPy:DEVice1:LANGuage` 支持 PNG/BMP/JPG），1920×1080 约 187 KB、耗时约 0.9 s，是屏幕镜像的来源；`SYSTem:DISPlay:UPDate` 默认 0 也不影响画面刷新 |
| 测量值读取 | `MEASurement<n>:RESult:ACTual?` 单项约 0.4 ms，**但槽位存在≠可用**：`ENABle=0`、或源通道已关闭、或槽位不存在时，该查询会拖满 **2 秒**超时。所以要先验证再纳入轮询（`scope_live.py` 已实现） |
| VISA 独占 | 同一时刻只有一个进程能打开该 USB 资源，第二个 `viOpen` 报 `0xBFFF0011`；网页服务因此提供"让出设备"接口 |
| 窗口化导出 | `EXPort:WAVeform:STARt/STOP` 在本机型固件上返回 `-397 Not available`，无法只导出一段记录；要降低传输量只能改 `ACQ:POIN`（当前 9.62 M 点 ≈ 38 MB/17 s） |
| VISA 读取 | 读满缓冲区时返回 `0x3FFF0006 (VI_SUCCESS_MAX_CNT)`，属**正常返回**，不是错误 |
| 设备节点权限 | `/dev/usbtmc0` 默认 `root:root 0600`，**root 可直接用**；如需普通用户访问，加 udev 规则 `KERNEL=="usbtmc*", MODE="0666"` |
| 设置保护 | 采集脚本用 `try/finally` 保证即使中途报错也会还原 `ACQ:POIN` 与 `ACQ:POIN:MODE`，并恢复 `RUN` 连续采集 |

### 二次开发示例

```python
import sys
sys.path.insert(0, "/root")
from rsvisa import Instrument

with Instrument(timeout_ms=30000) as inst:
    print(inst.query("*IDN?"))
    inst.write("CHAN2:STAT ON")
    print(inst.query("CHAN2:SCAL?"))
    raw = inst.query_block("CHAN2:DATA?")     # 二进制块自动解析
```

写 SCPI 前先查 `/root/scpi_headers.txt`（从仪器导出的 3256 条命令头，比如
`grep -i "MEAS" /root/scpi_headers.txt`）。

---

## 8. 环境与依赖清单

| 组件 | 版本/说明 |
|---|---|
| 树莓派系统 | Raspbian GNU/Linux 12 (bookworm)，内核 6.12.25+rpt-rpi-v8 |
| R&S VISA | 5.12.9（`rsvisa_5.12.9_armhf.deb`），依赖齐全，`dpkg -l rsvisa` 可查 |
| USBTMC | 内核模块 `usbtmc`（`/lib/modules/.../usbtmc.ko.xz`） |
| SMB 客户端 | `cifs-utils 2:7.0-2`，`mount.cifs 7.0`，内核 `cifs` 模块 |
| Python | 3.11.2（无 pyvisa；VISA 走 ctypes），未装 matplotlib |

---

## 9. 运维手册

### 9.1 网络盘

```bash
df -h /mnt/scope_results          # 看挂载是否在
mount -a                          # 按 /etc/fstab 重挂
umount /mnt/scope_results         # 卸载
```

- fstab 条目：`//172.16.102.207/F$/ZYS_HYBASE/automation/scope_results /mnt/scope_results cifs credentials=/etc/cifs-credentials,uid=0,gid=0,file_mode=0664,dir_mode=0775,iocharset=utf8,_netdev,nofail 0 0`
- 凭据文件：`/etc/cifs-credentials`（权限 600，内容 `username=remote` / `password=...`）
- `_netdev,nofail` 保证 PC 不在线时树莓派照常启动，不会卡住

### 9.2 work-pc 侧必须保留的防火墙规则

```
名称：SMB from raspberry-pi (172.16.102.132)
方向：入站 协议：TCP 端口：445 远程地址：172.16.102.132 动作：允许
```

- **删掉它 445 立即不通，网络盘挂不上**（已实测）。
- 该规则只放行树莓派这一个 IP，未对校园网开放。
- 删除命令：`Remove-NetFirewallRule -DisplayName "SMB from raspberry-pi (172.16.102.132)"`
- 注意：work-pc **不响应 ICMP**（ping 不通是正常的），但 TCP 22/445 是通的，排查时别被 ping 误导。

### 9.3 常见问题排查

| 现象 | 原因与处理 |
|---|---|
| `scope-capture` 报找不到仪器 | ① 示波器 USB 是否插好：`lsusb \| grep 0aad`；② 设备节点是否存在：`ls -l /dev/usbtmc0`；③ 内核日志：`dmesg \| grep -i usbtmc` |
| 采集报 `viRead 超时` | 通常是发了一条仪器不支持的查询（不会有响应）。用 `visa_query.py` 单条试，并查 `SYST:ERR?` |
| 记录长度改不动 | 忘记先切 `ACQ:POIN:MODE MAN`（脚本已处理） |
| 网络盘写入后 PC 侧显示 0 字节 | SMB 元数据缓存延迟，稍等即可；以 MD5 为准 |
| 网络盘挂载失败 | 检查 work-pc 是否开机、445 是否通（`timeout 3 bash -c "echo > /dev/tcp/172.16.102.207/445"`）、防火墙规则是否还在 |
| 树莓派 IP 变了 | 已固定为静态 172.16.102.132/22；若校园网拒绝该地址，见 9.4 |
| 网页打不开（`http://172.16.102.132:8080/`） | **先确认服务开了没有**——默认是关的，执行 `scope-web on`。仍打不开：① `scope-web status`；② 树莓派本机 `curl -s http://127.0.0.1:8080/api/status`；③ 端口被占：`ss -lntp \| grep 8080`；④ 换端口改 unit 里的 `--port` |
| 网页能开但画面不动 | 多半是页面上点了"让出设备"在倒计时里（顶部有红条提示）；或示波器被别的程序占着（见 9.4）。看日志：`journalctl -u scope-live -n 30` |
| 采集脚本报 `0xBFFF0011 打不开仪器` | 示波器被网页服务独占。`scope-capture` 会自动让出；自己写的脚本先 `systemctl stop scope-live` 或调 `/api/pause?seconds=120` |
| 网页"数据波形"打开后示波器时间窗变短 | 正常现象：该模式把记录长度临时切成 5000 点，示波器屏幕时间窗随之变短；点"关闭数据波形"即恢复 |

### 9.4 已知风险

- **wlan0 静态 IP 与校园网 DHCP 的潜在冲突**：172.16.102.132 原为 DHCP 分配地址（租期 24h），改为静态后不再续租。租约过期后理论上可能被分配给其他设备造成冲突。建议向网络中心登记静态绑定，或选用 DHCP 池外地址。
- **示波器以太网配置目前闲置**：示波器以太网为静态 192.168.2.200/24、网关 192.168.2.1；树莓派 eth0 为 192.168.2.201/24 但**当前无链路**。此前配置的"方案B 路由模式"（PC 经树莓派访问示波器真实 IP）仍在树莓派上生效，因无链路而处于闲置状态，详见 `/root/scope-forward-README.md`。**注意 192.168.2.200 曾与树莓派 eth0 撞车，已把 eth0 改为 .201。**
- **VISA 独占**：`scope-live` 在跑的时候，别的进程打不开示波器（报 `0xBFFF0011`）。这是 R&S VISA/USBTMC 的行为，不是配置问题。服务平时不开，`scope-capture`/`scope-shot` 也内置了自动交接；自写脚本需先让出（见 5.5）。
- **网页服务默认不启动、也不自启**：示波器完整留给自动化测试。需要观察时 `scope-web on`，看完 `scope-web off`。**忘了关也不会永久占着**——只是在你关掉前自动化脚本会打不开仪器，看到 `0xBFFF0011` 就是这个原因。
- **屏幕镜像是 1 帧/秒，不是视频**：受仪器 `HCOPy:DATA?` 自身耗时（约 0.9 s）限制，做到 1 fps 已是这台机器的上限（PNG/BMP/JPG 三种格式耗时几乎一样，说明瓶颈在仪器渲染+USB 传输，不在编码）。

---

## 10. 验证记录（交付时的实测证据）

| 验证项 | 结果 |
|---|---|
| VISA 资源枚举 | 找到 `USB0::0x0AAD::0x0197::1335.2050k08-101400::INSTR` 与 `ASRL0::INSTR` |
| 身份查询 | `*IDN?` → `Rohde&Schwarz,MXO38,1335.2050k08/101400,2.8.2.0`；`*OPT?` → `B282,B285`；`SYST:ERR?` → `0,"No error"` |
| 采集性能 | 50000 点 / 200000 字节 / **1.0 秒**读完（USB HS） |
| 数据有效性 | 峰峰 0.0294 V 的真实小信号；时间轴 dt = 52 ns，与 19.23 MSa/s 自洽 |
| 设置保护 | 采集后示波器恢复 `9615386` 点 / `AUTO` 模式，错误队列干净 |
| 网络盘写入 | Windows 侧与树莓派侧文件均为 `1637753` 字节 |
| 数据完整性 | MD5 双向一致：`d08796daf228da0c54780158f3d06561` |
| PC→树莓派推文件 | `scp` 返回 `exit=0`，文件内容一致 |
| **屏幕镜像** | 从 PC 侧（WiFi，172.16.103.86）取 `http://172.16.102.132:8080/frame.png` 成功，PNG 1920×1080 / 约 187 KB；连续取帧内容不同（画面在动） |
| **镜像帧率** | 服务自报 `fps = 1.04`；MJPEG 流实测 6 秒收到 7 帧（≈1.17 fps） |
| **测量值刷新** | 页面读到示波器自身测量：峰峰值 / 平均值 / 幅度 / 有效值（与示波器屏幕上显示的 4 项一致） |
| **数据波形** | `GET /wave.json` 返回 C2 的 8000 点（dt = 52 ns、峰峰 25.7 mA），页面 JS 在打桩 DOM 下成功绘制 8000 点 |
| **一键存快照** | `GET /api/capture` 在 `F:\ZYS_HYBASE\automation\scope_results\` 生成 `live_20260920-105545.png`（188069 字节，1920×1080）与 `.csv`（5003 行） |
| **让出设备** | 调 `/api/pause?seconds=25` 后，外部脚本立即成功打开 VISA 资源；到期后服务自动重连（fps 恢复 1.0） |
| **自动交接** | 服务运行中直接跑 `scope-capture -c 2 -p 4000 -u A`：3.3 秒完成、CSV 130260 字节、示波器设置恢复 `AUTO/9615386`、错误队列干净 |
| **页面 JS** | 用打桩 DOM/canvas 执行页面内联 JS：状态面板、测量表、采集参数、画布绘制全部正常，空数据不抛异常 |
| **按需启停** | `scope-web on` → 服务 active、打印网址、自报 `connected=true`；`scope-web off` → 服务 inactive；`systemctl is-enabled` = **disabled** |
| **仪器交还** | 服务停止后外部进程 0.02 s 打开仪器成功（`ACQ:POIN:MODE=AUTO`），自动化脚本可独占 |
| **一次性截屏** | `scope-shot` → 0.92 s、185676 字节、1920×1080 PNG，落到 `F:\ZYS_HYBASE\automation\scope_results\screen_20260920-110354.png`，无残留进程 |

---

## 11. 本次交付做过的变更

**树莓派上：**

- 安装 `rsvisa_5.12.9_armhf.deb`（R&S VISA 5.12.9）
- 新增 `/root/rsvisa.py`、`/root/visa_probe.py`、`/root/visa_query.py`、`/root/scope_capture_usb.py`、`/root/scpi_headers.txt`
- 新增 `/usr/local/bin/scope-capture`
- 新增 `/etc/cifs-credentials`（600）与 `/etc/fstab` 中的网络盘条目
- 新增挂载点 `/mnt/scope_results`
- 授权 work-pc 的公钥 `work-pc-to-pi` 登录 root
- eth0 静态地址由 192.168.2.200 改为 **192.168.2.201**（避开与示波器冲突）
- wlan0 由 DHCP 改为 **静态 172.16.102.132/22**（网关 172.16.100.1，DNS 202.96.134.133 / 202.96.128.68）
- 文档：`/root/scope-usb-visa-README.md`、`/root/scope-forward-README.md`（以太网方案）
- **（v1.1 新增）实时波形网页**：`/root/scope_live.py`（只用标准库 + rsvisa，无新依赖）
- **（v1.1 新增）** systemd 服务 `/etc/systemd/system/scope-live.service`，监听 `0.0.0.0:8080`
- **（v1.2 修改）** 该服务**已关闭开机自启并停止运行**（`systemctl disable --now scope-live`）：平时仪器独占给自动化测试，需要观察时 `scope-web on`
- **（v1.2 新增）** `/usr/local/bin/scope-web`（按需启停 + 状态查询）、`/usr/local/bin/scope-shot` + `/root/scope_shot.py`（一次性截屏）
- **（v1.1 修改）** `/usr/local/bin/scope-capture` 增加"自动让出/收回示波器"，避免与网页服务抢仪器
- **（v1.1 修改）** `/root/scope_capture_usb.py` 增加 `-u/--unit` 单位标注，CSV 列名由 `voltage_V` 改为 `value[_单位]`（原写法在电流探头下是错的）

**work-pc 上：**

- 新增 SSH 密钥 `C:\Users\remote\.ssh\id_ed25519`（用于免密推送文件到树莓派）
- 新增防火墙规则 `SMB from raspberry-pi (172.16.102.132)`（入站 TCP 445，仅该 IP）
- 新增静态路由 `192.168.2.0/24 → 172.16.102.132`（以太网方案的遗留，当前未使用）

---

## 12. 文件清单

**树莓派（`/root/`）**

| 文件 | 说明 |
|---|---|
| `rsvisa.py` | R&S VISA 的 ctypes 封装（推荐二次开发直接 import） |
| `visa_probe.py` | 枚举 VISA 资源并查询身份 |
| `visa_query.py` | 命令行发送任意 SCPI |
| `scope_capture_usb.py` | 采集波形并存 CSV（自动改/还原仪器设置，`-u` 标注单位） |
| `scope_shot.py` | 一次性截屏（抓一张屏幕 PNG 存 PC） |
| `scope_live.py` | **实时波形网页服务**（屏幕镜像 + 测量值 + 数据波形 + 存快照 + 让出设备） |
| `scpi_headers.txt` | 仪器导出的 3256 条 SCPI 命令头 |
| `scope-usb-visa-README.md` | USB/VISA 侧技术笔记 |
| `scope-forward-README.md` | 以太网路由方案说明（当前闲置） |

**系统服务 / 命令**

| 名称 | 说明 |
|---|---|
| `scope-live`（systemd） | 网页实时波形服务，**默认停用且不开机自启**，由 `scope-web` 按需启停 |
| `scope-web {on\|off\|status\|restart}` | 开/关实时波形网页（`on` 会打印网址与状态） |
| `scope-shot [-f 前缀]` | 抓一张示波器屏幕存到 `/mnt/scope_results`，抓完即退出 |
| `scope-capture` | 采集波形并默认写入 `/mnt/scope_results`（自动与网页服务交接设备） |

**数据目录**

| 位置 | 说明 |
|---|---|
| `/mnt/scope_results` = `F:\ZYS_HYBASE\automation\scope_results` | 采集结果 CSV 存放处 |
| ↳ `mxo38_ch<N>_<时间>.csv` | `scope-capture` 采到的波形 |
| ↳ `live_<时间>.png` / `live_<时间>.csv` | 网页上"保存快照到 PC"存下的画面与波形 |
| ↳ `screen_<时间>.png` | `scope-shot` 一次性截屏 |
