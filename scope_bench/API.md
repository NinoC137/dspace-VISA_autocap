# MXO38 台架 API（供 AI agent 调用）

2026-09-20：API 子命令不变，`bench_config.json` 默认改为 `raspi_ssh` 后端。
PC 的 `scope_api.py` 经 Git SSH 调用 Pi `/root/mxo_bench/pi_agent.py`，使用 Pi 的 USB/R&S VISA，
原始数据经 `/mnt/scope_results` 回写 PC 网络盘，再在 PC 封装 HDF5 和分析。
默认还在冻结记录上保存五档原生 Zoom 截图；配置见 `README.md` 的 `screenshots`。
新增进度状态 `screenshots`，位于 `exporting` 与 `analyzing` 之间。
每次采集新增 `screenshots/index.html`、`index.json` 和原始/标注 PNG。
截图失败不丢弃原始波形；`manifest.screenshots.status=failed`、最终状态的 `warnings` 会明确报告。
`ping`/`state` 会校验网络盘映射（小型探针文件），不会启停采集。完整配置与运维见 `README.md`。
旧 Windows USB 重枚举/恢复脚本不适用于树莓派后端。

本目录的 `scope_api.py` 是**唯一的 agent 入口**，把示波器操作封装成"一次调用一个 JSON"的形式；`scope_mcp/server.mjs`（本机 macOS）把它包装成 MCP server，供任意 MCP 客户端调用。

## 安全边界（务必先读）

- `scope_api.py` **只操作示波器**：读状态、采集一次、导出波形、重算指标。
- 它**绝不写入 ControlDesk 参数**、**绝不写 Enable1**、**绝不动录波器**。变换器的使能只由 `run_parameter_sweep.ps1` 负责。
- **采集前必须由操作员把被测对象断电。** 可用 `--interlock` 交给台架强制校验：给出一个 shell 命令，退出码非 0 就拒绝采集。

## 在工位上直接调用

```powershell
cd F:\ZYS_HYBASE\automation
.\scope_bench\.venv\Scripts\python.exe -u .\scope_bench\scope_api.py ping
```

参数与子命令：

| 子命令 | 作用 | 关键参数 |
|---|---|---|
| `ping` | 确认示波器应答 SCPI，返回 IDN | — |
| `state` | 只读状态：通道、探头单位、耦合、采样率、记录长度 | — |
| `raw` | 执行**一条只读查询** | `--command CHANnel2:SCALe?`（写操作会被拒绝） |
| `capture` | 准备一次采集目录，立即返回 | `--directory` `--channels CH2` `--unit V` `--record-duration-s` `--sample-rate-hz` `--export-timeout-s` `--analysis-channel` `--interlock` |
| `capture-start` | 后台启动已准备好的采集 | `--directory` `--workdir` |
| `capture-status` | 查询进度：starting/prepared/acquiring/frozen/exporting/screenshots/analyzing/complete/failed | `--directory` |
| `capture-abort` | 请求中止（导出中不中断，见下） | `--directory` `--workdir` |
| `captures` | 列出历史采集及指标、sha256 | `--directory` `--limit` |

每个子命令向 stdout 输出**恰好一个 JSON 对象**，退出码 0 成功 / 1 失败；诊断信息走 stderr。

### 采集流程

```powershell
$py = ".\scope_bench\.venv\Scripts\python.exe"
$api = ".\scope_bench\scope_api.py"
$root = "F:\ZYS_HYBASE\automation\scope_results"

# 1. 准备（不碰仪器）
$r = & $py -u $api capture --directory $root --channels CH2 --unit V --record-duration-s 0.5 --analysis-channel CH1 | ConvertFrom-Json
# 2. 后台开跑（立刻返回，不占用会话）
& $py -u $api capture-start --directory $r.directory --workdir "$root\_sessions\$($r.capture)"
# 3. 轮询
& $py -u $api capture-status --directory $r.directory
```

产物在 `$r.directory`：`waveforms.h5`、`manifest.json`（含 sha256 与指标）、`settings.json`、`worker.log`。

### 关键约束

- **通道必须配置单位**（`A`/`V`/`W`）：波形会带单位写入 HDF5 属性。用 `--unit V` 可以按次覆盖，不改动 `bench_config.json`。未配置单位时 API 会直接拒绝，而不是产出无单位数据。
- **分析通道若没有单位，频谱分析会跳过**，manifest 记为 `analysis_status: "skipped"` 并给出原因；波形与基础统计仍然完整。
- **导出不可中断**：波形只在示波器内存与工作进程里各有一份，中途取消等于毁掉这次测量。`capture-abort` 会等导出结束再停。
- 记录长度受 `bench_config.json` 的 `analysis`/`acquisition` 约束（至少覆盖两个 Welch 窗、采样率需覆盖载波群）。

## 示波器链路故障与恢复

这条 USB 链路会**间歇性丢操作**，且一次未读完的传输会让示波器"变哑"（后续所有查询都超时）。驱动已内建自愈：

- 每次查询失败后用 VISA `clear` 中止挂起传输并重试（`instrument.query_attempts`，默认 3）。
- 恢复尝试 `instrument.recover_attempts`（默认 5）次，间隔递增。
- 每次采集的 `manifest.json` 里带 `link` 字段：`{link_errors, recoveries, operations, max_attempts}`——用它判断仪器到底有多不稳。
- 只有连续失败才报错，并明确要求**给示波器断电重启**。

排查工具（只读 / 主机侧）：

```powershell
# 链路自检：*IDN? 应答时延、60 次查询可靠性、DATA:HEADer?
.\scope_bench\.venv\Scripts\python.exe .\scope_bench\check_scope_link.py

# 主机侧 USB 重新枚举（示波器真正卡死时无效，只能断电重启）
powershell -File .\scope_bench\recover_scope_link.ps1

# 关闭示波器及其上游 Hub 的 USB 选择性挂起（需管理员）
.\scope_bench\.venv\Scripts\python.exe .\scope_bench\disable_usb_suspend.py

# 实测传输吞吐，用于核定超时预算
.\scope_bench\.venv\Scripts\python.exe .\scope_bench\measure_throughput.py
```

## MCP server

`scope_mcp/server.mjs` 运行在 macOS 上，通过 SSH 调用工位上的 `scope_api.py`，因此**工位不需要开放任何端口**。工具名形如 `mcp__mxo38__scope_ping`：

| 工具 | 说明 |
|---|---|
| `scope_rig_info` | 报告 SSH 主机、解释器、采集根目录等路径 |
| `scope_ping` | 只读：示波器是否应答 |
| `scope_state` | 只读：当前通道/采集配置 |
| `scope_query` | 只读：执行一条 SCPI 查询 |
| `scope_capture` | 启动一次采集，立即返回采集目录 |
| `scope_capture_status` | 只读：采集进度与指标 |
| `scope_capture_abort` | 中止采集（导出中不中断） |
| `scope_captures` | 只读：历史采集列表 |
| `scope_fetch_file` | 把工位文件取回本机（用于读 waveforms.h5） |
| `scope_recover_link` | 主机侧 USB 重新枚举（尽力而为） |

环境变量可覆盖默认值：`SCOPE_SSH_HOST`、`SCOPE_RIG_ROOT`、`SCOPE_RIG_PYTHON`、`SCOPE_API`、`SCOPE_CAPTURE_ROOT`、`SCOPE_WORK_ROOT`、`SCOPE_TOOL_TIMEOUT_MS`。

自检（不需要 agent）：

```bash
node ~/.dsh/mcp/mxo38-scope-bench/server.mjs   # stdio，等 MCP 客户端握手
```
