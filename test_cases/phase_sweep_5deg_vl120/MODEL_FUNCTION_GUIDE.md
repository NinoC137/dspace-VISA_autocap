# 100 μs 任务中的电流重建记录

本次只增加 dSPACE 记录，示波器通道、探头换算、接线、记录长度和采样率全部保留。没有修改、编译、下载或启动 `PACC_Drive.slx`。

## 函数修改

本目录 `model_reference/PACC_RippleCompositionCPU_complexV2_telemetry.m` 是对用户粘贴函数的修改参考版，可将函数内容放入原 `MATLAB Function13` 块；原八个输出和原七个输入顺序保留，新加六个输出。参考文件使用 `_telemetry` 函数名以便与原函数做离线对照；粘贴时可以保持你的原函数名。

| 新输出 | 尺寸 | 含义 |
|---|---|---|
| `IP_avg` | 1×1 double | 两端口 P 节点的载波单元平均电流之和 / A |
| `IO_avg` | 1×1 double | 两端口 O 节点的载波单元平均电流之和 / A |
| `IH_avg_hat` | 1×1 double | `IP_avg+BETA_LH*IO_avg`，仅 DC 投影；不是实测 I_H |
| `nodeMean` | 4×1 double | `[IP1_avg;IO1_avg;IP2_avg;IO2_avg]` / A |
| `nodeCoeff` | 16×1 double | 已计算的复数载波单元系数，实虚部分开，见下文 |
| `reconReady` | 1×1 boolean | 占空比历史已填满，与原来的 20 ms 窗口 `valid` 分开 |

对每个端口，相电流使用原代码的 `ix`（包含 SIGN_I1/SIGN_I2）。从原 `dHist` 中取同一载波单元的 `D_now` 和 `D_prev`，并沿用 0..1 限幅：

```matlab
IP_p_avg = sum(ix .* (dU_now+dU_prev)/2);
IO_p_avg = sum(ix .* ((dL_now+dL_prev)-(dU_now+dU_prev))/2);
```

这是原函数使用的“以载波谷值为中心、前后两半占空比不同”的单元平均值，不是谷值瞬时电流。不要直接用 `sum(ix.*dU_now)` 代替过渡单元的平均值。

在原 `cPRe/cPIm/cORe/cOIm` 已算出后，增加赋值：

```matlab
jNode = 8*(p-1)+4*(n-1);
nodeCoeff(jNode+1) = cPRe;
nodeCoeff(jNode+2) = cPIm;
nodeCoeff(jNode+3) = cORe;
nodeCoeff(jNode+4) = cOIm;
```

即端口 1 的 n=1、n=2，再端口 2 的 n=1、n=2，每组依次为 `[Re(cP);Im(cP);Re(cO);Im(cO)]`。这里保存的是原系数 c，没有额外乘 2。没有增加三角函数计算、搜索或更快任务。

新输出每 100 μs 更新，不能放到 `if m >= NWIN` 内只在 20 ms 窗末更新；原八个输出仍按原逻辑保持。参考版在停用时将新增输出清零，重新使能后等待 `NLAT` 拍历史填满，再置 `reconReady=true`。

把六个输出接到具名信号，保留为可测量变量（必要时设置 test point），再按原流程重新生成模型与 TRC。记录接口约定的变量路径是 `Model Root/MATLAB Function13/<输出名>`。编译后若实际路径不同，需同步修改本单元的 `waveform_groups.json` 与 `observation_signals.json`。不要将未连接且被优化掉的信号当作已导出变量。

## 自动化记录

仍使用原“VL120 三个 ξ 相移扫描：0°–180° / 5°”，111 点。原两组相电流 MF4 继续保存，并增加每工况的 `*+ripple_current_rec.mf4`，包含上述六个输出（数组中每个分量一并记录）。末端快照也写入 `case.json` 的 `Observations`。

专用记录器不要求先添加可视化 Time Plotter。运行时只把缺失的新信号补入 Measurement Configuration，不清空原列表。如果已有测量正在运行且新信号尚未加入/激活，程序提示操作者先停止测量，不自行停止用户已有测量。

在首次测试之前，先完成模型修改、编译及原有受控下载流程，确认六个输出在 ControlDesk 中可读。此软件部署未替你下载模型或启动实测。缺失路径会在使能前报错，不能用零填充绕过。

## 解释边界

- `IP_avg/IO_avg/IH_avg_hat` 是 100 μs 更新的平均/投影量，只能看慢变化，不能对其直接 FFT 来验证 10/20 kHz 开关纹波。
- `nodeCoeff` 是开关模型生成的系数包络，保存它可在离线按原函数 `2/NWIN * sum(c_n * exp(-j*2*pi*q*m/NC))` 复算所保留的边带，属于模型结果，不能称为实测纹波。两个端口各在自身载波坐标系，合成时需要使用记录的实际相移角并正确处理参考时刻。
- `IH_avg_hat` 使用 H_P(0)=1、H_O(0)=BETA_LH；当前 BETA_LH=0，因而它与 `IP_avg` 相同。这不是错误，也不是新引入的 I_H 传感器通道。现有模型中的 `idc1/idc2` 名称本身不足以证明其是 HVDC 源电流，没有将它们冒充为 I_H。
- 高频实测 I_H 继续保存于同工况示波器 `waveforms.h5` 的 CH1。要在 dSPACE 本身记录实测 I_H，仍需实际电流测量链与经确认的 ADC 标定/路径；本次没有新增这条硬件链。
- `CROSS_GAIN/CROSS_PHASE` 是互谱差分校正，不能唯一确定两个端口各自的时域源电流，不应直接用它们为单端口电流任意加相位。
- 示波器与 dSPACE 有各自的时间轴；“同工况保存”不代表已经验证亚载波级同步。

## 已完成的离线验证

MATLAB R2024a，`CurrentTelemetryTest`：3 项通过。覆盖 650 拍（含停用/恢复）的原八输出一致性、稳态均值/复数系数解析对照，以及历史填满/停用重置。这里只验证数值行为，未验证目标处理器最坏执行时间、HostService 实际带宽或新编译模型的变量可见性。
