# 分 VL 自动测量

示波器联动入口为根目录 `START_SCOPE_BENCH.cmd`。面板支持选择单个或多个测试用例、CH1–CH8开关及10–15秒使能配置；参见 `scope_bench/README.md`。旧的分 VL 入口已归档到 `launchers/legacy/`。

脚本已经部署到 `work-pc: F:\ZYS_HYBASE\automation`。电源电压由用户手动调整，一次只运行一个 VL 组。

## 启动

在 work-pc 当前 ControlDesk 所在的桌面会话中：

1. 手动设置本组 VL 电源电压及总母线 VH=240 V。
2. 打开 `Project_PACC / Experiment_PACC`，让已下载的控制程序可运行，并在 ControlDesk 中 Go Online，保持 `Enable1=0`。
3. 双击根目录 `START_SCOPE_BENCH.cmd`，选择相同人工电源设置下的一个或多个用例。这是实际执行入口，会写参数和使能。不要在普通 SSH 会话中直接启动。
4. 本组完成后 `Enable1=0`，再手动调电源并启动下一组。脚本不控制电源、不下载或启动实时应用、不自动切换项目。

仅查看计划，不写参数：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "F:\ZYS_HYBASE\automation\core\run_test_batch.ps1" -Mode Plan -TestIds legacy_vl120
```

只读检查当前 ControlDesk 的参数路径、界面通道和测量信号列表：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "F:\ZYS_HYBASE\automation\core\run_test_batch.ps1" -Mode Check -TestIds legacy_vl120
```

命令行执行本组，在末尾加 `-Run`。MATLAB 可在相同桌面会话中通过 `system('powershell.exe ... -Run')` 调用；MATLAB 包装未单独测试。

当前 DS1202 使用 `UploadConnectedVariables` 上线方式，可以在没有任何数据集的情况下在线标定。
脚本支持这种 `DirectOnline` 参数访问，不要求创建或激活 `WorkingDataSet`。
在线 `-Check` 会检查该上下文、五个参数的标量读数和 `Enable1=0`；离线检查明确提示仅检查配置。
如果已有 WorkingDataSet，运行时仍采用原有工作数据集路径；其他缺少工作数据集的上下文会拒绝运行，避免将保存的数据集误当在线参数。

## 工况与写入内容

每个工况写入以下参数，并逐个回读验证：

| 参数 | 写入内容 |
| --- | --- |
| `Model Root/Constant3/Value` | 下表 ξ |
| `Model Root/ud_ref6/Value` | 60 |
| `Model Root/ud_ref7/Value` | 60 |
| `Model Root/phi2_deg/Value` | 下表 φ* |
| `Model Root/Enable1/Value` | 每次测量 1，结束/可捕获异常后 0 |

| 启动脚本 | 顺序工况：ξ / φ*（度） | 数量 |
| --- | --- | ---: |
| `START_VL100.cmd` | 0.20 / 115 | 1 |
| `START_VL120.cmd` | 0.80 / 118；0.35 / 123；0.50、0.51、0.52、0.53、0.54、0.55 / 180 | 8 |
| `START_VL140.cmd` | 0.80 / 123；0.85 / 116；0.45 / 120；0.30 / 102 | 4 |
| `START_VL160.cmd` | 0.85 / 119；0.55 / 117 | 2 |

既有工况保存在 `config/sweep_cases.csv`；测试用例注册在 `test_cases/catalog.json`。可把工况的 `enabled` 改为 `false` 跳过某行，但同时必须更新目录中的 `expected_cases`，否则批测器会拒绝运行。
VL 是本组的人工电源设置标签，不是脚本回读/控制的电源值。λ、Γ、改善率、P_H/P_L 是图片中的分析/预测结果，不作为控制器参数写入。
按用户更正，`ud_ref6/7=60`，模型内部乘 √2，因此实际命令峰值为约 84.8528 V；不采用截图标题的 80 V。截图中的预测结果不能因此当作当前幅值下的新测量结果。

## 时序

```text
Enable1=0
写入本行 ξ、ud_ref6/7、phi2_deg，并回读
两路录波器均启动
Enable1=1，持续约 3 秒
Enable1=0
停止两路录波器，检查并保存 MF4
从成功关闭 Enable1 起至少间隔 1 秒
下一行
```

