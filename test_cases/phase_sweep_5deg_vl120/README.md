# VL120 phase sweep（5°）

## 用途

在固定的 VL120 条件下，按 case 表扫描 ξ 与相位角，统一采集八通道记录并生成可追溯结果。外部电源、探头、量程和保护设置由现场操作者确认。

## 采集约束

- 默认 scope profile 为 MSO58B；通道、单位、采样率、点数和记录长度必须以连接检查的实际回读值为准。
- 预期 case 数量由 `catalog.json` 校验，CSV 行数不符时在使能前中止。
- 结果写入配置指定的 `scope_results/sweeps/`，每个工况使用唯一 case ID。

## 运行

先执行：

```powershell
.\core\run_test_batch.ps1 -Mode Plan -TestIds phase_sweep_5deg_vl120
.\core\run_test_batch.ps1 -Mode Check -TestIds phase_sweep_5deg_vl120
```

确认 ControlDesk Online、`Enable1=0` 且外部电源设置正确后，由现场操作者执行 Run。停止时先关闭使能，再等待示波器导出和结果校验完成。

## 完整模板

`phase_sweep_5deg_vl120_x0515_p000_p180` 保留 0°–180° 的 37 个启用 case。CSV、catalog、runner 和 README 的数量必须保持一致。
