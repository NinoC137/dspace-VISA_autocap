# dSPACE MicroLabBox（DS1202）自动化测试使用手册

> 适用环境（已在工位 `172.16.102.207` 上核实）：MATLAB R2022a（`D:\MATLAB\R2022a`，启动时自动加载 dSPACE RCP/HIL 2023‑A、RTI1202 23.1）、ControlDesk 2023‑A、MicroLabBox（DS1202）、工作目录 `F:\ZYS_HYBASE`。
> 配套文件（本目录）：`DspaceRig.m`（MATLAB→ControlDesk 封装类）、`dspace_selftest.m`（首次联调）、`exp9_auto_dspace.m`（exp9 脚本硬件版样例）。三个文件也已复制到工位 `F:\ZYS_HYBASE\automation\`，并通过了 `checkcode` 语法检查；`DspaceRig.loadRecording/getSignal` 已用工位上真实的 ControlDesk 录制文件验证。**COM 在线部分尚未在 PACC 台架上实跑**，请按第 9 节顺序联调。

---

## 0. 三句话结论

1. **Simulink 不"运行"dSPACE**。Simulink + RTI 只负责把模型编译成实时应用（`.rta`）并下载到 MicroLabBox；下载完成后模型在 MicroLabBox 的处理器上**自主实时运行**，与 Simulink 再无关系。所以硬件上没有 `sim()` 的对应物——"在 Simulink 中启动 dSPACE"= 在带 RTI 块的模型上 **Ctrl+B**（或 `rti_build2`）。
2. **MATLAB 脚本控制硬件走 ControlDesk 的 COM 自动化接口**：`app = actxserver('ControlDeskNG.Application')`，然后用它写参数（替代 `set_param`）、定时录制并导出 `.mat`（替代 `sim` + Scope）。这是 dSPACE 官方支持的 M 脚本方式（ControlDesk Automation 手册 p.219）。老式 MLIB/MTRACE 库在工位上**没有安装**。
3. **四个 auto_sim 脚本的改法是"分析层不动、只换模型层"**：`load_system/set_param/sim/Scope→工作区` 这一层换成 `DspaceRig` 的 `openExperiment/setParam/record/getSignal`，`localAnalyzeCase` 以下原样复用。但有两条硬约束：负载电阻和直流源电压不能软件设定；CPU 采样是 10 kHz（= 开关频率），**开关纹波指标（fsw/2fsw 载波群）无法从 ControlDesk 数据得到**，需要示波器或 FPGA 采集（第 7 节）。

---

## 1. 你工位上的现状（已核实）

| 项目 | 现状 |
|---|---|
| 硬件模型 | `F:\ZYS_HYBASE\PACC_Drive.slx`（CPU 模型，RTI1202）、`PACC_FPGA_Drive.slx`（FPGA 模型，RTI FPGA/Xilinx）。`PACC_model.slx` 是**仿真模型**，与硬件模型块名不同 |
| 已生成的实时应用 | `F:\ZYS_HYBASE\pacc_drive.rta`（可执行）、`pacc_drive.sdf`（系统描述，ControlDesk 加载入口）、`PACC_Drive.trc`（变量描述：块名→地址→可写/只读）、`.map` |
| 构建目录 | `F:\ZYS_HYBASE\PACC_Drive_rti1202\`（C 代码、`.mk`、`.trc.part`） |
| ControlDesk 工程 | `F:\ZYS_HYBASE\Project_PACC\Project_PACC.CDP`，实验 `Experiment_PACC`，变量描述 `Variable Descriptions\pacc_drive.sdf`，布局 `Layout1.lay` |
| 采样 | `sampling_period[0] = HostService = 0.0001 s`（10 kHz）；CPU 任务 `Timer Task 1` 由 FPGA 中断触发 |
| COM 接口 | `ControlDeskNG.Application`（CLSID `{098707C9-…}`）、`ControlDeskNG.MC3System`、`dSPACE.MeasurementDataAPI.Measurements` 均已注册 |
| 命令行工具 | `C:\Program Files\dSPACE ControlDesk 2023-A\Main\bin\DSSigConv.exe`：`.mf4 → .mat` 转换，已验证 |
| Python | `C:\Program Files\Python39\python.exe` 带 pywin32/pythonnet（备用通道） |

---

## 2. 联动机制：三个软件各管什么

```
 Simulink (PACC_Drive.slx + RTI1202 blocks)
   │  Ctrl+B  ==  rti_build2('PACC_Drive','Command','CML')
   │  C = 生成 C 代码 (Simulink Coder + rti1202.tlc)
   │  M = make：交叉编译/链接 → pacc_drive.rta  (同时生成 .sdf/.trc/.map)
   │  L = load：通过以太网下载到 MicroLabBox，并自动 Start
   ▼
 MicroLabBox (DS1202)  ← 模型在这里以固定步长实时运行，独立于 PC
   ▲  HostService 0.1 ms：主机按变量地址读/写内存
   │
 ControlDesk (Project_PACC / Experiment_PACC)
   读取 pacc_drive.sdf → 知道每个变量的地址/类型/可写性
   Go Online = Start Online Calibration → 可以改参数、测量、录制
