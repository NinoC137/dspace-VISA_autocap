# MXO38 参数控制交接文档（scope-ctl / VISA SCPI）

| 项 | 内容 |
|---|---|
| 版本 | v1.0 |
| 日期 | 2026-09-20 |
| 适用链路 | 示波器 USB → 树莓派 `172.16.102.132`（R&S VISA）→ WiFi → work-pc / Mac |
| 前置文档 | `RASPI_MXO38_DELIVERY.md`（链路、网络盘、`scope-capture`/`scope-web`/`scope-shot`） |
| 本文位置 | `F:\ZYS_HYBASE\automation\MXO38_SCPI_CONTROL_HANDOFF.md`，树莓派副本 `/root/MXO38_SCPI_CONTROL_HANDOFF.md` |

---

## 1. 一句话说明

树莓派上新增了一个命令 **`scope-ctl`**，把示波器所有常用面板操作（通道开关、V/div、偏置、耦合、带宽、时基、触发、采集模式、网格/余辉/亮度、测量槽、截图、设置保存/恢复、原始 SCPI）包装成一行命令。**每次调用独立打开并关闭 VISA 会话，不常驻、不占仪器**；每个写操作都回显"旧值 → 新值"并检查错误队列。

AI 侧同一套能力以 MCP 工具 `mcp__mxo38__scope_ctl_*` 暴露（Mac 上的 `~/.dsh/mcp/mxo38-scope-bench/server.mjs`，直连树莓派）。

---

## 2. 前提与安全边界

- **VISA 独占**：同一时刻只有一个进程能打开示波器。`scope-ctl` 会自动请 `scope-live` 网页服务让出（`/api/pause`），用完交还；但**不会**与台架采集（`pi_agent.py` / `START_SCOPE_BENCH.cmd` 批测）协商。**批测进行中不要用 `scope-ctl` 写参数。**
- 打开 VISA 失败报 `0xBFFF0011 Insufficient location information...` = 别的进程正占着仪器，不是仪器不在。先 `scope-web status`、`ps aux | grep -E 'scope_live|pi_agent|scope_capture'`。
- `scope-ctl` **只碰示波器**。ControlDesk 参数、`Enable1`、录波器与它无关。
- 会大改仪器状态的操作已加护栏：`autoscale` 会先记快照；`scpi` 写命令必须加 `--write`；`*RST`、`SYST:PRES`、`SYST:SHUT`、`MMEM:DEL`、`CAL:ALL` 等还要再加 `--i-know`。MCP 工具不暴露 `--i-know`。
- 改参数前建议先 `scope-ctl settings snapshot <file.json>`，事后 `settings restore` 可一键回到原样（实测 100 项全部恢复，零差异）。

---

## 3. 三种入口

```bash
# ① 在树莓派上（最直接）
ssh root@172.16.102.132
scope-ctl state

# ② 在 work-pc 上（PowerShell）
ssh root@172.16.102.132 "scope-ctl chan 2 --scale 0.2"

# ③ 在 Mac 上（已免密）
ssh root@172.16.102.132 scope-ctl state
```

加 `--json` 得到机器可读输出（MCP 就是这么调的）。`scope-ctl -h`、`scope-ctl chan -h` 看各子命令帮助。

---

## 4. 子命令速查

