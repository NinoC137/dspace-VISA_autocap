# MXO38 远程面板（Mac 侧上位机）

本机浏览器里的示波器控制面板：远程调整 R&S MXO38 的通道/探头（使能、单位 V/A、每格、偏置、耦合、带宽、反相、探头衰减）、时基、采集、触发，以及连续 / 停止 / 单次采集；附带屏幕截图预览、测量读数、参数快照与恢复、撤销上一步、原始 SCPI 控制台。

| 项 | 内容 |
|---|---|
| 版本 | v0.1.0，2026-09-21 |
| 链路 | 浏览器 → 本机 `127.0.0.1:8038`（`server.mjs`）→ ssh 复用连接 → 树莓派 `root@172.16.102.132` `scope-ctl --json` → USB VISA → MXO38 |
| 依赖 | 仅 Node ≥ 18（Homebrew `/opt/homebrew/bin/node`）；到树莓派的 ssh 免密（已配置） |
| 前置文档 | `../MXO38_SCPI_CONTROL_HANDOFF.md`（参数 ↔ SCPI 对照、坑）、`../RASPI_MXO38_DELIVERY.md` |
| 同源工具 | `~/.dsh/mcp/mxo38-scope-bench/server.mjs` 的 `scope_ctl_*` MCP 工具，与本面板调用的是同一个树莓派脚本 `/root/scope_ctl.py` |

## 启动

```bash
# 方式一：Finder 里双击 start.command
# 方式二：终端
cd "/Volumes/1TBWDBlue/文稿/Claw-Workspace/02-Engineering/controldesk_automation/mxo38_panel"
node server.mjs            # 启动后自动打开 http://127.0.0.1:8038/
node server.mjs --no-open  # 不自动开浏览器
```

Ctrl-C 停止。可用环境变量：`SCOPE_PI_HOST`（默认 `root@172.16.102.132`）、`PANEL_PORT`（默认 8038）、`PANEL_BIND`（默认 `127.0.0.1`，只监听本机）。

首次调用要建立 ssh 主连接（约 1–4 s），之后每次操作 0.3–0.6 s（其中仪器本身约 0.03–0.1 s，其余是树莓派上 Python 启动和 WiFi 往返）。

## 面板布局

- **页眉**：仪器 IDN / 固件；三个运行键 **连续 / 停止 / 单次**（单次可设最长等待秒数，正常触发模式下等不到触发会报"未完成"而不是卡死）；采集计数（增长 = 仪器在跑）；右侧链路状态、台架是否占用、网页查看器是否开着、USB 是否识别。
- **工具栏**：刷新、自动刷新间隔（默认 15 s，台架采集进行中自动跳过）、保存快照 / 恢复快照、撤销上一步、界面预设、错误队列、Autoset、链路救援。
- **通道与探头**：8 行，每行开关、单位 V/A、每格、偏置、位置、耦合、带宽、反相、探头衰减。通道色块与仪器信号栏一致，关闭的通道显示为空心。
- **仪器屏幕**：点"截图"取回 1920×1080 PNG 预览（约 1.5 s）；"存到共享盘"写到 `F:\ZYS_HYBASE\automation\scope_results\panel_<时间>.png`（时间为树莓派本地时间，Europe/London）。
- **时基 / 采集 / 触发 / 测量读数**：与 `scope-ctl tb / acq / trig / meas read` 一一对应。
- **操作记录**：每条写操作显示 SCPI 头、`旧值 → 新值`、仪器错误队列；下方原始 SCPI 输入框（`?` 结尾为查询；写命令需勾选"允许写命令"）。

## 编辑规则

- 输入框改完 **按回车** 才写入仪器，改动未提交时边框变琥珀色，Esc 放弃。下拉框、开关、勾选框改动立即写入。
- 数值支持 SI 后缀：`500m`、`20u`、`50M`；显示时同样用工程记法（`19.23 MSa/s`、`52 ns`）。
- 每次写入后面板只相信仪器回读的 `after` 值；写入失败（如触发电平超量程 `-222 Data out of range`）会原样显示仪器错误。
- **撤销上一步**：把上一组写操作的 `before` 值原样写回（经 `scope-ctl scpi --write`）。恢复快照或 Autoset 之后撤销栈清空。
- 探头衰减填数字会把该通道探头衰减模式切成手动（MAN）；想回自动，在 SCPI 控制台发 `PROB<n>:SET:ATT:MODE AUTO`。
- 记录长度写入会把 `ACQ:POIN:MODE` 切到手动；台架采集前记得改回自动，否则采集被截短。

