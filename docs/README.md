# 文档导航

Tiffany 保留原始事件，按需读取字段，用 Hook 编写业务。这里按使用、开发、设计和规划组织维护中的说明；入口与默认值以当前源码和配置示例为准。

## 从这里开始

| 想做什么 | 入口 |
| --- | --- |
| 安装项目、接入 OneBot、运行第一个 Hook | [项目 README](../README.md) |
| Linux / Windows 启动、配置、更新与回滚 | [部署指南](guides/deployment.md) |
| 接入 QQ 官 Bot，了解字段、恢复与回复边界 | [QQ 接入指南](guides/qqofficial.md) |
| 找到模块职责，跟踪一条消息的调用链 | [源码地图](development/code-map.md) |
| 分阶段读代码，编写 Hook、Service 与 Scope 扩展 | [学习与开发路线](development/learning-guide.md) |
| 运行测试，查找单元、集成和协议测试 | [测试导航](../tests/README.md) |
| 复测性能，了解工具、计时与统计边界 | [基准指南](development/benchmarks.md) |
| 理解分层、依赖、资源归属与投递边界 | [架构约束](design/architecture.md) |
| 配置预算、同步接纳、公平调度与取消 | [调度契约](design/scheduling.md) |
| 查看未完成的开发与实机验收事项 | [后续路线图](planning/roadmap.md) |

## 目录职责

```text
docs/
├── README.md
├── guides/          # 使用、部署与平台接入
├── development/     # 源码、扩展学习与性能复测
├── design/          # 当前架构和运行契约
└── planning/        # 尚未完成的目标与验收
```


## 维护约定

- 使用与开发文档描述已经实现的行为；规划只保留尚未完成的工作。
- 配置、入口或公共接口改变时，同步更新对应指南、示例和链接。
- 阶段测试流水随提交或版本发布记录保存，避免把某次测试数量或环境结论当作长期说明。
- 基准方法与原始测量分开维护；新结果记录源码、依赖、环境、参数和计时边界，保留失败与中断样本。

项目许可统一为根目录的 [MIT License](../LICENSE)。
