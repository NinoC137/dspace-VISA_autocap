# MXO38 自动波形台架

## 多时基原生截图

界面新增 **保存多时基截图**（默认开启）和 **整图跨度（约10格）**。
默认填写 `200ms, 50ms, 10ms, 1ms, 100us`；可修改列表或关闭截图。
这里是整张放大波形图的时间跨度，对应约 `20ms/div, 5ms/div, 1ms/div, 100us/div, 10us/div`，
不是采样间隔，也不是将采集时基重新设为这些数值。所有已勾选通道一起出现在每张截图里。

流程为：一次采集冻结 → 工况关断 → HDF5 导出 → 五档原生 PNG 截图 → RMS/频谱分析。
截图调用 `HCOPy:DATA?`，在临时 SmartGrid 页中仅显示 Zoom 波形，默认围绕记录中心逐级放大。
采集时长仍为 0.5 s，截图不重新触发、不写 ControlDesk、不延长 10–15 s 使能时间；会增加关断后的保存耗时。
采用示波器原生网格并打开坐标标签，保留原通道垂直比例。顶部工具栏显示的是完整采集时基，
当前截图的 Zoom 跨度应读下方坐标及图片追加的标注栏。

每个 `scope/capture_<id>/screenshots/` 内包含：

- `01_span_200ms_CH2_raw.png` 等五张原始仪器 PNG（不改动像素）。
- 对应 `_marked.png`：只在图片下方追加标注，不覆盖波形；包含整图跨度、每格时间、实际跨度、
  相对触发点的中心位置、勾选通道、工况参数、capture ID、截图 UTC 时间。
- `index.html`：双击即可浏览本次五档截图并打开原图。
- `index.json`：实际时基、时间范围、采集时间标识、工况、各图片 SHA-256 和恢复结果；
  `manifest.json.screenshots` 同步保存完整索引。

配置示例（旧配置自动补齐，无需手改）：

```json
"screenshots": {
  "enabled": true,
  "spans_s": [0.2, 0.05, 0.01, 0.001, 0.0001],
  "center_fraction": 0.5,
  "settle_s": 0.5,
  "timeout_s": 30
}
```

`center_fraction` 是在实际记录起止点之间的位置；跨度不得超出记录，启动前会检查。
实际 Zoom 跨度受采样点量化影响，按读回值记录，容许最多两个采样间隔的差异。
波形 HDF5 与截图都保存仪器 `ACQuire:HISTory:ISODate?` 采集标识，并检查导出/截图过程中没有换记录。
截图需要一个空闲 SmartGrid 页（仪器最多四页）；结束时删除临时页，恢复原活动页及显示/网格设置。
如果截图失败，已完成的 PNG、HDF5 和分析结果仍保留，界面日志显示警告，索引状态为 `failed`；
不要将 `capture_status=complete` 理解为截图也完整，检查 `screenshots.status`。