| 子命令 | 作用 | 例子 |
|---|---|---|
| `info` | IDN / 选件 / 错误队列 | `scope-ctl info` |
| `state [--all]` | 全量只读快照（默认只展开已打开通道） | `scope-ctl state --all` |
| `chan <n\|all> [on\|off] [--scale --offset --position --coupling --bandwidth --invert --skew --unit --atten]` | 通道参数 | `scope-ctl chan 2 --scale 500m --offset 0.7`<br>`scope-ctl chan 3 on --coupling DCLimit` |
| `tb [--scale --range --position --reference --roll]` | 时基 | `scope-ctl tb --scale 1m --reference 50` |
| `acq [--type --count --srate-mode --srate --points-mode --points --interp]` | 采集 | `scope-ctl acq --type AVER --count 16`<br>`scope-ctl acq --points 20000` |
| `trig [--mode --source --type --slope --level --holdoff-mode --find-level --force]` | 触发 | `scope-ctl trig --mode NORM --source C2 --level 0.1 --slope POS` |
| `run` / `stop` / `single [--wait N]` | 运行控制 | `scope-ctl single --wait 10` |
| `autoscale` | 仪器 Autoset（大改通道/时基/触发，慎用） | |
| `display [...]` | 网格/细网格/标签/十字线/余辉/矢量或点/亮度/背光/字号/结果表位置/屏幕消息 | `scope-ctl display --grid off --persist off --message 'VL=400V'` |
| `view <clean\|bare\|analysis\|default\|list>` | 界面预设 | `scope-ctl view clean` |
| `meas <n> [on\|off] [--type --source --stats --reset-stats --clear]` | 测量槽 1-8 | `scope-ctl meas 3 on --type RMS --source C2` |
| `meas read` | 读所有已启用测量值 | |
| `screenshot [-f 前缀] [-o 目录]` | 整屏 PNG 到 `/mnt/scope_results`（即 `F:\...\scope_results`） | `scope-ctl screenshot -f before` |
| `settings list\|save <名>\|recall <名>` | 仪器内部设置文件（`/home/storage/userData/settings/<名>.dfl`） | `scope-ctl settings save vl400` |
| `settings snapshot <file.json>` / `restore <file.json>` | 参数快照到 JSON / 从 JSON 回写 | `scope-ctl settings snapshot /mnt/scope_results/snap.json` |
| `scpi <cmd...> [--write] [--i-know]` | 原始 SCPI；`?` 结尾为查询 | `scope-ctl scpi 'CHAN2:SCAL?' 'ACQ:SRAT?'`<br>`scope-ctl scpi --write 'CHAN2:SCAL 0.5'` |
| `errors` | 读取并清空错误队列 | |
| `recover` | 链路救援：viClear → USB 端口重枚举 → 复查 IDN | |

数值支持 SI 后缀：`500m` `20u` `2n` `10k` `20M`。通道写法 `2` / `C2` / `CH2` 均可。

---

## 5. 参数 ↔ SCPI 对照（本机实测，固件 2.8.2.0）

`SCPI` 列写的是短形式，长形式见括号。`scope-ctl` 列为对应选项。"实测值"是 2026-09-20 交付时的仪器状态。

### 5.1 通道 `CHANnel<n>`（n = 1..8）

| 参数 | SCPI | scope-ctl | 取值 | 实测（CH2） |
|---|---|---|---|---|
| 开关 | `CHAN<n>:STAT` | `chan n on/off` | ON/OFF | 1 |
| 每格 | `CHAN<n>:SCAL` | `--scale` | 数值，单位随探头（V/div 或 A/div） | 0.5 |
| 满屏 | `CHAN<n>:RANG` | `--range` | = scale × 10 | 5 |
| 偏置 | `CHAN<n>:OFFS` | `--offset` | 通道单位 | 0.705 |
| 垂直位置 | `CHAN<n>:POS` | `--position` | 格（div） | 0 |
| 耦合 | `CHAN<n>:COUP` | `--coupling` | `DC`(50 Ω) / `DCLimit`(1 MΩ) / `AC` | DCL |
| 带宽 | `CHAN<n>:BAND` | `--bandwidth` | Hz，如 `20M`、`FULL` | 50000000 |
| 阻抗 | `CHAN<n>:IMP` | （只读显示） | | 50 |
| 反相 | `CHAN<n>:INV` | `--invert` | ON/OFF | 0 |
| 时延 | `CHAN<n>:SKEW:TIME` | `--skew` | 秒 | 0 |
| 外部衰减 | `CHAN<n>:EATT` / `EATS` | — | 数值 / LIN\|LOG | 50 / LIN |

### 5.2 探头 `PROBe<n>:SETup`