```

- **可在线改的量**：TRC 中 `flags: PARAM` 的变量，即 Constant 的 `Value`、Gain 的 `Gain`、Saturation 的上下限等。改这些**不需要重新编译**。
- **需要重新 Ctrl+B 的量**：模型结构、采样时间、信号线、Scope 增删、任何非可调参数（例如写死在 MATLAB Function 里的常数）。
- **FPGA 部分**：`PACC_FPGA_Drive` 需单独用 RTI FPGA 流程综合（Vivado，耗时长）。只要 FPGA 设计不变，CPU 模型 Ctrl+B 时会自动带上已生成的比特流，不必重新综合。
- 文件角色：`.rta` 给硬件用；`.sdf` 给 ControlDesk 用（它内部引用 `.rta` 和 `.trc`）；`.trc` 是"变量字典"，写脚本时查它最可靠。

---

## 3. 手动流程：在 Simulink 里"启动"dSPACE

### 3.1 前置检查（每次开机一次）
1. MicroLabBox 上电，网线接主机；在 ControlDesk 的 **Platforms/Devices** 或 dSPACE **Platform Manager** 里确认设备状态为 Connected。
2. 只用 **R2022a** 这份 MATLAB（`D:\MATLAB\R2022a`）。它启动时会打印 `RTI Platform Support RTI1202 activated`，说明 dSPACE 工具链挂上了；其它版本的 MATLAB 无法构建。
3. `cd F:\ZYS_HYBASE`（构建产物会写到当前目录）。

### 3.2 构建并下载（改了模型之后）
```matlab
cd F:\ZYS_HYBASE
open_system('PACC_Drive');
% 方式 A：模型窗口按 Ctrl+B
% 方式 B：命令行（可放进脚本）
[err, msg] = rti_build2('PACC_Drive', 'Command', 'CML');   % C 生成代码, M 编译, L 下载
if err, error(msg); end
```
- 首次或不确定时，检查 Configuration Parameters：Solver = Fixed‑step；Code Generation → System target file = `rti1202.tlc`；**RTI load options → "Load application after build" 已勾选**（默认勾选，这就是"下载并启动"的开关，RTI Implementation Guide p.223）。
- 构建成功后 MicroLabBox 上的应用**立刻处于 Running**，`simState = RUN`。不需要、也没有"在 Simulink 里按 Run"这一步。
- 构建期间旧的 `.rta/.map/.trc` 会被删除；构建时最好先在 ControlDesk 里 **Go Offline**，构建完再 Go Online（ControlDesk 会提示变量描述已更新，选择重新加载）。

### 3.3 只下载、不重新编译
```matlab
rti_build2('PACC_Drive', 'Command', 'L');     % 把现有 pacc_drive.rta 重新下载并启动
```
或在 ControlDesk 中：Platform 右键 → **Load Real‑Time Application** → 选 `pacc_drive.sdf`。脚本里对应 `rig.loadApplication('F:\ZYS_HYBASE\pacc_drive.sdf')`。

### 3.4 在 ControlDesk 中操作
打开 `Project_PACC` → 激活 `Experiment_PACC` → 工具栏 **Go Online**（Start Online Calibration）→ 布局 `Layout1` 上改参数/看曲线 → **Start Measuring** / **Start Recording**。这一步就是你已经会的部分；下面的自动化只是把这些按钮换成 COM 调用。

### 3.5 常见问题
- `rti_build2` 报找不到板卡：Platform Manager 里没注册或 IP 不通；ControlDesk 占用了板卡的独占访问（先 Go Offline）。
- 构建后 ControlDesk 变量全变灰：`.sdf` 已更新但实验里还挂着旧的变量描述，右键 Variable Description → Reload。
- 改了 Constant 的值却要重新构建：说明该参数被 Simulink 优化掉了（Inline parameters），在 Configuration Parameters → Code Generation → Optimization 里把 **Default parameter behavior = Tunable**，或把该 Constant 的值改为工作区变量并在 RTI 中声明可调。

---

## 4. MATLAB 自动化通道的选择

| 通道 | 在工位上的状态 | 结论 |
|---|---|---|
| ControlDesk COM 自动化（`actxserver('ControlDeskNG.Application')`） | ProgID 已注册；手册明确支持 M 脚本 | **首选**。参数读写、测量、录制、导出、下载 .rta 全覆盖 |
| ASAM MCD‑3（`ControlDeskNG.MC3System`，示例 `Demos\MC3\Matlab\Measurement\DsMC3_MeasurementDemo.m`） | 已注册，示例可用 | 只做测量时可用，写参数不如 COM 直接 |
| ASAM XIL API（MAPort） | 装了标准程序集，但无 MAPort 实现清单（`C:\ProgramData\ASAM\XIL\Implementation` 不存在）；示例只有 C# | 不建议 |
| MLIB/MTRACE | **未安装** | 不可用 |
| Python + win32com | Python39 有 pywin32 | 备用；对象模型与 MATLAB 完全一致 |

---

## 5. ControlDesk COM 对象模型速查（`DspaceRig` 内部就是这些调用）

| 目的 | 调用 | 手册页 |
|---|---|---|
| 连接/启动 ControlDesk | `app = actxserver('ControlDeskNG.Application')` | p.219 |
| 打开工程、激活实验 | `root.Projects.Item('Project_PACC').Open()`；`app.ActiveProject.Experiments.Item('Experiment_PACC').Activate()` | p.120–121 |
| 实验里的平台 | `plt = app.ActiveExperiment.Platforms.Item(0)`；`plt.Name`（本工位为 `Platform`） | p.757 |
| 下载 .rta | `reg = app.PlatformManagement.Platforms.Item(k)`（`reg.Type==30` 即 DS1202）→ `reg.LoadRealtimeApplicationControlled(sdf, true, true, 1)` | p.1789 |
| 启/停实时应用 | `rta = reg.RealTimeApplications.Item(0)`；`rta.Start()/Stop()`；`rta.State`（1=Running, 2=Stopped） | p.622, p.391 |
| Go Online / Offline | `app.CalibrationManagement.StartOnlineCalibration()` / `StopOnlineCalibration()`；`plt.CalibrationState==1` | p.127 |
| 写参数前 | `plt.ActiveVariableDescription.DataSets.WorkingDataSet.Activate()` | p.131 |
| 读/写标量 | `v = plt.ActiveVariableDescription.Variables.Item('Model Root/phi2_deg/Value'); v.ValueConverted = 120` | p.130–131 |
| 测量信号列表 | `app.MeasurementDataManagement.MeasurementConfiguration.Signals.RemoveAll(); …Signals.Add('Platform(HostService)://Model Root/PHV/In1')` | p.144, p.1455 |
| 定时录制 | `rec = app.MeasurementDataManagement.MainRecorder; rec.BaseFileName='…\case.mf4'; rec.ExportFullPath='…\case.mat'; rec.StopCondition.Enabled=true; .Type=0(TimeLimit); .TimeLimit=0.5; rec.Start(false,true)`；`rec.State==0` 表示结束 | p.147, p.1403 |
| mf4 → mat | `rec.ExportFullPath`（自动）；或 `Measurement.Export('x.mat', true)`；或命令行 `DSSigConv.exe in.mf4 out.mat` | M&R 手册 p.200 |

变量路径规则：`Model Root/<块名>/<参数或端口>`。Constant → `/Value`，Gain → `/Gain`，Scope 输入 → `/In1, /In2…`，块输出 → `/Out1`。测量信号定义 = `平台名(raster)://路径`。不确定时在 ControlDesk 的 Variables 面板右键 → Copy，粘贴出来就是路径。

