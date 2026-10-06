# Tiffany 弹性调度性能验收

**整体性能验收：未通过。** 功能回归与 30 分钟稳定性通过；吞吐、尾延迟及配对区间分别按既定门槛判定。未通过条件不作为性能承诺。

基线来自实施前当前工作区的源码快照，包含已有未提交优化。没有用旧 Git HEAD 替代。原性能报告和中间未通过数据保留，新结果单独存放。

共享预算、保留额度、同会话 FIFO、同步接纳和 Adapter drain 的接口见 [调度说明](ELASTIC_SCHEDULING.md)。本轮没有单会话并行、持久化队列、后台调参或新增运行依赖。

## 环境与方法

Windows 11，CPython 3.12.14；aiohttp 3.14.3、websockets 16.0、psutil 7.1.3。性能使用已有扩展测量环境，两侧使用同解释器与依赖，真实 MetricRegistry 开启、Trace 关闭、GC 开启。没有在共享 CI 上设置速度门槛。

最终正常场景每轮、每个实现使用独立进程，轮次交替先后顺序，每个场景预热 1,000 条，计时 200,000 条，共 15 轮。计时场景最短 10.89 秒。吞吐为完成量除以计时，延迟覆盖提交到业务完成；RSS 为各场景预热后空闲采样。前期从 7 轮补至 15 轮的过程及未通过数据全部保留；优化满载完成路径后重新测量最终源码。

最终正常负载限制到逻辑 CPU [0]，HTTP 测量与服务限制到 [4, 5, 6, 7, 8, 9, 10, 11]，两侧测量进程均使用 Windows above-normal 优先级。同时进行的稳定性复测使用逻辑 CPU [12, 13, 14, 15]。两侧 CPU 与优先级设置一致，参数写入原始数据。隔离减少进程之间的直接争用，但没有排除操作系统调度、热管理和共享内存总线的影响。

完整 OneBot 场景从 JSON 帧编码、解码、会话键和预算接纳，到 Hook 完成；它是离线帧入口，不包含真实平台网络和回复时间。真正 WebSocket 满载时的 echo、ping 和 drain 另有本机集成测试。

## 正常负载

| 场景 | 基线 events/s | 新实现 events/s | 吞吐变化 | P95 变化 | P99 变化 | RSS 变化 | 配对吞吐中位数 95% 区间 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 单 Hook | 14,700 | 15,342 | +4.37% | -14.18% | -3.23% | +0.17% | 90.82%–114.88% |
| 10 Hook | 9,039 | 8,695 | -3.80% | -1.20% | +1.59% | +1.25% | 91.26%–110.32% |
| 1000 个无关 Hook | 13,751 | 14,084 | +2.42% | -12.63% | -8.02% | -3.14% | 92.32%–108.65% |
| 缓存字段读 16 次 | 13,924 | 14,060 | +0.98% | -7.10% | -18.24% | -1.90% | 89.75%–105.97% |
| 64 KiB 文本 | 14,295 | 14,366 | +0.50% | -8.65% | -14.38% | -2.45% | 90.28%–105.93% |
| 完整 OneBot 帧 | 9,749 | 10,955 | +12.38% | -22.04% | -3.89% | -1.68% | 101.57%–129.07% |

正常负载验收：**未通过**。要求吞吐至少 97%、P95 增幅不超过 5%、P99 不超过 10%、RSS 不超过 5%；15 轮后仍跨过 97% 的配对区间判为未通过。区间采用固定随机种子的配对中位数 bootstrap，4000 次重采样；这是所测工作站的波动估计，不是线上性能保证。原始数据另有每轮配对比值和每项指标最小–最大范围。

正常负载未通过条件：one_handler、ten_handlers、unmatched_1000、reads_16、text_64k。

加长测量中，10 Hook 吞吐中位数回退 3.80%，超过 3% 门槛；五个核心场景的配对区间仍覆盖门槛，无法排除 3% 回退。只有完整 OneBot 帧场景通过全部正常负载条件，吞吐中位数提高 12.38%。不能据此承诺所有正常负载都保持在 3% 以内。