| 参数 | SCPI | scope-ctl | 说明 |
|---|---|---|---|
| 探头名 | `PROB<n>:SET:NAME?` | 只读 | 本机 "User Defined"（无智能探头，`STAT?`=NDET） |
| 单位 | `PROB<n>:SET:ATT:UNIT` | `--unit V\|A` | **这就是 CSV/波形的量纲来源**；CH1、CH2 当前为 A，其余 V |
| 衰减模式 | `PROB<n>:SET:ATT:MODE` | 自动切 MAN | AUTO / MAN |
| 手动衰减 | `PROB<n>:SET:ATT:MAN` | `--atten` | CH1=0.1、CH2=0.2；**AUTO 模式下查询返回 -397 Not available（超时）** |
| 单位（备用） | `PROB<n>:SET:ADV:UNIT?` | — | 本机不支持（超时） |

### 5.3 时基 `TIMebase`

| 参数 | SCPI | scope-ctl | 实测 |
|---|---|---|---|
| s/div | `TIM:SCAL` | `--scale` | 0.05 |
| 总时长 | `TIM:RANG` | `--range` | 0.5（= 10 格） |
| 水平位置 | `TIM:HOR:POS` | `--position` | 0 |
| 参考点 | `TIM:REF` | `--reference` | 0（%，0=左 50=中 100=右） |
| 滚动 | `TIM:ROLL:ENAB` | `--roll` | AUTO / OFF |
| 格数 | `TIM:DIV?` | 只读 | 10 |

### 5.4 采集 `ACQuire`

| 参数 | SCPI | scope-ctl | 取值 / 实测 |
|---|---|---|---|
| 类型 | `ACQ:TYPE` | `--type` | `SAMPle` / `AVERage` / `ENVelope` / `PDETect`；实测 SAMP |
| 平均次数 | `ACQ:COUN` | `--count` | 1 |
| 采样率模式 | `ACQ:SRAT:MODE` | `--srate-mode` | AUTO / MANual；实测 MAN |
| 采样率 | `ACQ:SRAT` | `--srate` | 19230769.23 Hz（52 ns） |
| 分辨率 | `ACQ:RES?` | 只读 | 5.2e-8 s |
| 记录长度模式 | `ACQ:POIN:MODE` | `--points-mode` | AUTO / MANual；实测 AUTO |
| 记录长度 | `ACQ:POIN` | `--points`（自动切 MAN） | 9615386；**AUTO 下只读** |
| 上限 | `ACQ:POIN:MAX?` | 只读 | 10000000 |
| 插值 | `ACQ:INT` | `--interp` | LINear / SINX / SMHD；实测 SINX |
| 已采次数 | `ACQ:CURR?` | `info` 显示 | 用来判断 RUN 是否在跑（数字增长） |

### 5.5 触发 `TRIGger`

| 参数 | SCPI | scope-ctl | 取值 / 实测 |
|---|---|---|---|
| 模式 | `TRIG:MODE` | `--mode` | `AUTO` / `NORMal` / `FREerun`；实测 AUTO |
| 源 | `TRIG:EVEN1:SOUR` | `--source` | C1..C8 / EXT / LINE；实测 C3 |
| 类型 | `TRIG:EVEN1:TYPE` | `--type` | EDGE / WIDTh / RUNT / WINDow / TIMeout / INTerval / SLEWrate / SETHold / STATe / PATTern；实测 EDGE |
| 边沿 | `TRIG:EVEN1:EDGE:SLOP` | `--slope` | POSitive / NEGative / EITHer |
| 电平 | `TRIG:EVEN1:LEV<n>` | `--level`（n 取自触发源通道） | **每个通道各有一个电平**，LEV2=CH2 的电平 |
| 释抑 | `TRIG:HOLD:MODE` | `--holdoff-mode` | OFF / TIME / EVENts / RANDom / AUTO |
| 触发耦合 | `TRIG:ANED:COUP?` | 只读 | DCL |
| 找电平 | `TRIG:FIND` | `--find-level` | 动作 |
| 强制触发 | `TRIG:FORC` | `--force` | 动作 |

### 5.6 显示 `DISPlay` / 界面