导出的 MAT 结构（已验证）：`S.<文件名>.X(k).Data` 时间轴（`Raster='HostService'`），`S.<文件名>.Y(i).Name='In1'`、`.Path='Model Root/PHV'`、`.XIndex`、`.Data`（1×N double）。`DspaceRig.loadRecording` 把它整理成 `rec.signals(i).key='Model Root/PHV/In1'`，`DspaceRig.getSignal(rec, key)` 返回与 auto_sim 里 `localNormalizeSignal` 同构的 `struct(available, t, y)`。

---

## 6. 硬件模型 `PACC_Drive` 的变量清单（摘自 `PACC_Drive.trc`）

**可写参数（`flags: PARAM`）**——即脚本里 `setParam` 的目标：

| 路径 | 含义（推测，请核对模型） |
|---|---|
| `Model Root/phi2_deg/Value` | 载波移相角，度 —— 对应仿真 `phase_shift` |
| `Model Root/ud_ref/Value`, `ud_ref1/Value`, `ud_ref2/Value`, `ud_ref3/Value` | d 轴电压参考（四个：两 AC 口 × 可能的两套控制）—— 对应仿真 `V-F Control1/ud_ref`、`V-F Control2/ud_ref` |
| `Model Root/uq_ref*/Value` | q 轴电压参考 |
| `Model Root/enable/Value`, `Enable1/Value` | PWM/环路使能 |
| `Model Root/f1…f6/Value` | 频率或标志常数 |
| `Model Root/Constant2/3/5/21/Value` | 未命名常数——**ξ（power_distribute_factor）和 AC 相位差很可能在这里**，需你打开 `PACC_Drive.slx` 确认 |
| `Model Root/*_offset/Value`（`idc1_offset`, `mea_a1_offset`, `u1_offset`, `udc1_offset` …） | 传感器零漂校准 |
| `Model Root/Sat_phi/LowerLimit, UpperLimit` | 移相角限幅 |

