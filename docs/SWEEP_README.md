# ControlDesk 批测与 phase sweep

本说明描述仓库内批测脚本的使用边界。ControlDesk 工程、实时应用、MATLAB 版本和部署根目录由目标工位提供，不在仓库中固化。

## 推荐流程

1. 在目标工位打开 ControlDesk 并加载已验证的工程。
2. 手动设置外部电源；同一批次只选择相同手动设置的 case。
3. 先执行 `run_test_batch.ps1 -Mode List` 和 `-Mode Plan`。
4. 执行 `-Mode Check`，确认变量、录波器、示波器和结果目录。
5. 现场操作者确认保护条件后，才执行 `-Mode Run`。
6. 检查每个 case 的状态、MF4/HDF5、manifest 和日志。

## 命令示例

```powershell
.\core\run_test_batch.ps1 -Mode List
.\core\run_test_batch.ps1 -Mode Plan -TestIds legacy_vl120
.\core\run_test_batch.ps1 -Mode Check -TestIds legacy_vl120
.\core\run_test_batch.ps1 -Mode Run -TestIds legacy_vl120
```

## Case 与数量

case 定义位于 `test_cases/`，入口清单是 `test_cases/catalog.json`。每个 case 应明确手动电压、参数表、示波器 profile、录波信号和预期行数。runner 在启动前比较 CSV 实际行数与 catalog 的 `expected_cases`，不一致时中止。

phase sweep 的角度和 ξ 表必须与 README、CSV、catalog 和 runner 一致。当前完整模板保留 0°–180° 的 37 个 case；不要用部署时临时缩减的选择覆盖模板。

## 结果与安全

运行结果只写入本地配置指定的结果目录；路径、账号、地址、密钥和实时应用文件不提交到 Git。`Plan` 和 `Check` 只做验证，不代表硬件已经运行。停止时先关闭 `Enable1`，再停止采集与清理。
