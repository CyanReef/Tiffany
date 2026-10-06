# 文档导航

项目的核心约束是 **保留原始事件、按需读取字段、用 Hook 编写业务**。本目录将现行说明、设计与规划、历史实测分别整理；入口和默认值以代码及现行说明为准。

## 现行说明

| 主题 | 文档 |
| --- | --- |
| 安装与第一个 Hook | [项目 README](../README.md) |
| 目录职责与消息调用链 | [源码地图](CODE_MAP.md) |
| 分阶段阅读与扩展示例 | [学习路线](LEARNING_GUIDE.md) |
| Linux / Windows 启动、配置、更新与回滚 | [部署说明](DEPLOYMENT.md) |
| 已取得的部署证据与实机待办 | [部署验证记录](DEPLOYMENT_VALIDATION.md) |
| QQ 字段、网关与 HTTP 边界 | [QQ 接入设计](QQOFFICIAL_DESIGN.md) |
| 共享预算、同步接纳与公平调度 | [调度契约](ELASTIC_SCHEDULING.md) |
| 测试分类和运行命令 | [测试导航](../tests/README.md) |
| 测量工具、依赖和结果分类 | [基准导航](../benchmarks/README.md) |

## 已保存的实测

| 范围 | 报告 | 状态与边界 |
| --- | --- | --- |
| 共享预算实施前后 | [弹性调度性能](PERFORMANCE_ELASTIC.md) | 保留未通过的性能门槛、恢复采样和 30 分钟稳定性证据 |
| Tiffany / NoneBot / AstrBot / Koishi | [四框架消息处理](FRAMEWORK_COMPARISON_REPRESENTATIVE.md) | 2026-10-06 的离线采样；对象由来源指纹固定 |
| 核心微基准的方法 | [基准说明](BENCHMARKS.md) | 保留 2026-09-30 结果，与后续批次分别阅读 |

报告描述其记录的源码和环境。项目整理后的代码不能直接继承历史性能结论；新测量应指定新的输出文件，再独立校验和生成报告。

## 设计、规划与历史

- [项目方向与核心约束](planning/target.md) 和 [实施计划](planning/FRAMEWORK_PLAN.md)：只将未完成事项列为规划。
- [dsh 架构研究](design/DSH_ARCHITECTURE_STUDY.md)：设计启发及与 Tiffany 的职责边界。
- [历史报告索引](archive/README.md)：此前代码检查、指标/调度优化及三/六框架采样。
- [基准结果索引](../benchmarks/results/README.md)：原始数据、源码 ZIP 和中间未通过结果。

项目许可统一为根目录的标准英文 [MIT License](../LICENSE)。