## HTTP 高负载

本机 HTTP 服务实际等待至少 5/50/500 ms，由 64 个预热线程负责等待。会话数为 1/16/64/256。每个条件在独立进程比较旧默认（总容量 256、会话积压 32、全局 16、Adapter 4）、旧调优（容量和积压 8192、全局和 Adapter 64）以及新默认。客户端 HTTP 连接上限统一为 64；这不代表 QQ 的下游 API 额度会自动提高。

闭环每个生产者等待完成后再提交，窗口 2.5 秒，窗口末尾的工作排空时间计入吞吐。固定到达使用同一预定速率 `1.5 × min(会话数, 64) / HTTP延迟`；事件循环调度可能造成短暂追赶，原始数据记录实际到达窗口。突发一次提交后等待排空。5/50 ms 的多会话条件提交 512 条，其余条件提交 64 条；同一条件三种实现的提交量和预定到达速率相同。

### 闭环完成吞吐（7 轮中位数）

| HTTP ms | 会话数 | 旧默认 events/s | 旧调优 events/s | 新默认 events/s | 新 / 旧调优 | P95 比值 | P99 比值 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 5 | 1 | 160.6 | 159.8 | 161.4 | 1.010 | 0.985 | 0.985 |
| 5 | 16 | 637.3 | 2347.7 | 2378.0 | 1.013 | 0.981 | 1.076 |
| 5 | 64 | 633.5 | 4549.5 | 4418.2 | 0.971 | 1.044 | 1.108 |
| 5 | 256 | 630.3 | 3863.2 | 4086.8 | 1.058 | 0.976 | 0.946 |
| 50 | 1 | 19.5 | 19.5 | 19.5 | 1.001 | 0.999 | 1.001 |
| 50 | 16 | 77.8 | 309.9 | 310.3 | 1.001 | 0.998 | 0.993 |
| 50 | 64 | 77.6 | 1214.2 | 1214.8 | 1.001 | 1.003 | 0.964 |
| 50 | 256 | 77.3 | 1218.3 | 1217.9 | 1.000 | 0.996 | 1.007 |
| 500 | 1 | 2.0 | 2.0 | 2.0 | 1.000 | 1.000 | 1.000 |
| 500 | 16 | 8.0 | 31.8 | 31.8 | 1.000 | 1.000 | 1.000 |
| 500 | 64 | 8.0 | 126.0 | 126.4 | 1.003 | 0.998 | 0.997 |
| 500 | 256 | 8.0 | 126.5 | 126.3 | 0.999 | 1.001 | 0.999 |

闭环验收：**未通过**。同有效并发、无拒绝时，吞吐至少为旧调优的 95%，P95/P99 增幅不超过 10%。有效并发由 Hook 内活动计数实测，未把接纳量当作完成吞吐。

闭环未通过条件：closed_0.005_64。

### 固定到达与突发排空

