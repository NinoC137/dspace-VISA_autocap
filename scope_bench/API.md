# scope_bench API 说明

`scope_api.py` 将 PC 侧批测请求转发给配置指定的采集代理。代理负责仪器通信和分块读取，PC 侧负责结果目录、校验、HDF5 封装和分析。连接地址、账号、远端目录和共享盘挂载点均由本地配置提供。

## 调用原则

- 所有请求都应携带 case ID、capture ID、通道列表、采集参数和超时预算；
- 采集前先读回仪器设置，采集后保存原始 header 和实际设置；
- 大数据使用分块传输，每个分块校验长度，结束后校验 SHA-256；
- 目标文件先写临时后缀，完整校验后再原子改名；
- 失败时保留日志、清单和未完成文件，不把未完成文件标记为成功；
- 控制请求必须与仪器锁绑定，不能与其他采集进程并发。

## 结果结构

```text
<results>/
├── capture_<id>/
│   ├── manifest.json
│   ├── waveforms.h5
│   ├── screenshots/
│   └── logs/
└── transfer_manifest.json
```

`<results>` 只是配置占位符，不应在文档中替换成真实主机路径。接口返回值应区分 `planned`、`running`、`complete`、`failed` 和 `aborted`；截图状态与采集状态分别记录。

## 硬件边界

API 的离线测试、假仪器和配置检查不等于真实链路验证。真实采集前由现场操作者确认 ControlDesk、外部电源、保护设置和仪器锁状态。