| 参数 | SCPI | scope-ctl | 实测 |
|---|---|---|---|
| 网格 | `DISP:DIAG:GRID` | `--grid` | 1 |
| 细网格 | `DISP:DIAG:FIN` | `--fine-grid` | 1 |
| 轴标签 | `DISP:DIAG:LAB` | `--labels` | 1 |
| 十字线 | `DISP:DIAG:CROS` | `--crosshair` | 1 |
| 绘制样式 | `DISP:DIAG:STYL` | `--style` | VECTors / DOTS |
| Y 轴固定 | `DISP:DIAG:YFIX` | `--y-fixed` | 1 |
| X 轴固定 | `DISP:DIAG:XFIX` | `--x-fixed` | 0 |
| 余辉 | `DISP:PERS:STAT` / `TIME` / `INF` | `--persist` / `--persist-time` / `--infinite` | 1 / 0.05 s / 0 |
| 余辉清除 | `DISP:PERS:RES` | `--persist-reset` | 动作 |
| 波形亮度 | `DISP:INT` | `--intensity` | 50 % |
| 背光 | `DISP:BACK` | `--backlight` | 90 % |
| 清屏 | `DISP:CLR` | `--clear` | 动作 |
| 对话框 / 结果字号 | `DISP:DIAL:FONT` / `DISP:RES:FONT` | `--dialog-font` / `--result-font` | 22 / 22 |
| 结果表位置 | `LAY1:RPOS` | `--result-position` | LEFT |
| 屏幕消息 | `SYST:DISP:MESS "文本"` + `SYST:DISP:MESS:STAT` | `--message '文本'`，`--message ''` 清除 | 已验证能在屏幕上显示 |
| 远控时刷屏 | `SYST:DISP:UPD` | `--remote-update` | 0（默认；本机型 0 也不影响画面刷新与截图） |
| 锁前面板 | `SYST:KLOC` | `--lock-keys` | 0 |
| 工具栏收起 | `DISP:TOOL:DES` | `--toolbar-deselect` | 动作 |

界面预设 `view`：

| 预设 | 内容 |
|---|---|
| `clean` | 网格开、细网格关、标签开、十字线关、余辉关、矢量、消息关、亮度 60 —— **纯波形界面** |
| `bare` | 连网格与标签都关 —— 截图贴报告 |
| `analysis` | 细网格、十字线、50 ms 余辉 |
| `default` | 恢复到 2026-09-20 观察到的原始样式 |

> "各个选项的界面"（对话框、菜单）没有 SCPI 可以直接打开/关闭；能控制的是上面这些显示元素、结果表位置、工具栏与屏幕消息。若要看仪器上打开了什么对话框，用 `screenshot`。

### 5.7 测量 `MEASurement<n>`（槽位 1..8）

| 参数 | SCPI | scope-ctl | 实测 |
|---|---|---|---|
| 启用 | `MEAS<n>:ENAB` | `meas n on/off` | 1,2,4,5,7 启用 |
| 类型 | `MEAS<n>:MAIN` | `--type` | PDEL(峰峰) MEAN AMPL RMS FREQ PER MAX MIN RTIM FTIM PDCY NDCY HIGH LOW … |
| 源 | `MEAS<n>:SOUR` | `--source` | C2（MEAS7 在 C3） |
| 统计 | `MEAS<n>:STAT` | `--stats` | 0 |
| 当前值 | `MEAS<n>:RES:ACT?` | `meas read` | |

**槽位规则（实测）**：槽位未启用、或源通道关闭、或该测量没有结果（如 C3 无信号测 FREQ）时，`MAIN?`/`SOUR?`/`RES:ACT?` 都会**超时**而不是报错。`scope-ctl` 已做保护：只对已启用且源通道打开的槽位读值。

### 5.8 光标、缩放

| 参数 | SCPI | 说明 |
|---|---|---|
| 光标开关 / 功能 / 源 | `CURS1:STAT` / `FUNC` / `SOUR` | 实测 1 / PAIR / SPECNORM1（当前光标挂在频谱上） |
| 光标位置 | `CURS1:X1P` `X2P` `Y1P` `Y2P` | |
| 缩放 | `LAY1:ZOOM1:...` | **本机 `LAY1:ZOOM1:COUN?` 超时**，缩放相关未验证 |

`scope-ctl` 暂未封装光标/缩放，用 `scpi` 直接发。