**可测信号（Scope 块，`flags: READONLY`）**——即 `configureMeasurement` 的输入：
`PHV/In1`, `PLV/In1`, `Pac1/In1`, `Pac2/In1`, `PQ1/In1…`, `PQ2/…`, `idc1/In1`, `idc2/In1`, `udc1/In1`, `udc2/In1`, `u1…u4/In1`, `uc1/uc2`, `Ic1/In1`, `Ic2/In1`, `measa1/measb1/measc1/measa2/…`, `phi_opt/In1`, `phi_output/In1…`, `phi_search/In1`, `gamma/In1…`, `Vabc1/Vabc2`, `Vdq1/Vdq2`, `Vt1/Vt2`, `w1/w2`, `Scope1…Scope56`, `tabc11…tabc22`, `RMS2`, `RMS5`。

**仿真脚本参数 → 硬件的映射表（需要你补全 TODO）：**

| auto_sim 里的量 | 仿真块 | 硬件 | 状态 |
|---|---|---|---|
| `phase_shift`（deg） | `phase_shift` Constant | `Model Root/phi2_deg/Value` | 名称吻合，需确认单位/符号 |
| `power_distribute_factor` ξ | `power_distribute_factor` | 无同名块 | **TODO**：在 `PACC_Drive.slx` 找到 ξ 所在 Constant |
| `AC_PhaseShiftAngle`（rad） | 同名 Constant | 无同名块 | **TODO** |
| `ud_ref`（AC1/AC2） | `V-F Control1/2/ud_ref` | `ud_ref*/Value` 之一 | **TODO** 确认哪个对应哪个口 |
| `L1/L2` 负载电阻 | `Resistance` | 负载箱 | **只能人工**（或用可编程负载 + VISA） |
| `DC Voltage Source2/3` 幅值 | `Amplitude` | 直流源 | **只能人工**（或可编程电源 + VISA） |
| `TransportDelay`（旧版载波移相） | — | 由 FPGA 实现 | 不适用 |