## 安全边界

- 面板只碰示波器，不涉及 ControlDesk 参数、`Enable1` 或录波器。
- **VISA 独占**：面板内部把所有树莓派调用排成一条队列串行执行，不会自己撞自己。但它与台架采集（`pi_agent.py`）之间没有锁：每次写操作前先在树莓派上 `pgrep` 采集进程，发现在跑就拒绝并显示原因；自动刷新也会跳过。确实需要时勾选页眉出现的"忽略台架占用，强制写入"。
- 面板不传 `--i-know`，所以 `*RST`、`SYST:PRES`、`SYST:SHUT`、`MMEM:DEL`、`CAL` 之类在 `scope-ctl` 那一层被拦下。Autoset、恢复快照、链路救援都要先在浏览器里确认。
- 改参数前建议先 **保存快照**（`/root/scope_data/panel/snap_<时间>.json`，树莓派本地盘）。2026-09-21 自测：改时基 / 开关 CH3 / 平均次数 → 恢复快照 → 与快照逐项比对 163 项零差异。`ctl_pretest.json` 是 2026-09-20 交付前的基线，也在下拉框里。
- 日志：每条写操作追加到 `logs/panel-YYYYMMDD.jsonl`（含 before/after、错误队列、是否被拒绝），可用于复现实验现场。

## HTTP 接口（供脚本或 AI 调用）

| 方法与路径 | 作用 |
|---|---|
| `GET /api/state[?force=1]` | 台架占用探测 + `scope-ctl state --all`；台架忙时 `state` 为 null |
| `POST /api/ctl` `{"args":["chan","2","--scale","200m"], "force":false, "confirm":false}` | 任意 scope-ctl 子命令（白名单：info state chan tb acq run stop single trig display view meas screenshot settings scpi errors recover autoscale） |
| `POST /api/undo` `{"items":[{"header":"TIM:SCAL","value":"0.05"}]}` | 写回给定值 |
| `GET /api/screenshot` | 返回 PNG（树莓派临时文件取回即删） |
| `POST /api/screenshot/save` `{"prefix":"panel"}` | 截图存共享盘 |
| `GET /api/snapshots` / `POST /api/snapshot` `{"tag":"..."}` / `POST /api/restore` `{"path":"..."}` | 快照列表 / 保存 / 恢复（只接受 `/root/scope_data/panel/*.json` 与基线） |
| `GET /api/queue` | 队列深度与当前执行项 |

例：

```bash
curl -s -X POST http://127.0.0.1:8038/api/ctl -H 'Content-Type: application/json' \
  -d '{"args":["trig","--mode","NORM","--source","C2","--level","0.1"]}'
```

## 排障

| 现象 | 处理 |
|---|---|
| 页眉红点、"连不上树莓派" | `ssh root@172.16.102.132 true` 是否免密可通；WiFi / 树莓派电源 |
| `0xBFFF0011 Insufficient location information` | 别的进程占着仪器：网页查看器（`scope-web status`）、台架采集、另一个 scope-ctl。面板页眉会显示查看器状态 |
| `0xBFFF0015` 超时反复出现 | 点"链路救援"（viClear → USB 重枚举），无效再给示波器断电 |
| 单次采集报"未在等待时间内完成" | 正常触发模式下没等到触发；用"强制触发一次"、改自动触发，或加长等待 |
| 端口 8038 被占 | `PANEL_PORT=8039 node server.mjs` |
| 退出后 ssh 主连接残留 | 面板退出时会 `ssh -O exit`；手动清理 `ssh -O exit -o ControlPath=/tmp/mxo38-panel-ssh-%C root@172.16.102.132` |

## 文件

| 文件 | 说明 |
|---|---|
| `server.mjs` | 本机 HTTP 服务 + 树莓派调用、队列、台架守卫、命令白名单、日志 |
| `public/index.html` `public/app.js` `public/style.css` | 前端（无框架、无构建） |
| `start.command` | 双击启动 |
| `logs/` | 写操作日志（自动创建） |
