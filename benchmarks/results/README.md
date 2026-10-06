# 基准结果分类

原始 JSON 和源码 ZIP 按用途保存。目录整理只移动文件，保持采样内容及 SHA-256；记录中的原始工作目录和恢复路径仍表示采样时的位置。

| 目录 | 内容 |
| --- | --- |
| [representative/](representative/) | 2026-10-06 四框架采样及对应源码 ZIP；`framework-comparison-representative.json` 是该批唯一结果文件 |
| [elastic/](elastic/) | 共享预算最终正常/HTTP/稳定性数据、恢复所需中断样本、阶段源码清单与四份 ZIP |
| [archive/micro/](archive/micro/) | 2026-09-30 核心分发和字段微基准 |
| [archive/optimization/](archive/optimization/) | 指标/调度优化及 2026-10-05 代码检查的原始采样 |
| [archive/comparison/](archive/comparison/) | 三框架、统一并发、六框架和原生路径诊断 |
| [archive/elastic-development/](archive/elastic-development/) | 共享预算实施过程中的完整与未通过采样；保留恢复关系 |
| [archive/representative-20261004-e9db8644e5ff/](archive/representative-20261004-e9db8644e5ff/) | 上一轮四框架 JSON、报告、图件及原 manifest；整个归档保持原字节 |

`elastic_validate.py` 可从新的分类目录找到恢复样本，仍核对记录的原文件摘要和全部保留观测。源码清单中的归档名称相对于其所在的 `elastic/` 目录。未完成的开发样本只作诊断，性能通过与否以对应报告的验收结果为准。

完全相同的 `framework-comparison-representative-20261006.json` 已合并到 `representative/framework-comparison-representative.json`；来源 ZIP 单独保留。新复测使用独立输出路径，见 [工具导航](../README.md)。