接口依据：[R&S MXO 3 官方手册](https://www.rohde-schwarz.com/us/manual/rs-mxo-3-user-manual_78701-1591401.html)
中的 SmartGrid layout、Zoom 和 `HCOPy:DATA?`；固件 2.8.2.0 另经真实冻结记录验证。

## 2026-09-20：树莓派 USB 接入

当前默认链路为 work-pc → SSH → 树莓派 `172.16.102.132` → USB → MXO38。
树莓派通过 `/mnt/scope_results`（CIFS）把波形分块写入 PC 的
`F:\ZYS_HYBASE\automation\scope_results`；PC 完成校验、HDF5 封装和分析。
树莓派无需安装 NumPy、SciPy、HDF5 或 matplotlib，也不在 SD 卡缓存波形。

**更新后关闭原有台架窗口，重新双击 `START_SCOPE_BENCH.cmd`。** 标题应带“树莓派 USB”。
VL、10–15 秒使能时间、CH1–CH8 勾选及检查/开始/停止操作保持原样。
本次保留 PC 现有通道选择（CH2 开，其余关）；若要分析 CH1 电流，请勾选 CH1。
默认结果目录改为 `scope_results\sweeps`，也可在界面选择其他 PC 目录。
无论最终目录选哪里，树莓派的传输暂存目录始终是已挂载的 `scope_results\.transport`。

配置位于 `bench_config.json` 的 `instrument.transport="raspi_ssh"` 和 `instrument.remote`：

兼容旧配置：加载时自动补齐吞吐/重试字段；若旧窗口保存的配置丢失 `transport`，
从独立安装配置 `connection.json` 恢复树莓派连接（包括默认600秒导出超时）。显式指定的后端仍优先。
界面每次检查/运行前重新读取磁盘配置，只覆盖通道勾选、使能时间和截图选项，避免旧内存配置覆盖新连接设置。

| 字段 | 当前值 |
|---|---|
| `host` | `root@172.16.102.132` |
| `ssh_executable` | `C:\Program Files\Git\usr\bin\ssh.exe` |
| `python` | `/usr/bin/python3` |
| `agent` | `/root/mxo_bench/pi_agent.py` |
| `share_root` | `/mnt/scope_results` |
| `local_share_root` | `F:\ZYS_HYBASE\automation\scope_results` |

这台 PC 的 Windows OpenSSH 9.5 在 Python 管道中不转发输入，已实测 Git SSH 正常，故显式选择后者。
不需要更换 SSH 密钥或安装 PC VISA 驱动。`local_visa` 后端保留供将来恢复直连。
交付文档及 `/root/rsvisa.py` 保留；新增 `pi_agent.py` 使用已有 R&S VISA 库，
正确设置实际 I/O 超时、接受 `VI_SUCCESS_MAX_CNT`、64 KiB 分块读出，突破旧封装 64 MB 上限。

“检查连接”校验 SSH、网络盘写入及 PC 端 SHA-256，再只读查询仪器；仅创建并清理小型网络盘探针文件。
网络盘未挂载、映射错误或校验失败时在使能前拒绝运行。正常传输校验后删除 `.f32` 暂存，
中断的 `.partial` 留在 `.transport` 供排查；它们不算成功结果，确认不需要后可手动清理。
Pi 会话和 PC 会话均加锁；批测期间不要并行运行旧 `scope-capture` / `visa_query.py`，它们不参与台架锁。

多个勾选通道在同一次 `RUNSingle` 中采集、冻结后依次读出；不使用逐通道重新触发的 CSV 流程。
准备阶段设 `ACQuire:POINts:MODE AUTO`，避免交付脚本留下的手动点数截短 0.5 秒记录。
时间轴仍由真实采样率/分辨率与各通道 Header 交叉验证，不从显示时基猜测；单位从探头读取，不能一律标为 V。
数据到 PC 后继续计算所有通道 RMS，CH1 电流计算 FFT、10/20 kHz 的 q=5 纹波群。

默认传输超时下限 600 秒，并按每通道字节数与保守 200 kB/s 下限扩展；桥接等待也同步按通道数与重试数扩展。
这是关断后的导出预算，不会延长 10–15 秒使能时间。保持现有 6 秒稳定、0.5 秒单次采集和到时关断机制。

验证边界：软件假仪器覆盖真实子进程 RPC、双通道导出、超过 64 MB 分块、坏帧/断线/错盘校验；
真实链路仅执行身份、完整设置读取及网络盘探针，未触发新采集、未写 ControlDesk 参数或使能。
首次实际批测仍需由操作者在准备好的台架上启动。

同时修复独立 API 的旧启动问题：`capture-start` 不再把字符串 `None` 当作工作脚本；
独立采集自行推进准备/采集/导出，不等待不存在的 ControlDesk IPC 指令；已使用目录拒绝二次启动；
中止请求只写标记，不再立即杀死正在导出的子进程。分 VL 流程仍由 PowerShell 发送时序指令。

## 日常使用

在 work-pc 已打开 ControlDesk 的同一个 Windows 桌面中，双击上一级的 `START_SCOPE_BENCH.cmd`。

1. 选 VL，手动将电源调到对应电压。
2. 勾选 CH1–CH8 中需要采集的通道，选每工况 10–15 秒使能时间。
3. 点“检查连接”，检查当前 ControlDesk 项目和示波器。
4. ControlDesk 已 Online、Enable1=0 后，点“开始批测”。该按钮会按现有工况表写参数并使能。
5. 点“打开结果”查看文件。“停止”请求先关闭 Enable1，再停止录波和清理工作进程。

使能默认12秒，通道沿用上次选择。选择会保存到 `bench_config.json`；每次运行另存一份配置快照。CH1 关闭时仍可采其他通道，电流群分析自动跳过。未勾选通道在实际批测准备阶段会被关闭，勾选通道打开；只读检查不会切换它们。

默认采集设置：0.5秒、保留准备前实际采样率、Sample、单次、Auto触发、零预触发。保持原有探头标定、垂直量程、耦合与模拟带宽；电流分析要求 A 单位和 DC 耦合。准备阶段读取实际设置，不满足记录长度、采样率、点数预算或通道单位时，在使能前报错。默认每通道上限1亿点，总原始数据预算4GiB，可在JSON修改。

只读实测CH1带宽50MHz、当前采样率约192.3MSa/s，因此默认不执行原设计候选的5MSa/s降采样。约192.3MSa/s、0.5秒时，每通道原始文件约385MB，8通道约3.08GB，后处理还需要额外内存与磁盘空间。默认报告标记 `anti_alias_verified=false` 和 `probe_bandwidth_verified=false`，不把它们自动改成 true；精度仍须结合探头频响核验。多通道深存储能力以实际准备阶段读回为准。

首次安装/补装依赖运行 `SETUP_SCOPE_BENCH.cmd`。Python依赖位于本目录 `.venv`；当前树莓派后端使用 Pi 的 R&S VISA，只有 `local_visa` 后端使用 PC VISA 驱动。Tkinter 使用 Windows Python 自带模块。

旧 `START_VL*.cmd` 的无示波器3秒流程仍保留；新台架统一从 `START_SCOPE_BENCH.cmd` 进入。

已修复在线时报 `No WorkingDataSet` 的问题：当前 DS1202 的 `UploadConnectedVariables` 模式没有数据集也可在线访问参数。检查连接时会输出 `ONLINE PARAMETER CHECK OK: access=DirectOnline; Enable1=0`；不需要额外创建数据集。该分支已在当前在线会话完成只读检查，前后参数和测量状态一致；实际参数写入和整批采集仍由“开始批测”触发。

## 保存与分析

当前默认结果位于 `scope_results/sweeps/VL_*/run_*/case_*/scope/capture_*/`：

- `waveforms.h5`：仅所选通道的原始float32物理量，独立时间轴属性；不重复乘探头缩放。
- `settings_before.json` / `settings.json`：修改前、准备后设置回读。
- `config.json` / `context.json`：通道配置、工况参数请求及回读。
- `events.jsonl` / `status.json`：采集、冻结、导出、分析状态；故障还有 `error.txt`。
- `manifest.json`：原始文件SHA256、采集状态、分析状态及报告路径。
- `analysis/<版本_ID>/metrics.json`：所有通道基础DC/RMS/AC RMS/峰值，CH1增加10/20kHz频带结果。
- `spectrum.h5`：Welch PSD、完整记录PSD及Hann相干增益校正的FFT峰值幅度；非相干单bin峰值存在scalloping误差。
- `ch1_report.png`：时域显示与两群PSD。显示抽点仅用于绘图，指标使用所有样点。
- 批次根目录 `summary.csv`：每次成功采集一行，含质量标记、参数、RMS及群均方值。

文件先以`.partial`写入、回读校验和计算哈希，完成后再提交正式名称及manifest。失败不会用上一工况缓存冒充成功，已保存原始文件不受后处理错误影响。

q=5同时计算严格±250Hz和边带中点分组±275Hz（f0=50Hz）。主群指标是PSD积分的A²，合成RMS是平方和开根号；A²s不称电能J。无额外低通的总RMS对应实际采集链带宽。当前版不实现设计稿中的可选100kHz滤波指标。

质量检查包括分块DC/AC RMS变化、探头量程及仪器overload；波形变化/未知带宽会给出`review`标记，不自动调整工况或重试使能。探头30A与标称0.1V/A不足以单独证明绝对精度。

重算已有波形：面板点“重算已有波形”，选原始 `waveforms.h5`。使用文件内嵌配置，每次写新分析目录；既有manifest及批次汇总保留原批测结果，便于追溯。

## CLI

在 `F:\ZYS_HYBASE\automation` 下使用 PowerShell：

```powershell
# 只验证配置与计划，无仪器访问
.\scope_bench\.venv\Scripts\python.exe .\scope_bench\bench.py plan

# 仅示波器只读检查；不会启停采集或修改通道
.\scope_bench\.venv\Scripts\python.exe .\scope_bench\bench.py check

# 完整批测计划
.\core\run_test_batch.ps1 -Mode Plan -TestIds phase_angle_validation_01

# 实际运行，必须在 ControlDesk 所属桌面会话中
.\core\run_test_batch.ps1 -Mode Run -TestIds phase_angle_validation_01

# 独立重算 / 重建批次汇总
.\scope_bench\.venv\Scripts\python.exe .\scope_bench\bench.py analyze --file '完整路径\waveforms.h5'
.\scope_bench\.venv\Scripts\python.exe .\scope_bench\bench.py summarize --directory '完整路径\run_xxx'
```

## 实现与验证边界

每工况启动一个独立Python工作进程，VISA会话从准备保持到导出完成。本机文件消息替代设计稿候选named pipe：每次capture目录唯一，状态通过原子替换发布，主计时循环只轮询短小本地文件；同一资源用进程锁避免两个本台架实例争抢。

默认约6秒发起采集，8.5秒前确认完成，12秒关断后导出。采集未按时完成会提前关断，不延长使能等待VISA。停止/异常清理优先关断，再取消工作进程；导出一旦开始便不可中断：此时波形只存在于示波器内存与工作进程中，中途取消只会毁掉这次测量，因此停止请求在导出完成后才生效，仅用于结束本批而不丢弃该工况。阻塞导出超时可强制结束采集进程并保留部分文件。软件无法在主机断电或COM失效时保证关断，既有硬件保护仍必需。

只读check会读取SCPI错误队列，报告并消费已有错误；不会复位仪器、清空波形或改变采集设置。完成批测后仪器保持停止，保留本次通道及采集配置。

离线测试：

```powershell
.\scope_bench\.venv\Scripts\python.exe -m unittest discover -s .\scope_bench -p test_bench.py -v
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tests\test_sweep.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scope_bench\test_bridge.ps1 -Python .\scope_bench\.venv\Scripts\python.exe
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scope_bench\test_stop_bridge.ps1 -Python .\scope_bench\.venv\Scripts\python.exe
```

`test_stop_bridge.ps1` 专门守护停止语义：导出期间到达的停止请求不得中断导出（波形是唯一副本），必须在导出完成后才被记录并结束本批；已在导出前挂起的停止则直接跳过导出。界面“停止”按钮写入 `sessions\<id>\stop.json`，桥接在工况边界消费该请求并删除它，同时保留 `stop.json.used` 作为本次运行停止原因的凭据，下一次 `Run` 开始时清除二者。

连接自检（只读，不改动仪器）：

```powershell
.\scope_bench\.venv\Scripts\python.exe .\scope_bench\check_scope_link.py
```

## 传输超时与链路自愈（2026-09-18 实机排查结论）

现场批量在 `CHANnel2:DATA:VALues?` 上报 `VI_ERROR_TMO`，此后每个工况都在采集前失败。实机排查把原因拆成两条，都已修：

1. **超时预算算法有单位错误（真 bug）。** 原 `_transfer_timeout_ms` 把"按吞吐折算的秒数"直接与"毫秒"取 max，导致 38 MB 记录的预算只有约 0.28 s，必然在传输中途超时。现在统一按毫秒：`max(export_timeout_s, 2 × 字节数 / min_throughput_bytes_per_s × 1000)`，随记录长度线性放宽。
2. **一次未读完的传输会拖垮整条链路。** 传输中途超时后示波器不再回应任何查询（连 `*IDN?` 也超时），NI-VISA 与 R&S VISA 表现一致。**VISA `clear()` 可恢复**（已实测），因此驱动现在：捕获 `VisaIOError` → `clear()` → 等待 → 重试（`query_attempts` / `recover_attempts`），只有连续失败才报错并要求断电重启示波器。

同时加固：

- `snapshot()` 对**单条**元数据查询失败容错，记为 `unavailable`，不让一个工况因此失败；但**探头单位读不到时直接拒绝**，避免产出无单位波形。
- 分析通道无单位时改为**跳过频谱分析**并记录原因（`analysis_status: "skipped"`），波形与基础统计照常产出——纯示波器采集因此可用。
- `manifest.json` 增加 `link` 字段（`link_errors` / `recoveries` / `operations` / `max_attempts`），用于判断仪器稳定性。

实测还发现这条 USB 链路会**间歇性丢操作**（同批查询有的成功有的超时，成功时延中位数 0.8 ms），仪器接在 Genesys Logic Hub（`VID_05E3`）之后，且各 USB 设备此前都开着选择性挂起。已用 `disable_usb_suspend.py` 关闭示波器及上游 Hub 的选择性挂起；若可靠性仍低，先换线并直插主机后置 USB 口（绕过 Hub）。

**重要：批量中出现 `VI_ERROR_TMO` 后的处置。** 曾观察到一次大块 `CHANnel<n>:DATA:VALues?` 传输在收到 IEEE 块头之后一个字节数据都没到达，随后该 MXO38 对任何 SCPI 查询都不再应答（`*IDN?` 也超时）。此状态跨进程持续，NI-VISA 与 R&S VISA 都不通则说明是仪器侧而非软件侧；禁用/启用设备（`Disable-PnpDevice`/`Enable-PnpDevice`，PnP 显示 Status OK）也无法恢复。此时必须**给示波器断电重启**，然后用上面的自检确认 `*IDN?` 与 `CHANnel2:DATA:HEADer?` 正常，再重新开始批测。带着未响应的示波器重跑，每个工况都会在采集前的 `snapshot()` 阶段失败，看起来像“一开始就报错”。

以上使用软件假仪器/假Rig，不连接或使能变换器。真实单次采集、工况联动及测量精度需在准备好的台架上验收；配置/单元测试通过不能替代硬件验收。

2026-09-18 已验证：本地及work-pc的11项Python测试；Windows PowerShell原批测与新增示波器故障路径；实际子进程启动、空格路径、冻结/导出消息及退出码；880×600/800×540界面布局、通道开关、忙碌/停止状态。通过同用户临时桌面任务完成界面截图与ControlDesk+示波器只读检查，任务已删除。当时ControlDesk CalibrationState=0（离线），没有执行Run或RUNSingle。既有三个PS脚本备份在 `automation/backup_before_scope_20260918`。