最快的查法：跑 `dspace_selftest.m`，它会把所有 `…/Value` 变量导出到 `writable_values.csv`；再对照 `PACC_Drive.slx` 里各 Constant 的连线即可确定。

---

## 7. 物理与采样约束——决定哪些指标能搬到硬件

1. **采样率**：ControlDesk 能记录的最高速率 = HostService raster = **10 kHz**，而 `fsw = 10 kHz`。每个开关周期只有一个采样点，且 CPU 采样通常与 PWM 同步（采到的是周期平均值），因此 **`localRippleMetrics`/`localBandEnergy` 中 fsw、2fsw 载波群 RMS、DC 母线纹波、开关级 `AC1Current_sw` 等指标无法从 ControlDesk 数据得到**。仿真里 `analysisFs = 200 kHz` 没有硬件对应物。
   - 替代方案 A（推荐、改动最小）：示波器采 `HL_current`、AC 电流（≥1 MS/s），MATLAB 用 Instrument Control Toolbox（`visadev` + SCPI）拉波形，喂给原来的 `localAnalyzeCase`。把它做成 `localAcquireScope(cfg)`，返回与 `getSignal` 同构的 `struct(available,t,y)`。
   - 替代方案 B：在 FPGA 模型里加高速采集缓冲（RTI FPGA 的 buffer/burst 传输），工作量大。
   - 方案 C：只搬"CPU 域"指标——功率 `PHV/PLV/Pac1/Pac2`、平均电流、`phi_opt/gamma` 等硬件已算好的量。exp8/exp9 的观测器/J 曲线若已在硬件模型中实现，直接录 `phi_opt`、`phi_search` 即可；J 曲线整条向量（仿真 Scope83 1×544）硬件模型没有输出。
2. **不可软件设定**：负载电阻、直流源电压。exp10 扫 `VL ∈ {100,120,140,160}` 只能"人工换一档 → 脚本扫完 ξ → 再换"，脚本里保留 `cfg.dcLowVoltage` 只作记录。
3. **稳态等待**：仿真从 0 起算，`analysisStart = 0.10 s`；硬件是连续运行的，改完参数后 `pause(cfg.settleTime)`（建议 1–3 s，取决于电压环带宽）再录制。录制文件的时间轴从录制开始计（≈0），所以 `analysisStart/End` 改为录制窗口内的区间。
4. **安全**：脚本一次能改的参数远比手点快。建议：参数变化用小步（例如 `ud_ref` 斜坡）、每个工况前后读回 `PHV/PLV` 判断是否越限、`onCleanup` 里决定是否写 `enable = 0`（`exp9_auto_dspace.m` 的 `cfg.disableOnExit`）。**首次自动化请让人在急停旁边。**

---

## 8. 四个 auto_sim 脚本的改造方案

### 8.1 原则
四个脚本结构完全一样：`localDefaultConfig → localBuildCases → load_system → localResolveModelBindings → localConfigureModelForBatch → localConfigureScopeLogging → 循环{localApplyCase → localRunOneCase(sim) → localExtractCaseData → localAnalyzeCase → localExportCase}`。只替换前半段"模型层"，后半段分析/导出层不动。

### 8.2 函数级替换表（对四个脚本通用）