### 5.9 截图、设置、系统

| 功能 | SCPI | scope-ctl | 实测 |
|---|---|---|---|
| 截图 | `HCOP:DATA?`（格式由 `HCOP:DEV1:LANG` 决定，PNG） | `screenshot` | 1920×1080，约 176 KB，0.98 s |
| 保存设置 | `MMEM:SAV '/home/storage/userData/settings/<名>.dfl'` | `settings save` | 1.47 MB/文件 |
| 恢复设置 | `MMEM:RCL '...'` | `settings recall` | |
| 列目录 | `MMEM:CAT? '<dir>'` | `settings list` | |
| 删除 | `MMEM:DEL '<file>'` | `scpi --write --i-know` | 已验证 |
| 运行控制 | `RUN` / `STOP` / `SINGle` | `run` / `stop` / `single` | |
| Autoset | `AUToscale` | `autoscale` | |
| 错误队列 | `SYST:ERR:ALL?` / `SYST:ERR:COUN?` | `errors` | |
| 命令语法 | `SYST:HELP:SYNT? 'TRIGger:MODE'` | `scpi` | **参数要加单引号**，否则超时。只对部分命令有返回 |
| 全部命令头 | `SYST:HELP:HEAD?` | 已导出 `/root/scpi_headers.txt`（3255 条） | |

---

## 6. 已踩过的坑（二次开发必读）

1. **不支持/不可用的查询 = 超时，不是错误应答。** 例如 AUTO 模式下的 `PROB6:SET:ATT:MAN?`、未启用的 `MEAS3:MAIN?`、`LAY1:ZOOM1:COUN?`、不带引号的 `SYST:HELP:SYNT?`。仪器把 `-397 Not available` 或 `-113 Undefined header` 放进错误队列，VISA 端只看到 `0xBFFF0015` 超时。**这种超时不会打坏链路**，超时后照常发下一条即可；`scope-ctl` 用 `SYST:ERR:ALL?` 探链路，只有探不通才 `viClear`。
2. **不要用阻塞式 `*OPC?` 等单次采集。** NORM 触发下等不到触发时 `*OPC?` 会一直挂到超时，接着几十次盲目 `viClear` 把 USB 管道打乱，最终出现命令被截断（错误队列里出现 `Undefined header;PROB6:SET:AT�`、`Invalid separator` 和 `-350 Queue overflow`），仪器对任何命令都不再应答。`scope-ctl single` 已改为发 `*OPC` 后轮询 `*ESR?`，超时只报告不阻塞。
3. **链路彻底哑掉时，树莓派上可以不断电救回**：`echo 0 > /sys/bus/usb/devices/1-1.1/authorized; sleep 2; echo 1 > .../authorized`（`scope-ctl recover` 自动做）。2026-09-20 实测 `viClear` ×4 无效、USB 重枚举后 `*IDN?` 立即恢复。Windows 直连时代的 PnP 禁用/启用无效，这是新链路的优势。
4. `ACQ:POIN` 在 `ACQ:POIN:MODE AUTO` 下只读；改点数必须先切 MAN（脚本已处理），用完记得 `acq --points-mode AUTO` 或 `settings restore`，否则台架采集会被截短（台架 prepare 阶段也会自己切回 AUTO）。
5. `TRIG:EVEN1:LEV<n>` 的 n 是**通道号**不是事件号；改源之后电平要写对应通道的槽。
6. `TIM:REF 50` 回读为 `50.00000000000001`，做比较时按浮点容差处理。
7. R&S VISA 默认 I/O 超时 2000 ms（`VI_ATTR_TMO_VALUE`），`rsvisa.Instrument(timeout_ms=…)` 传入的是 `viOpen` 的打开超时，**不是** I/O 超时；要改 I/O 超时必须 `viSetAttribute(vi, 0x3FFF001A, ms)`。`scope-ctl --timeout` 已封装。
8. 树莓派时区是 Europe/London（BST），截图/CSV 文件名里的时间比工位（UTC+8）慢 7 小时。改法：`timedatectl set-timezone Asia/Shanghai`（本次未改）。
9. 错误队列一旦溢出（`-350`）会丢弃旧条目，读回的字符串可能含乱码；`scope-ctl errors` 读一次即清空。