| 条件 | 旧默认完成 / 提交 | 新完成 / 提交 | 旧调优排空 s | 新排空 s | 新 / 旧调优吞吐 | P95 比值 | P99 比值 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| open_0.5_1 | 64/64 | 64/64 | 32.082 | 32.083 | 1.000 | 1.001 | 1.001 |
| open_0.5_16 | 64/64 | 64/64 | 2.290 | 2.309 | 0.991 | 1.003 | 1.012 |
| open_0.5_64 | 64/64 | 64/64 | 0.830 | 0.831 | 0.999 | 1.001 | 1.000 |
| open_0.5_256 | 64/64 | 64/64 | 0.843 | 0.829 | 1.016 | 1.000 | 1.000 |
| burst_0.5_1 | 33/64 | 64/64 | 32.083 | 32.080 | 1.000 | 1.000 | 1.000 |
| burst_0.5_16 | 64/64 | 64/64 | 2.012 | 2.030 | 0.991 | 1.009 | 1.009 |
| burst_0.5_64 | 64/64 | 64/64 | 0.511 | 0.514 | 0.993 | 1.007 | 1.007 |
| burst_0.5_256 | 64/64 | 64/64 | 0.510 | 0.509 | 1.003 | 0.997 | 0.996 |
| open_0.005_1 | 64/64 | 64/64 | 0.388 | 0.393 | 0.988 | 1.039 | 1.029 |
| open_0.05_1 | 64/64 | 64/64 | 3.284 | 3.285 | 1.000 | 1.000 | 1.014 |
| burst_0.005_1 | 33/64 | 64/64 | 0.396 | 0.385 | 1.030 | 0.971 | 0.971 |
| burst_0.05_1 | 33/64 | 64/64 | 3.284 | 3.283 | 1.000 | 1.000 | 1.000 |
| open_0.005_16 | 321/512 | 512/512 | 0.226 | 0.214 | 1.055 | 0.920 | 0.918 |
| open_0.005_64 | 265/512 | 512/512 | 0.101 | 0.111 | 0.906 | 1.143 | 1.121 |
| open_0.005_256 | 261/512 | 512/512 | 0.103 | 0.108 | 0.957 | 1.083 | 1.090 |
| open_0.05_16 | 336/512 | 512/512 | 1.679 | 1.680 | 1.000 | 1.000 | 0.999 |
| open_0.05_64 | 276/512 | 512/512 | 0.454 | 0.450 | 1.009 | 1.011 | 0.981 |
| open_0.05_256 | 276/512 | 512/512 | 0.449 | 0.452 | 0.994 | 1.020 | 1.021 |
| burst_0.005_16 | 256/512 | 512/512 | 0.224 | 0.223 | 1.005 | 0.983 | 0.994 |
| burst_0.005_64 | 256/512 | 512/512 | 0.110 | 0.102 | 1.072 | 0.938 | 0.931 |
| burst_0.005_256 | 256/512 | 512/512 | 0.110 | 0.105 | 1.052 | 0.938 | 0.932 |
| burst_0.05_16 | 256/512 | 512/512 | 1.660 | 1.660 | 1.001 | 0.999 | 1.000 |
| burst_0.05_64 | 256/512 | 512/512 | 0.449 | 0.452 | 0.993 | 1.017 | 1.007 |
| burst_0.05_256 | 256/512 | 512/512 | 0.449 | 0.446 | 1.007 | 0.995 | 0.993 |

固定到达 / 突发验收：**未通过**。5/50 ms 使用连接预热后的 7 轮中位数；500 ms 为最终源码的单轮结果，不提供多轮统计结论。预热 `max(4, min(会话数, 64))` 次，不改变额度或验收门槛。多会话提交 512 条，覆盖全部 256 个会话。超出预算另有 9000 条过载回归：8192 条接纳、808 条明确拒绝，64 个活动 Task，排空后恢复。保留区和大事件先耗尽字节预算均单独测试。

固定到达 / 突发未通过条件：open_0.005_64。

5 ms / 64 会话是当前未达标的高负载条件：闭环 P99 相对同额度旧实现增长 10.83%；固定到达吞吐为旧调优的 90.62%，P95/P99 分别增长 14.34%/12.12%。两项均按原门槛标记未通过。短窗口及系统调度存在波动，但现有证据不能把失败归因于某个单一因素，也不能证明性能保证已达成。

## 30 分钟稳定性

实际运行 1800.01 秒，179 轮，完成 280,064 条。突发规模按 128 条单会话、512 条/16 会话、4096 条/64 会话轮换，每次排空后等待约 10 秒再继续。每轮断言预算、会话队列、就绪队列、活动工作映射、Task 和 owner 引用归零，数值同时写入原始数据。GC 后 Context 与 Work 保留量为 0，同阶段 Future 数量没有增长。

排空后 RSS 范围 33.06–38.53 MiB；允许 Python 分配器保留内存，不要求返回冷启动值。估算预算不包含 Hook 自行分配的对象及调用方保留的结果，不能当作进程 RSS 硬限制。稳定性期间的源码摘要保持不变。

## 源码与原始数据

