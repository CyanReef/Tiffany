# 基准工具导航

测量工具与生产运行时隔离，方法和完整复测步骤见 [基准指南](../docs/development/benchmarks.md)。psutil、Matplotlib、其他框架与 Node 包使用独立环境，不进入服务器的基础核心。

| 工具 | 用途 | 依赖与说明 |
| --- | --- | --- |
| `run.py` / `plot.py` | 核心分发与懒字段微基准 | 测量使用标准库和核心，绘图使用 `benchmark` 可选依赖 |
| `representative/` | 四框架消息、规则、HTTP、突发与资源 | `python -B -m benchmarks.representative.run --help`；Python 扩展锁文件与 `koishi/` Node 清单 |
| `elastic.py` / `elastic_validate.py` | 源码对照、恢复采样与稳定性 | `python -B -m benchmarks.elastic --help`；校验读取自己生成的数据 |
| `optimize.py` / `plot_optimization.py` | 指标与分发路径的快照比较 | 采样额外需要 psutil；绘图使用 Matplotlib |
| `compare.py` / `compare_concurrency.py` / `plot_comparison.py` | 早期三框架与统一并发对比 | `requirements-comparison.lock` |
| `compare_expanded.py` / `expanded_engines.py` / `koishi_worker.cjs` / `plot_expanded.py` | 早期六框架及原生路径诊断 | Python 扩展与 Graia 锁文件分开，Node 清单在 `koishi/` |

历史结果目录及源码快照已清理。采样、校验、绘图和四框架报告的默认数据路径统一在 `.build-cache/benchmarks/`。先生成数据，再通过 `--input` 选择同批 JSON；来源和统计方法不同的批次分别保存。源码对照的基线需要自行准备。

```powershell
python -B -m benchmarks.run --output .build-cache/benchmarks/local.json
python -B -m benchmarks.plot --input .build-cache/benchmarks/local.json --output .build-cache/benchmarks/plots

# 对四框架工具生成的 JSON 进行校验
python -B -m benchmarks.representative.validate --input .build-cache/benchmarks/representative/results.json
```

业务计数与完成量要独立核验，不能用提交或接纳的计时冒充业务完成。新的失败或中断样本与同批数据保存，恢复测量时保留引用关系；用 `python -B -m benchmarks.elastic_validate <结果.json>` 校验调度测量。