---

## 7. 常见操作配方

```bash
# 记录现场 → 调参 → 截图 → 恢复
scope-ctl settings snapshot /mnt/scope_results/before_$(date +%H%M).json
scope-ctl chan 2 --scale 200m --offset 0.7
scope-ctl tb --scale 2m
scope-ctl trig --mode NORM --source C2 --level 0.1
scope-ctl view clean
scope-ctl screenshot -f tuned
scope-ctl settings restore /mnt/scope_results/before_XXXX.json

# 平均 16 次看纹波
scope-ctl acq --type AVER --count 16
scope-ctl meas 3 on --type RMS --source C2
scope-ctl meas read
scope-ctl acq --type SAMP --count 1

# 只想看一眼数值，不想改任何东西
scope-ctl state
scope-ctl meas read

# 仪器不应答
scope-ctl info            # 先看是不是 0xBFFF0011（被占）还是 0xBFFF0015（超时）
scope-ctl recover         # viClear → USB 重枚举
```

---

## 8. 文件清单与变更

**树莓派**

| 文件 | 说明 |
|---|---|
| `/root/scope_ctl.py` | 本工具（约 900 行，仅依赖标准库 + `/root/rsvisa.py`） |
| `/usr/local/bin/scope-ctl` | 包装：`exec python3 -u /root/scope_ctl.py "$@"` |
| `/root/MXO38_SCPI_CONTROL_HANDOFF.md` | 本文副本 |
| `/root/scope_data/ctl_pretest.json` | 2026-09-20 调试前的参数快照（可作恢复基线） |

**work-pc**

| 文件 | 说明 |
|---|---|
| `F:\ZYS_HYBASE\automation\MXO38_SCPI_CONTROL_HANDOFF.md` | 本文 |
| `F:\ZYS_HYBASE\automation\pi_tools\scope_ctl.py` | 脚本备份（树莓派重装时 `scp` 回去即可） |

**Mac（AI 侧）**

| 文件 | 说明 |
|---|---|
| `~/.dsh/mcp/mxo38-scope-bench/server.mjs` | MCP：新增 `scope_ctl_*` 工具，`scope_recover_link` 改为调用树莓派 `scope-ctl recover` |
| `~/.codex/skills/mxo38-scope-bench/SKILL.md` | 技能说明 |

示波器本身：测试期间写入的所有参数已用 `settings restore` 回滚，`state` 与测试前快照零差异；测试用设置文件 `ctl_test.dfl` 已删除；测试截图 `ctl_test_20260920-191248.png` 留在 `scope_results`。

---

## 9. 验证记录（2026-09-20）

| 项 | 结果 |
|---|---|
| `state --all` 全量读取 | 205 项，0.08 s，0 超时 |
| 通道写入 | `CHAN2:SCAL 0.5→0.2→0.5`、`OFFS 0.705→0.7`，`CHAN3:STAT 0→1` |
| 时基 / 触发 | `TIM:SCAL 0.05→0.02`、`TIM:REF 0→50`；`TRIG:MODE AUTO→NORM`、源 C3→C2、斜率、`LEV2 1.66→0.1` |
| 显示 | 网格/十字线/余辉关、DOTS、屏幕消息显示与清除、`view clean` / `view default` |
| 测量 | `MEAS3` 启用为 MAX@C2 并读回 0.01695；`meas read` 五槽 |
| 采集 | `ACQ:POIN` 3846154→20000（自动切 MAN） |
| 单次采集 | NORM 无触发时 `single --wait 4` 4 s 返回 `completed=false`，链路保持健康 |
| 截图 | 1920×1080 PNG，175697 B，0.98 s，落盘 `scope_results` |
| 设置文件 | `settings save ctl_test` → 1470963 B；`list` 可见；已删除 |
| 快照恢复 | `settings restore` 100 项全部成功，之后 `state` 与快照零差异 |
| 链路救援 | `recover`：健康时 viClear 直接通过；哑链路时 USB 重枚举后恢复（见第 6 节第 3 条） |