基线生产源码摘要：`f02c6dda29490161073856894ae068e31f84f897d39d9e60f70fed82161b0a6f`。最终源码摘要：`9e30108ab6d05d3c392ba53104eaa642e3ca550a0de49582e78cf6b1b19dba18`。最终正常、HTTP 和 30 分钟稳定性测量使用同一份生产源码，并在测量结束后检查摘要未变化。

全部源码 ZIP 的归档摘要、manifest 和生产文件逐一校验；[源码清单](../benchmarks/results/elastic/tiffany-elastic-sources.json) 保留各阶段差异，没有修改原始采样的摘要。

- [最终正常 15 轮原始数据](../benchmarks/results/elastic/tiffany-elastic-final-normal.json)
- [加长计时的最终正常 15 轮原始数据](../benchmarks/results/elastic/tiffany-elastic-final-normal-long.json)
- [最终闭环 HTTP 原始数据](../benchmarks/results/elastic/tiffany-elastic-final-http-closed.json)
- [最终单会话固定到达 / 突发数据](../benchmarks/results/elastic/tiffany-elastic-final-http-serial.json)
- [最终 512 条多会话固定到达 / 突发数据](../benchmarks/results/elastic/tiffany-elastic-final-http-arrivals.json)
- [最终 500 ms 固定到达 / 突发数据](../benchmarks/results/elastic/tiffany-elastic-final-http-slow.json)
- [最终 30 分钟原始数据](../benchmarks/results/elastic/tiffany-elastic-final-soak.json)
- [实施前工作区源码](../benchmarks/results/elastic/elastic-before-sources.zip)、[交付源码](../benchmarks/results/elastic/elastic-delivery-sources.zip)

前期正常负载 7 轮与 15 轮未通过区间判断的数据保留为 `tiffany-elastic-normal-7.json`、`tiffany-elastic-normal.json`。优化直接启动分支后的 `tiffany-elastic-normal-v2-15.json` 曾通过正常负载，`tiffany-elastic-http-closed.json` 通过闭环，`tiffany-elastic-soak.json` 通过 30 分钟；这些都是较早源码的结果。

最终源码每场景 60000 条 / 15 轮测量 `tiffany-elastic-final-normal.json` 的吞吐、延迟与 RSS 中位数都达门槛，但单 Hook、字段缓存和长消息三个配对区间仍跨过 97%，按计划标记未通过，没有把不确定结果当作通过。随后预先固定每场景 200000 条 / 15 轮、单个 CPU 与 above-normal 优先级，作为独立的加长测量；不与前一批样本合并。上方表格与正常负载验收仅引用这批加长测量，门槛保持不变。

加长测量进程在保存 27 个样本后中断，原文件保留为 [中断时的原始数据](../benchmarks/results/elastic/tiffany-elastic-final-normal-long-interrupted.json)。仅补测缺失的 3 个样本，没有挑选或删除已保存结果；恢复前核对两侧源码、解释器、依赖、CPU、优先级和全部测量参数。最终数据记录恢复时间、原文件摘要和两个调度脚本摘要，独立校验确认原样本逐项不变。恢复期间的时间间隔也是测量限制。

前期固定到达 / 突发数据 `tiffany-elastic-http-arrivals.json`、`tiffany-elastic-http-arrivals-warm.json`、`tiffany-elastic-http-arrivals-512.json` 分别包含 7、2、2 个未通过条件，全部保留。仅预热 4 次的首批短测混入建连成本；自动预热后的 64 条 / 7 轮仍有两个 5 ms 突发条件未达 95%（64 会话 93.95%、256 会话 94.65%）；512 条复测仍有 5 ms 固定到达 / 16 会话和突发 / 64 会话未通过。没有把这些失败改写为通过。随后复用直接启动分支，减少满载后续事件的就绪集合开销，并重新验收最终源码。未完成的中断诊断不作为验收数据。

## 复测

使用同一 Python 环境运行两侧；本次使用 `.build-cache/expanded-python-env/Scripts/python.exe`。所需测量依赖为 aiohttp、psutil、websockets，本项目现有扩展测量锁文件可复用，不改变核心依赖。恢复 ZIP 到独立目录后，`--baseline` / `--source` 指向相应目录；快照中的 manifest 会逐文件检查。

