# 自动化台架导入记录

本文件只记录本仓库的导入范围，不记录来源电脑的盘符、账号、地址或其他部署身份信息。

## 已纳入

- `START_SCOPE_BENCH.cmd`、`SETUP_SCOPE_BENCH.cmd` 和 scope profile 选择入口；
- `core/`、`config/`、`scope_bench/` 的可复用源码、配置、测试和说明；
- `test_cases/` 中的 catalog、phase sweep、ξ steady/switch、showcase 和 carrier-only case；
- 与上述功能直接相关的工具、回归测试和相对路径文档。

## 已排除

机器本地虚拟环境、会话状态、运行结果、备份、暂存目录、缓存、日志、密钥、完整采集数据和未脱敏的现场记录均未纳入仓库。

## Case 数量

`phase_sweep_5deg_vl120_x0515_p000_p180` 保留早期完整的 0°–180° 网格，共 37 个启用 case。CSV、catalog、README 和 dedicated runner 已同步；部署环境中的临时子集不作为通用模板。

## 验证边界

本次导入只做文件筛选、配置核对和离线测试，没有连接 ControlDesk、控制示波器、改变硬件状态或采集新数据。使用前必须在目标环境重新核对本地配置和保护条件。