| 原函数 | 作用 | 硬件版替换 |
|---|---|---|
| `load_system(cfg.modelFile)` + `onCleanup(close_system)` | 加载模型 | `rig = DspaceRig(); rig.openExperiment('Project_PACC','Experiment_PACC'); rig.goOnline();` + `onCleanup(@() rig.goOffline())` |
| `localResolveModelBindings` / `localResolveBlocks`（`find_system` 按名找块） | 建立 块句柄 | `cfg.var.* = 'Model Root/…/Value'` 字符串表 + `localResolveVariables`（对每个路径 `rig.getParam` 一次，找不到即报错） |
| `localConfigureModelForBatch`（StopTime、Solver、FastRestart） | 仿真设置 | 删除；改为 `cfg.settleTime`、`cfg.recordTime` |
| `localConfigureScopeLogging` / `localScopeMap`（Scope→工作区变量名） | 信号来源 | `rig.configureMeasurement({'Model Root/PHV/In1', …})`；Scope 多输入用 `{'…/In1','…/In2','…/In3'}` 拼列 |
| `localApplyCase`（`set_param(...,'Value',num2str(v))` + `assignin('base',…)`） | 设工况 | `rig.setParams({cfg.var.phaseShift, phi; cfg.var.powerFactor, xi; …})`；`assignin` 全部删除；`Resistance/Amplitude` 两行删除并打印"请人工设定" |
| `localRunOneCase`：`sim(cfg.model,'ReturnWorkspaceOutputs','on')` | 跑一次 | `pause(cfg.settleTime); matFile = rig.record(cfg.recordTime, fullfile(caseDir, caseName));` |
| `localFetchSimVariable(simOut, name)` / `localFetchSignal` / `localNormalizeSignal` | 取信号 | `rec = DspaceRig.loadRecording(matFile); d.X = DspaceRig.getSignal(rec, cfg.sig.X)`（输出同构，`localFlattenSignal/localTimeFirst` 可保留但已用不到） |
| `localValidateEssentialSignals`（要求 Scope78/7/17/91/92 ≥3 列） | 校验 | 改为检查 `d.*.available`；开关级信号（Scope7/17/91/92 门极）在 CPU 域不存在，从必需列表移除或改由示波器提供 |
| `localAnalyzeCase` 及其以下（FFT、RMS、J 曲线、导出 CSV/MAT/JSON） | 分析导出 | **不动**；`cfg.analysisFs = 1e4`；对 fsw/2fsw 指标加守卫（`analysisFs < 4*fsw` 时置 NaN 并标记 `status='ok_no_ripple'`），示波器数据接入后再放开 |

### 8.3 `DspaceRig` 接口（`DspaceRig.m`）

```matlab
rig = DspaceRig();                                  % 连接/启动 ControlDesk
rig.openExperiment(project, experiment)             % 打开工程、激活实验，缓存平台与变量集合
rig.loadApplication(sdfPath, startAfterLoad)        % 下载 .rta（= rti_build2 'L' / ControlDesk Load RTA）
rig.applicationState() / startApplication / stopApplication
rig.goOnline() / rig.goOffline() / rig.isOnline()
v = rig.getParam(path);  rig.setParam(path, v);  rig.setParams({path, v; ...})   % 写后自动回读校验
tbl = rig.listVariables(regexPattern)               % 列出变量描述里的变量（建映射表用）
rig.configureMeasurement(paths, bufferSizeSec)      % 重建测量信号列表（raster 默认 HostService）
[matFile, mf4File] = rig.record(durationSec, baseName)   % 定时录制 + 自动导出 MAT（失败则 DSSigConv 兜底）
rec = DspaceRig.loadRecording(matFile);  d = DspaceRig.getSignal(rec, keyOrCell)   % 静态，可离线用
```

### 8.4 样例：`exp9_auto_dspace.m`
这是 `exp9_auto_sim.m` 的逐函数改造版（文件头有对照表）。主循环变成：

```matlab
localApplyCase(rig, cfg, ang);                       % rig.setParam('Model Root/phi2_deg/Value', ang)
pause(cfg.settleTime);
matFile = rig.record(cfg.recordTime, fullfile(runDir, sprintf('exp9_phi_%03d', ang)));
rec = DspaceRig.loadRecording(matFile);
d.IH = DspaceRig.getSignal(rec, cfg.sig.IH);  ...    % 其余同原脚本
r = localAnalyzeCase(d, cfg, ang);                   % 原分析函数，仅加了采样率守卫
```