```powershell
$elasticPython = ".\.build-cache\expanded-python-env\Scripts\python.exe"
Expand-Archive benchmarks/results/elastic/elastic-before-sources.zip .build-cache/elastic/reproduce-before
Expand-Archive benchmarks/results/elastic/elastic-delivery-sources.zip .build-cache/elastic/reproduce-final
$elasticHttpCpus = @(4, 5, 6, 7, 8, 9, 10, 11)
& $elasticPython -B -m benchmarks.elastic --baseline .build-cache/elastic/reproduce-before --source .build-cache/elastic/reproduce-final --repeats 15 --events 200000 --cpu-affinity 0 --priority above-normal --output .build-cache/elastic/repeat-normal.json
& $elasticPython -B -m benchmarks.elastic --suite http --baseline .build-cache/elastic/reproduce-before --source .build-cache/elastic/reproduce-final --repeats 7 --loads closed --cpu-affinity $elasticHttpCpus --priority above-normal --output .build-cache/elastic/repeat-http.json
& $elasticPython -B -m benchmarks.elastic --suite http --baseline .build-cache/elastic/reproduce-before --source .build-cache/elastic/reproduce-final --repeats 7 --delays .005 .05 --session-counts 1 --loads open burst --load-events 64 --cpu-affinity $elasticHttpCpus --priority above-normal --output .build-cache/elastic/repeat-serial.json
& $elasticPython -B -m benchmarks.elastic --suite http --baseline .build-cache/elastic/reproduce-before --source .build-cache/elastic/reproduce-final --repeats 7 --delays .005 .05 --session-counts 16 64 256 --loads open burst --load-events 512 --cpu-affinity $elasticHttpCpus --priority above-normal --output .build-cache/elastic/repeat-arrivals.json
& $elasticPython -B -m benchmarks.elastic --suite http --baseline .build-cache/elastic/reproduce-before --source .build-cache/elastic/reproduce-final --repeats 1 --delays .5 --loads open burst --load-events 64 --cpu-affinity $elasticHttpCpus --priority above-normal --output .build-cache/elastic/repeat-slow.json
& $elasticPython -B -m benchmarks.elastic --soak --source .build-cache/elastic/reproduce-final --seconds 1800 --cpu-affinity 12 13 14 15 --output .build-cache/elastic/repeat-soak.json
& $elasticPython -B -m benchmarks.elastic_validate benchmarks/results/elastic/tiffany-elastic-final-normal-long.json benchmarks/results/elastic/tiffany-elastic-final-http-closed.json benchmarks/results/elastic/tiffany-elastic-final-http-serial.json benchmarks/results/elastic/tiffany-elastic-final-http-arrivals.json benchmarks/results/elastic/tiffany-elastic-final-http-slow.json benchmarks/results/elastic/tiffany-elastic-final-soak.json
python -B -m unittest discover -s tests -t . -q
```

首次 7 轮无法区分 3% 回退与噪声时，可用 `--resume 原七轮.json --repeats 15 --output 新十五轮.json` 补齐；它要求源码、解释器、依赖和测量参数完全一致。独立校验脚本从逐轮数据重算汇总、完成量、指标、源码摘要和验收结果。

正常测量意外中断时，先保留原 JSON，再用相同参数加 `--resume 中断文件.json --resume-interrupted` 补齐原计划的缺失样本。此模式不改变轮次数，逐个核验保留样本的会话条件、来源和唯一性；校验脚本会核对恢复文件摘要及保留结果。

本机完整回归使用 Windows CPython 3.14.4：234 项，4 项按平台跳过，全部通过；更新包生成和 data 排除检查通过。Linux 3.11–3.14、Windows 3.11/3.14 的原 CI 矩阵保留，本轮没有运行远端矩阵。额外 3.12 扩展环境诊断缺少 prometheus-client，且并行诊断中有一项短超时 HTTP 测试波动，未作为完整回归验收；正式性能采样不需要 exporter 依赖，其业务指标为真实注册表。
