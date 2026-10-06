# 基准工具导航

基准工具与生产运行时隔离。核心只要求项目基础依赖；测量所需的 psutil、Matplotlib、其他框架和 Node 包使用独立环境，不进入服务器的基础核心。

| 工具 | 用途 | 依赖与说明 |
| --- | --- | --- |
| `run.py` / `plot.py` | 核心分发与懒字段微基准 | 测量使用标准库和核心，绘图使用 `benchmark` 可选依赖 |
| `representative/` | 当前四框架消息、规则、HTTP、突发与资源维度 | [四框架报告](../docs/FRAMEWORK_COMPARISON_REPRESENTATIVE.md)；入口 `python -B -m benchmarks.representative.run` |
| `elastic.py` / `elastic_validate.py` / `elastic_report.py` | 共享预算前后对照、恢复采样和稳定性 | [调度性能报告](../docs/PERFORMANCE_ELASTIC.md)；验证与报告生成读取已保存数据 |
| `optimize.py` / `plot_optimization.py` | 以源码快照比较指标与分发路径 | [历史优化报告](../docs/archive/PERFORMANCE_OPTIMIZATION.md)，新增测量额外需要 psutil |
| `compare.py` / `compare_concurrency.py` / `plot_comparison.py` | 历史三框架及统一并发对比 | [历史三框架报告](../docs/archive/FRAMEWORK_COMPARISON.md)；`requirements-comparison.lock` |
| `compare_expanded.py` / `expanded_engines.py` / `koishi_worker.cjs` / `plot_expanded.py` | 历史六框架及原生接入 | [六框架报告](../docs/archive/FRAMEWORK_COMPARISON_EXPANDED.md)；Python 与 Graia 锁文件分开，Node 清单在 `koishi/` |

默认输出按类别存放在 `results/`；新复测应指定 `.build-cache/` 下的新文件，避免覆盖已保存的采样。绘图及报告通过 `--input` 选择数据，来源和统计方法不同的批次分别保存。

```powershell
python -B -m benchmarks.run --output .build-cache/benchmarks/local.json
python -B -m benchmarks.plot --input .build-cache/benchmarks/local.json --output .build-cache/benchmarks/plots

python -B -m benchmarks.representative.validate
python -B -m benchmarks.elastic_validate benchmarks/results/elastic/tiffany-elastic-final-normal-long.json benchmarks/results/elastic/tiffany-elastic-final-soak.json
```

[结果索引](results/README.md) 说明当前数据、历史数据和源码快照的位置；各报告提供解释器、依赖、平台、计时边界和复测参数。失败或中断数据仍保留，不能用只完成接纳的计时冒充业务完成。