把同样的改法套到其余三个脚本：
- **exp8_auto_sim.m**（角度 × 运行点 A–L 扫描）：`localBuildCases` 不变；`localApplyCase` 里 `phase_shift/power_distribute_factor/ud_ref` 三项改 `setParams`，`load/dcHigh/dcLow` 改为打印提示；每个运行点（VH/VL/负载不同）之间需要人工换档，可在 `cfg.cases` 里加 `manualStep=true` 并用 `input('按回车继续')` 停一下；观测器相关信号（Scope80–86、89）改为硬件的 `phi_opt/phi_search/gamma`，J 曲线列布局相关的 `localCurveMins` 保留但会返回 NaN。
- **exp10_auto_sim.m**（0° 下 ξ 0.10–0.85 扫描 × VL 四档）：ξ 路径填到 `cfg.var.powerFactor`；VL 四档为外层人工循环。这是最适合先跑的脚本——每档 16 个 ξ 点，全程只改一个软件参数。
- **auto_sim.m**（`Inverter_CBPWM` 旧模型）：该模型与 `PACC_Drive` 差异最大（`TransportDelay` 载波移相、`ScopeData27/29/72` 等），建议不要迁移，其用例已被 exp8/exp10 覆盖。

### 8.5 建议的工况时序
`setParams(工况)` → `pause(settleTime)` → 读 `PHV/PLV/idc1` 做越限检查 → `record(recordTime)` → 分析 → 下一工况。全部工况结束或出错：`onCleanup` 中先按策略写 `enable`，再 `goOffline`。不要在循环里 `loadApplication`（重新下载会让硬件重启、参数复位）。

---

## 9. 首次联调顺序（每步都能单独回退）

1. 台架安全状态、MicroLabBox 已连接、`pacc_drive.rta` 已在运行（Simulink Ctrl+B 或 ControlDesk 已 Load）。
2. 在工位 MATLAB 中：`addpath('F:\ZYS_HYBASE\automation'); dspace_selftest`。它只做：连接 → 打开工程 → Go Online → 读 `phi2_deg` → 导出可写变量表 → 录 1 s → 读回并画图。任何一步失败都不会改参数。
   - 若 `openExperiment` 报找不到工程：ControlDesk 里 File → Project Roots 确认 `F:\ZYS_HYBASE` 在列表里。
   - 若 `variable` 报找不到：用 `rig.listVariables('phi')` 看真实名称，或在 ControlDesk Variables 面板复制路径。
   - 若 `record` 超时但 `.mf4` 已生成：说明 `ExportFullPath` 未生效，类会自动调用 `DSSigConv` 兜底；确认 `.mat` 出现即可。
3. 用 `writable_values.csv` + `PACC_Drive.slx` 补全 `exp9_auto_dspace.m` 里的 TODO（ξ、AC 相位、两个 `ud_ref`、`IH/P1/P2` 信号）。
4. 把 `cfg.angles` 改成单个安全角度、`cfg.recordTime = 0.5`，跑 `exp9_auto_dspace.m`；确认 CSV、`.mf4/.mat` 都生成后再放开角度列表。
5. 再迁移 exp10（最简单）、exp8。示波器通道接入后，去掉 `localAnalyzeCase` 里的采样率守卫。

---

## 10. 参考

- 手册文本（已从工位复制并转成纯文本）：本机 `/tmp/dspace_docs/`（`ControlDeskAutomation.txt`、`ControlDeskMeasurementAndRecording.txt`、`RTIAndRTIMPImplementationGuide.txt` 等），工位 `C:\Temp\dsdocs\`（PDF）。
- ControlDesk Automation：Basics p.118（启动）、p.120（工程/实验）、p.123（平台）、p.128（直接变量访问）、p.144（测量与录制）、p.219（M 脚本 COM 连接）；API：`RegisteredDS1202Platform` p.1789、`ControllableRealTimeApplication` p.622、`MainRecorder` p.1403、`MeasurementSignals` p.1455、`ApplicationState` p.391、`StopConditionType`（TimeLimit=0）。
- Measurement & Recording：MAT 导出 p.200，MAT 结构 p.204。
- RTI Implementation Guide：Build & download p.223（"Load application after build"）。
- 工位示例脚本：`C:\Program Files\dSPACE ControlDesk 2023-A\Demos\ToolAutomation\Python\*.py`（Python 版对象模型与 MATLAB 一一对应）、`…\Demos\MC3\Matlab\Measurement\DsMC3_MeasurementDemo.m`。
