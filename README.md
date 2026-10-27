# PACC ControlDesk / 示波器自动化

本仓库保存可复用的 ControlDesk 批测、示波器采集、波形分析和实验 case。部署主机、网络地址、账号、仪器序列号及本地盘符均不写入仓库；请在目标环境的配置文件中填写。

## 快速使用

1. 首次使用运行 `SETUP_SCOPE_BENCH.cmd`，准备 Python 依赖。
2. 打开 `START_SCOPE_BENCH.cmd`，选择示波器、测试 case，并执行连接检查。
3. 确认 ControlDesk 已 Online、`Enable1=0`，再启动批测。
4. 结果写入配置指定的 `scope_results/` 或 `sweep_results/`，不要在运行中移动结果目录。

命令行等价操作：

```powershell
.\core\run_test_batch.ps1 -Mode List
.\core\run_test_batch.ps1 -Mode Plan -TestIds phase_angle_validation_01,legacy_vl120
.\core\run_test_batch.ps1 -Mode Check -TestIds phase_angle_validation_01
.\core\run_test_batch.ps1 -Mode Run -TestIds phase_angle_validation_01
```

真实批测会写入 ControlDesk 参数并控制使能；`Plan`、`Check` 和测试脚本中的离线检查不等于硬件运行。一次批测只应包含相同手动电源设置的 case。

## 按需拉取 Tek `.tss`

在 `START_SCOPE_BENCH.cmd` 窗口中选用 MSO58B，点击 **拉取 Tek .tss**，即可递归增量复制仪器中的 `.tss` 文件。已存在且相对路径、文件大小匹配的文件不会重复下载；历史清单和本地 SHA-256 用于核验。每个新增文件完整读取并校验后，会立即写入配置指定的 `Tek-scope/run_*/` 目录。

最终清单 `transfer_manifest.json` 记录新增和复用文件。仪器原文件保留，操作不触发 ControlDesk 批测。通信注意事项和故障处理见 [`scope_bench/README.md`](scope_bench/README.md) 的“按需拉取 Tek 内部 `.tss` 文件”。

## 波形细节图

批测完成后，可将一个完整的 `batch_*` 结果目录拖到 `PLOT_HVDC_DETAIL.cmd`，或运行：

```cmd
PLOT_HVDC_DETAIL.cmd "<batch-result-directory>"
```

该工具只读取已保存的 `waveforms.h5`，生成 `hvdc_detail/` 下的 PNG、SVG 和 `index.json`，不会启动采集或修改示波器。

## 目录

| 目录 | 内容 |
| --- | --- |
| `core/` | 批测选择、调度和安全清理 |
| `config/` | 共享配置和 ControlDesk 波形组 |
| `test_cases/` | catalog 与自包含实验定义 |
| `scope_bench/` | MXO38/MSO58B 采集、传输、分析和界面 |
| `launchers/legacy/` | 旧版 per-VL 流程兼容入口 |
| `matlab/` | DspaceRig 与 MATLAB 示例 |
| `tools/` | 只读清单、诊断和仪器辅助工具 |
| `tests/` | 非硬件回归测试 |
| `docs/` | 使用说明与设计记录 |

测试目录由 `test_cases/catalog.json` 管理。每个 case 声明手动设置、scope profile 和预期数量；执行前会校验数量，避免 CSV 被意外修改后静默扩大批测范围。

## 维护约定

- 运行环境相关的路径、账号、地址、密钥、序列号和结果文件放在本地配置或部署记录中，不提交到 Git。
- 不要提交 `.venv`、缓存、日志、临时文件、完整采集数据或未脱敏的现场记录。
- 启动真实硬件前先执行 `Plan`、`Check` 和必要的离线回归测试。
- `phase_sweep_5deg_vl120_x0515_p000_p180` 保留 0°–180° 的 37 个完整 case，CSV、catalog、README 和 runner 的数量必须一致。

更细的操作流程见 [`scope_bench/README.md`](scope_bench/README.md)、[`docs/SWEEP_README.md`](docs/SWEEP_README.md) 和各 case 目录下的 README。