正常测量期间主机每约 10 ms 检查录波状态。3 秒是 Windows/COM 主机时序，不是硬实时定时；参数回读、文件保存和通信有开销，所以每行周期约 4 秒加开销，不能同时做到“每 3 秒开始下一组”和“3 秒测量后关闭 1 秒”。
两路录波在 enable=1 前启动、enable=0 后停止，文件会包含少量禁能前后沿数据，以完整覆盖 3 秒使能区间。保留平台原始时间戳，两路文件可据共同时间轴对齐；不假设两个 COM Start 调用同一瞬间发生。

## 波形与文件名

已从当前布局核对：

| 文件后缀 | 当前窗口 | 三个实际信号 |
| --- | --- | --- |
| `meas1abc` | `Layout1 / Time Plotter_25` | `measa1/In1`、`measb1/In1`、`measc1/In1` |
| `meas2abc` | `Layout1 / Time Plotter_26` | `measa2/In1`、`measb2/In1`、`measc2/In1` |

均属于 `Platform(HostService)://Model Root/...`，完整配置在 `config/waveform_groups.json`。
每组文件录入三个标量通道，使用独立的临时 ControlDesk 录波器采集，不从仅显示约两周期的绘图窗口截取数据，也不清空已有测量信号列表。

文件名使用**写入后回读值**：

```text
xi_<Constant3>+<ud_ref6*sqrt(2)>_<ud_ref7*sqrt(2)>+<group>.mf4
xi_0.8+84.9_84.9+meas1abc.mf4
xi_0.8+84.9_84.9+meas2abc.mf4
```

幅值四舍五入到一位小数，因此 `60*sqrt(2)` 为 **84.9**，示例中的 84.8 属于截断值。完整未舍入参数保存在 `case.json`。

输出结构：

```text
automation\sweep_results\VL_120\run_<时间戳>_<PID>\
  plan.csv
  waveform_groups.json
  case_01_phi_118\
    xi_0.8+84.9_84.9+meas1abc.mf4
    xi_0.8+84.9_84.9+meas2abc.mf4
    case.json
  ...
  run_status.json
```

独立运行目录与工况目录防止不同 VL、φ 或重复实验覆盖同名文件。保存后核对 `LastRecordedFiles`、文件存在、MDF 文件头、长度，并记录 SHA-256。
`case.json` 保存回读值、使能请求时间、实际主机使能间隔及故障；`run_status.json` 标记整组完成与否。

## 中断与验证范围

参数回读不一致、录波器异常、掉线或保存失败时停止后续工况，并尝试先写 `Enable1=0`，随后停止临时录波器。完成后删除本脚本新增的录波器；若测量服务由脚本启动，结束后停止它。其他参数保留最后一组值。
Ctrl+C 可进入 PowerShell 的 finally 清理；强杀进程、主机断电或 COM 通信完全失效时，软件无法保证关断，仍需台架已有硬件保护。

已验证：

- Windows PowerShell 5.1 语法与计划生成；4 个 VL 组、15 个工况。
- 真正连接当前 ControlDesk 的只读 `Check`，参数与两组波形的路径、信号 Active 状态均通过；检查前后参数/平台状态和录波器数量一致。
- 无硬件 mock 测试：顺序、最小关闭等待、命名/区域设置及 6 种故障路径。运行 `tests/test_sweep.ps1` 可复验。

**未执行实际参数写入、enable=1、录波或电源操作，因此尚未实测验证在线录波分支和生成 MF4 的实际通道/时长。** 文件检查目前是结构头与存在性检查，不等于已核验所有采样值。当前 ControlDesk 离线，需由用户准备好台架后运行上述入口。

API 核对依据：工位 `C:\Temp\dsdocs\ControlDeskAutomation.pdf`（May 2023），直接变量写入 p.131，录波示例 p.148，MainRecorder p.1403–1406，Recorders p.1763，RecordingSignals p.1764–1765。录波器 `Export` 导出的是录波器配置，不是波形，故脚本通过 `BaseFileName + Start/Stop` 生成 MF4。
