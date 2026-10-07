# 基准与复测指南

本页说明测量工具、计时边界和复测方法。历史采样与源码快照已清理，复测需重新准备基线和采样数据；测量数字只对应记录的源码与环境。

## 工具与依赖

| 工具 | 测量范围 | 依赖 |
| --- | --- | --- |
| [run.py](../../benchmarks/run.py)、[plot.py](../../benchmarks/plot.py) | 核心事件分发、匹配路由和字段缓存 | 采样使用基础依赖；绘图使用 `benchmark` 可选依赖 |
| [representative/](../../benchmarks/representative/) | Tiffany、NoneBot、AstrBot、Koishi 的离线原生消息路径、规则、HTTP、突发和资源 | 独立 Python/Node 环境、psutil、aiohttp、框架依赖；入口参数指定解释器 |
| [elastic.py](../../benchmarks/elastic.py)、[校验](../../benchmarks/elastic_validate.py) | 两份源码的正常负载、HTTP、固定到达、突发与稳定性 | 独立环境；需基线和候选源码，HTTP 服务使用 aiohttp |
| [optimize.py](../../benchmarks/optimize.py)、[绘图](../../benchmarks/plot_optimization.py) | 指标与分发路径的源码快照对照 | psutil；绘图使用 Matplotlib |
| `compare*`、`expanded_engines.py`、`plot_comparison.py`、`plot_expanded.py` | 早期三/六框架采样与诊断 | 独立框架环境，锁文件与 Node 清单见 [工具导航](../../benchmarks/README.md) |

基准工具与服务器运行环境隔离。其他框架、绘图和资源采样依赖不进入基础核心。不同工具的工作负载与统计单位不同，不合并其结果。

## 核心微基准

在项目根目录运行；新增采样、图表和报告放在 `.build-cache/`，`docs/` 只保留维护中的说明。

```powershell
python -m pip install -e ".[benchmark]"
python -B -m benchmarks.run --output .build-cache/benchmarks/local.json
python -B -m benchmarks.plot --input .build-cache/benchmarks/local.json --output .build-cache/benchmarks/plots
```

| 参数 | 默认值 | 含义 |
| --- | ---: | --- |
| `run --events` | 3000 | 每轮每种分发场景的事件数 |
| `run --contexts` | 50000 | 每轮每种字段场景的 Context 数 |
| `run --warmup` | 200 | 每轮每种分发场景的预热次数 |
| `run --repeats` | 7 | 重复轮数 |
| `plot --language` | `all` | 简体中文和英文；可选 `zh-CN`、`en` |

分发计时从创建原始字典与 Envelope 开始，包含 Runtime 接纳、Scheduler、Context、Hook 和指标，直到串行 `emit()` 返回。注册、启动、预热和关闭不计时；Hook 只计数，不做 I/O。匹配 Hook 数量与无关事件类别分别变化，预热路由保持注册表不变。这不覆盖冷选路、注册变更或多生产者满载。

字段测量预先分配 Context，首次读取包含 Provider 查找、同步计算和缓存写入；缓存读取在计时前填充缓存，计时中不调用 Provider。Context 分配、预填充、验证和主动 GC 不计时，正常自动 GC 保持开启。

JSON 保存环境、逐轮数据、汇总和调用计数。绘图只读 JSON，不重新采样；中文输出需要可用中文字体，缺少时可选 `--language en`。

## 框架消息路径复测

先准备独立环境，不使用机器人账号。Python 依赖采用 [扩展锁文件](../../benchmarks/requirements-expanded.lock)，Node 依赖采用 [koishi 清单](../../benchmarks/koishi/package.json) 和 [锁文件](../../benchmarks/koishi/package-lock.json)。锁文件包含早期比较所需的额外框架；当前四框架入口只导入其测量对象。

```powershell
py -3.12 -m venv .build-cache/expanded-python-env
.\.build-cache\expanded-python-env\Scripts\python.exe -m pip install --require-hashes --only-binary=:all: -r benchmarks/requirements-expanded.lock
npm ci --prefix benchmarks/koishi

.\.build-cache\expanded-python-env\Scripts\python.exe -B -m benchmarks.representative.run --python .build-cache/expanded-python-env/Scripts/python.exe --server-python .build-cache/expanded-python-env/Scripts/python.exe --node node --node-env benchmarks/koishi --output .build-cache/benchmarks/representative/results.json
python -B -m benchmarks.representative.validate --input .build-cache/benchmarks/representative/results.json
python -B -m benchmarks.representative.plot --input .build-cache/benchmarks/representative/results.json --output .build-cache/benchmarks/representative/assets/performance
python -B -m benchmarks.representative.report --input .build-cache/benchmarks/representative/results.json --output .build-cache/benchmarks/representative/report.md
```

扩展锁文件针对 Python 3.12；上例使用 Windows 的 `py -3.12` 和虚拟环境路径，Linux 使用 `python3.12 -m venv` 并将解释器改为 `.build-cache/expanded-python-env/bin/python`。协调器与本机服务使用同一已安装 psutil、aiohttp 的环境；校验和绘图使用的当前 Python 也需要各自依赖。参数细节用 `python -B -m benchmarks.representative.run --help` 查看，生成报告中的来源与复测环境需结合本次 JSON 核对。

场景见 [cases.py](../../benchmarks/representative/cases.py)。计时覆盖原生事件构造、解析、调度和业务完成；匹配处理器、其他事件类别与同类 False 规则分别测量。HTTP 使用本机共享服务；突发需分别记录提交、已接纳、进入处理器、排队、拒绝与最终完成。

[sources.py](../../benchmarks/representative/sources.py) 保存源码快照并核验实际导入路径；[validate.py](../../benchmarks/representative/validate.py) 核对事件、字符、处理器、规则、HTTP、指标与接纳状态。重复测量单位是独立框架进程，进程内事件数不作为额外独立重复。

## 调度验证与源码对照

正确性测试覆盖预算、会话 FIFO、公平性、取消与真实本机 WebSocket 满载行为：

```powershell
python -B -m unittest tests.integration.core.test_elastic_scheduler tests.integration.core.test_scheduler_regressions tests.integration.onebot.test_elastic_transport -q
```

性能对照使用 `python -B -m benchmarks.elastic --help` 指定自己准备的 `--baseline`、`--source`、负载与独立 `--output`；基线和候选保持相同解释器、依赖与参数。默认基线目录 `.build-cache/elastic/before` 需要预先准备，不随仓库提供。采样后使用 `python -B -m benchmarks.elastic_validate <结果.json>` 重算完成量、摘要与验收条件；恢复测量时保留其引用的中断样本。接纳与取消契约见 [调度说明](../design/scheduling.md)。

## 解释结果

- 吞吐以完成业务量除以耗时，被拒绝、仅已提交或仍等待的事件不计入完成量。
- 中位数和最小–最大范围属于描述性统计；范围不等于置信区间。P95/P99 说明是逐轮计算还是合并事件计算。
- 记录源码摘要、Python/Node 与依赖版本、系统、GC/Trace/指标设置、预热和重复次数。
- 进程初始化、CPU、RSS/工作集、采样峰值、HTTP 服务端等待和事件完成延迟分别说明边界。
- 结果不覆盖真实平台收发、权限、限流、LLM 或完整生产应用，也不能替代实机与持续运行验收。

新的未通过、中断和诊断样本与同批结果一起保存，注明来源和恢复关系。清理历史数据不构成性能验收通过；发布结论需依据对应版本的新测量与实机证据。
