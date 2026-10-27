# MXO38 SCPI 控制说明

本文件只保留仓库内工具的接口边界。实际连接方式、远端主机、账号、地址、仪器序列号和部署目录属于本地配置，不纳入 Git 文档。

## 可用操作

`scope-ctl` 支持只读状态查询、通道显示参数、时基、采集、触发、测量、截图、设置快照和错误队列操作。常用形式：

```text
scope-ctl state
scope-ctl info
scope-ctl chan 2 --scale 0.5 --unit A
scope-ctl screenshot -f before
scope-ctl settings snapshot results/scope_snapshot.json
scope-ctl errors
```

原始 SCPI 通过 `scope-ctl scpi` 发送；写操作必须显式确认。优先使用封装好的只读命令，写入仪器前先保存设置快照，并在操作结束后恢复。

## 参数边界

- 通道单位必须来自探头或仪器设置，不要按通道编号猜测。
- 采样率、记录长度、带宽和时间轴必须以仪器回读值为准。
- 测量槽位未启用、源通道关闭或没有结果时，某些仪器可能超时；调用方应设置超时并记录原始错误。
- 截图和设置快照应写入运行结果目录，不要写入源码目录。

## 安全流程

1. 先执行只读 `state`、`info` 和错误队列检查。
2. 保存设置快照，记录 case、时间和配置摘要。
3. 只写入已授权的参数，逐项回读确认。
4. 完成后恢复快照并再次读取关键状态。
5. 发生通信异常时停止后续写操作，由现场操作者确认设备状态。

更完整的台架操作、`.tss` 增量拉取和故障处理见 [`scope_bench/README.md`](../scope_bench/README.md)。
