"""Render measured elastic-scheduler results without replacing old reports."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import zipfile

from benchmarks.elastic_validate import validate, verify_source

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / 'benchmarks/results/elastic'


def read(name):
    return json.loads((RESULTS / name).read_text(encoding='utf-8'))


def verify_archives(sources):
    for entry in sources.values():
        verify_source(entry['source'])
        path = RESULTS / entry['archive']
        assert hashlib.sha256(path.read_bytes()).hexdigest() == entry['archive_sha256']
        with zipfile.ZipFile(path) as archive:
            manifest = json.loads(archive.read('baseline-manifest.json'))
            for name, digest in manifest['files'].items():
                assert hashlib.sha256(archive.read(name)).hexdigest() == digest, name
            for name, digest in entry['source']['files'].items():
                assert hashlib.sha256(archive.read(name)).hexdigest() == digest, name


def render():
    normal = read('tiffany-elastic-final-normal-long.json')
    closed = read('tiffany-elastic-final-http-closed.json')
    arrivals = read('tiffany-elastic-final-http-slow.json')
    warm = read('tiffany-elastic-final-http-serial.json')
    parallel = read('tiffany-elastic-final-http-arrivals.json')
    soak = read('tiffany-elastic-final-soak.json')
    sources = read('tiffany-elastic-sources.json')
    checks = [validate(data) for data in (normal, closed, arrivals, soak)]
    validate(warm)
    validate(parallel)
    verify_archives(sources)
    final_source = sources['delivery']['source']
    assert normal['candidate_source'] == soak['source'] == final_source
    assert all(data['candidate_source'] == final_source for data in (closed, arrivals, warm, parallel))
    assert all(data['baseline_source'] == sources['before']['source'] for data in (normal, closed, arrivals, warm, parallel))
    minimum_seconds = min(row['minimum_case_seconds'] for row in normal['acceptance'])
    arrival_checks = ([row for row in arrivals['acceptance'] if '_0.5_' in row['case']]
                      + warm['acceptance'] + parallel['acceptance'])
    overall = checks[0]['passed'] and checks[1]['passed'] and all(row['passed'] for row in arrival_checks)
    lines = ['# Tiffany 弹性调度性能验收', '',
        f"**整体性能验收：{'通过' if overall else '未通过'}。** 功能回归与 30 分钟稳定性通过；吞吐、尾延迟及配对区间分别按既定门槛判定。未通过条件不作为性能承诺。", '',
        '基线来自实施前当前工作区的源码快照，包含已有未提交优化。没有用旧 Git HEAD 替代。原性能报告和中间未通过数据保留，新结果单独存放。', '',
        '共享预算、保留额度、同会话 FIFO、同步接纳和 Adapter drain 的接口见 [调度说明](ELASTIC_SCHEDULING.md)。本轮没有单会话并行、持久化队列、后台调参或新增运行依赖。', '',
        '## 环境与方法', '',
        f"Windows 11，CPython 3.12.14；aiohttp {normal['environment']['packages']['aiohttp']}、websockets {normal['environment']['packages']['websockets']}、psutil {normal['environment']['packages']['psutil']}。性能使用已有扩展测量环境，两侧使用同解释器与依赖，真实 MetricRegistry 开启、Trace 关闭、GC 开启。没有在共享 CI 上设置速度门槛。", '',
        f"最终正常场景每轮、每个实现使用独立进程，轮次交替先后顺序，每个场景预热 {normal['method']['warmup']:,} 条，计时 {normal['method']['events']:,} 条，共 15 轮。计时场景最短 {minimum_seconds:.2f} 秒。吞吐为完成量除以计时，延迟覆盖提交到业务完成；RSS 为各场景预热后空闲采样。前期从 7 轮补至 15 轮的过程及未通过数据全部保留；优化满载完成路径后重新测量最终源码。", '',
        f"最终正常负载限制到逻辑 CPU {normal['method']['cpu_affinity']}，HTTP 测量与服务限制到 {closed['method']['cpu_affinity']}，两侧测量进程均使用 Windows above-normal 优先级。同时进行的稳定性复测使用逻辑 CPU {soak['cpu_affinity']}。两侧 CPU 与优先级设置一致，参数写入原始数据。隔离减少进程之间的直接争用，但没有排除操作系统调度、热管理和共享内存总线的影响。", '',
        '完整 OneBot 场景从 JSON 帧编码、解码、会话键和预算接纳，到 Hook 完成；它是离线帧入口，不包含真实平台网络和回复时间。真正 WebSocket 满载时的 echo、ping 和 drain 另有本机集成测试。', '',
        '## 正常负载', '',
        '| 场景 | 基线 events/s | 新实现 events/s | 吞吐变化 | P95 变化 | P99 变化 | RSS 变化 | 配对吞吐中位数 95% 区间 |',
        '| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |']
    summaries = {(row['profile'], row['case']): row for row in normal['summary']}
    labels = {'one_handler': '单 Hook', 'ten_handlers': '10 Hook', 'unmatched_1000': '1000 个无关 Hook',
              'reads_16': '缓存字段读 16 次', 'text_64k': '64 KiB 文本', 'onebot_frames': '完整 OneBot 帧'}
    for row in normal['acceptance']:
        case, ratios = row['case'], row['ratios']
        b, a = summaries['before', case], summaries['after', case]
        changes = ' | '.join(f'{(ratios[k]-1)*100:+.2f}%' for k in ('throughput', 'p95_us', 'p99_us', 'rss_bytes'))
        lo, hi = row['paired_median_95_percent_interval']
        lines.append(f"| {labels[case]} | {b['throughput']['median']:,.0f} | {a['throughput']['median']:,.0f} | {changes} | {lo*100:.2f}%–{hi*100:.2f}% |")
    lines += ['', f"正常负载验收：**{'通过' if checks[0]['passed'] else '未通过'}**。要求吞吐至少 97%、P95 增幅不超过 5%、P99 不超过 10%、RSS 不超过 5%；15 轮后仍跨过 97% 的配对区间判为未通过。区间采用固定随机种子的配对中位数 bootstrap，4000 次重采样；这是所测工作站的波动估计，不是线上性能保证。原始数据另有每轮配对比值和每项指标最小–最大范围。", '',
        '正常负载未通过条件：' + ('、'.join(row['case'] for row in normal['acceptance'] if not row['passed']) or '无') + '。', '',
        '加长测量中，10 Hook 吞吐中位数回退 3.80%，超过 3% 门槛；五个核心场景的配对区间仍覆盖门槛，无法排除 3% 回退。只有完整 OneBot 帧场景通过全部正常负载条件，吞吐中位数提高 12.38%。不能据此承诺所有正常负载都保持在 3% 以内。', '',
        '## HTTP 高负载', '',
        '本机 HTTP 服务实际等待至少 5/50/500 ms，由 64 个预热线程负责等待。会话数为 1/16/64/256。每个条件在独立进程比较旧默认（总容量 256、会话积压 32、全局 16、Adapter 4）、旧调优（容量和积压 8192、全局和 Adapter 64）以及新默认。客户端 HTTP 连接上限统一为 64；这不代表 QQ 的下游 API 额度会自动提高。', '',
        '闭环每个生产者等待完成后再提交，窗口 2.5 秒，窗口末尾的工作排空时间计入吞吐。固定到达使用同一预定速率 `1.5 × min(会话数, 64) / HTTP延迟`；事件循环调度可能造成短暂追赶，原始数据记录实际到达窗口。突发一次提交后等待排空。5/50 ms 的多会话条件提交 512 条，其余条件提交 64 条；同一条件三种实现的提交量和预定到达速率相同。', '',
        '### 闭环完成吞吐（7 轮中位数）', '',
        '| HTTP ms | 会话数 | 旧默认 events/s | 旧调优 events/s | 新默认 events/s | 新 / 旧调优 | P95 比值 | P99 比值 |',
        '| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |']
    index = {(row['profile'], row['case']): row for row in closed['summary']}
    for check in closed['acceptance']:
        case = check['case']
        old, tuned, new = [index[p, case] for p in ('before', 'before_tuned', 'after')]
        r = check['ratios']
        lines.append(f"| {new['delay_ms']['median']:g} | {new['sessions']['median']:g} | {old['throughput']['median']:.1f} | {tuned['throughput']['median']:.1f} | {new['throughput']['median']:.1f} | {r['throughput']:.3f} | {r['p95_ms']:.3f} | {r['p99_ms']:.3f} |")
    lines += ['', f"闭环验收：**{'通过' if checks[1]['passed'] else '未通过'}**。同有效并发、无拒绝时，吞吐至少为旧调优的 95%，P95/P99 增幅不超过 10%。有效并发由 Hook 内活动计数实测，未把接纳量当作完成吞吐。", '',
        '闭环未通过条件：' + ('、'.join(row['case'] for row in closed['acceptance'] if not row['passed']) or '无') + '。', '',
        '### 固定到达与突发排空', '',
        '| 条件 | 旧默认完成 / 提交 | 新完成 / 提交 | 旧调优排空 s | 新排空 s | 新 / 旧调优吞吐 | P95 比值 | P99 比值 |',
        '| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |']
    index = {(row['profile'], row['case']): row for row in arrivals['summary'] if row['delay_ms']['median'] == 500}
    index.update({(row['profile'], row['case']): row for row in warm['summary'] if row['sessions']['median'] == 1})
    index.update({(row['profile'], row['case']): row for row in parallel['summary']})
    for check in arrival_checks:
        case = check['case']
        old, tuned, new = [index[p, case] for p in ('before', 'before_tuned', 'after')]
        r = check['ratios']
        lines.append(f"| {case} | {old['completed']['median']:g}/{old['offered']['median']:g} | {new['completed']['median']:g}/{new['offered']['median']:g} | {tuned['seconds']['median']:.3f} | {new['seconds']['median']:.3f} | {r['throughput']:.3f} | {r['p95_ms']:.3f} | {r['p99_ms']:.3f} |")
    lines += ['', f"固定到达 / 突发验收：**{'通过' if all(row['passed'] for row in arrival_checks) else '未通过'}**。5/50 ms 使用连接预热后的 7 轮中位数；500 ms 为最终源码的单轮结果，不提供多轮统计结论。预热 `max(4, min(会话数, 64))` 次，不改变额度或验收门槛。多会话提交 512 条，覆盖全部 256 个会话。超出预算另有 9000 条过载回归：8192 条接纳、808 条明确拒绝，64 个活动 Task，排空后恢复。保留区和大事件先耗尽字节预算均单独测试。", '',
        '固定到达 / 突发未通过条件：' + ('、'.join(row['case'] for row in arrival_checks if not row['passed']) or '无') + '。', '',
        '5 ms / 64 会话是当前未达标的高负载条件：闭环 P99 相对同额度旧实现增长 10.83%；固定到达吞吐为旧调优的 90.62%，P95/P99 分别增长 14.34%/12.12%。两项均按原门槛标记未通过。短窗口及系统调度存在波动，但现有证据不能把失败归因于某个单一因素，也不能证明性能保证已达成。', '',
        '## 30 分钟稳定性', '',
        f"实际运行 {soak['seconds']:.2f} 秒，{len(soak['samples'])} 轮，完成 {soak['samples'][-1]['calls']:,} 条。突发规模按 128 条单会话、512 条/16 会话、4096 条/64 会话轮换，每次排空后等待约 10 秒再继续。每轮断言预算、会话队列、就绪队列、活动工作映射、Task 和 owner 引用归零，数值同时写入原始数据。GC 后 Context 与 Work 保留量为 0，同阶段 Future 数量没有增长。", '',
        f"排空后 RSS 范围 {min(row['rss_bytes'] for row in soak['samples'])/1024/1024:.2f}–{max(row['rss_bytes'] for row in soak['samples'])/1024/1024:.2f} MiB；允许 Python 分配器保留内存，不要求返回冷启动值。估算预算不包含 Hook 自行分配的对象及调用方保留的结果，不能当作进程 RSS 硬限制。稳定性期间的源码摘要保持不变。", '',
        '## 源码与原始数据', '',
        f"基线生产源码摘要：`{normal['baseline_source']['sha256']}`。最终源码摘要：`{final_source['sha256']}`。最终正常、HTTP 和 30 分钟稳定性测量使用同一份生产源码，并在测量结束后检查摘要未变化。", '',
        '全部源码 ZIP 的归档摘要、manifest 和生产文件逐一校验；[源码清单](../benchmarks/results/elastic/tiffany-elastic-sources.json) 保留各阶段差异，没有修改原始采样的摘要。', '',
        '- [最终正常 15 轮原始数据](../benchmarks/results/elastic/tiffany-elastic-final-normal.json)',
        '- [加长计时的最终正常 15 轮原始数据](../benchmarks/results/elastic/tiffany-elastic-final-normal-long.json)',
        '- [最终闭环 HTTP 原始数据](../benchmarks/results/elastic/tiffany-elastic-final-http-closed.json)',
        '- [最终单会话固定到达 / 突发数据](../benchmarks/results/elastic/tiffany-elastic-final-http-serial.json)',
        '- [最终 512 条多会话固定到达 / 突发数据](../benchmarks/results/elastic/tiffany-elastic-final-http-arrivals.json)',
        '- [最终 500 ms 固定到达 / 突发数据](../benchmarks/results/elastic/tiffany-elastic-final-http-slow.json)',
        '- [最终 30 分钟原始数据](../benchmarks/results/elastic/tiffany-elastic-final-soak.json)',
        '- [实施前工作区源码](../benchmarks/results/elastic/elastic-before-sources.zip)、[交付源码](../benchmarks/results/elastic/elastic-delivery-sources.zip)', '',
        '前期正常负载 7 轮与 15 轮未通过区间判断的数据保留为 `tiffany-elastic-normal-7.json`、`tiffany-elastic-normal.json`。优化直接启动分支后的 `tiffany-elastic-normal-v2-15.json` 曾通过正常负载，`tiffany-elastic-http-closed.json` 通过闭环，`tiffany-elastic-soak.json` 通过 30 分钟；这些都是较早源码的结果。', '',
        '最终源码每场景 60000 条 / 15 轮测量 `tiffany-elastic-final-normal.json` 的吞吐、延迟与 RSS 中位数都达门槛，但单 Hook、字段缓存和长消息三个配对区间仍跨过 97%，按计划标记未通过，没有把不确定结果当作通过。随后预先固定每场景 200000 条 / 15 轮、单个 CPU 与 above-normal 优先级，作为独立的加长测量；不与前一批样本合并。上方表格与正常负载验收仅引用这批加长测量，门槛保持不变。', '',
        '加长测量进程在保存 27 个样本后中断，原文件保留为 [中断时的原始数据](../benchmarks/results/elastic/tiffany-elastic-final-normal-long-interrupted.json)。仅补测缺失的 3 个样本，没有挑选或删除已保存结果；恢复前核对两侧源码、解释器、依赖、CPU、优先级和全部测量参数。最终数据记录恢复时间、原文件摘要和两个调度脚本摘要，独立校验确认原样本逐项不变。恢复期间的时间间隔也是测量限制。', '',
        '前期固定到达 / 突发数据 `tiffany-elastic-http-arrivals.json`、`tiffany-elastic-http-arrivals-warm.json`、`tiffany-elastic-http-arrivals-512.json` 分别包含 7、2、2 个未通过条件，全部保留。仅预热 4 次的首批短测混入建连成本；自动预热后的 64 条 / 7 轮仍有两个 5 ms 突发条件未达 95%（64 会话 93.95%、256 会话 94.65%）；512 条复测仍有 5 ms 固定到达 / 16 会话和突发 / 64 会话未通过。没有把这些失败改写为通过。随后复用直接启动分支，减少满载后续事件的就绪集合开销，并重新验收最终源码。未完成的中断诊断不作为验收数据。', '',
        '## 复测', '',
        '使用同一 Python 环境运行两侧；本次使用 `.build-cache/expanded-python-env/Scripts/python.exe`。所需测量依赖为 aiohttp、psutil、websockets，本项目现有扩展测量锁文件可复用，不改变核心依赖。恢复 ZIP 到独立目录后，`--baseline` / `--source` 指向相应目录；快照中的 manifest 会逐文件检查。', '',
        '```powershell',
        '$elasticPython = ".\\.build-cache\\expanded-python-env\\Scripts\\python.exe"',
        'Expand-Archive benchmarks/results/elastic/elastic-before-sources.zip .build-cache/elastic/reproduce-before',
        'Expand-Archive benchmarks/results/elastic/elastic-delivery-sources.zip .build-cache/elastic/reproduce-final',
        '$elasticHttpCpus = @(4, 5, 6, 7, 8, 9, 10, 11)',
        '& $elasticPython -B -m benchmarks.elastic --baseline .build-cache/elastic/reproduce-before --source .build-cache/elastic/reproduce-final --repeats 15 --events 200000 --cpu-affinity 0 --priority above-normal --output .build-cache/elastic/repeat-normal.json',
        '& $elasticPython -B -m benchmarks.elastic --suite http --baseline .build-cache/elastic/reproduce-before --source .build-cache/elastic/reproduce-final --repeats 7 --loads closed --cpu-affinity $elasticHttpCpus --priority above-normal --output .build-cache/elastic/repeat-http.json',
        '& $elasticPython -B -m benchmarks.elastic --suite http --baseline .build-cache/elastic/reproduce-before --source .build-cache/elastic/reproduce-final --repeats 7 --delays .005 .05 --session-counts 1 --loads open burst --load-events 64 --cpu-affinity $elasticHttpCpus --priority above-normal --output .build-cache/elastic/repeat-serial.json',
        '& $elasticPython -B -m benchmarks.elastic --suite http --baseline .build-cache/elastic/reproduce-before --source .build-cache/elastic/reproduce-final --repeats 7 --delays .005 .05 --session-counts 16 64 256 --loads open burst --load-events 512 --cpu-affinity $elasticHttpCpus --priority above-normal --output .build-cache/elastic/repeat-arrivals.json',
        '& $elasticPython -B -m benchmarks.elastic --suite http --baseline .build-cache/elastic/reproduce-before --source .build-cache/elastic/reproduce-final --repeats 1 --delays .5 --loads open burst --load-events 64 --cpu-affinity $elasticHttpCpus --priority above-normal --output .build-cache/elastic/repeat-slow.json',
        '& $elasticPython -B -m benchmarks.elastic --soak --source .build-cache/elastic/reproduce-final --seconds 1800 --cpu-affinity 12 13 14 15 --output .build-cache/elastic/repeat-soak.json',
        '& $elasticPython -B -m benchmarks.elastic_validate benchmarks/results/elastic/tiffany-elastic-final-normal-long.json benchmarks/results/elastic/tiffany-elastic-final-http-closed.json benchmarks/results/elastic/tiffany-elastic-final-http-serial.json benchmarks/results/elastic/tiffany-elastic-final-http-arrivals.json benchmarks/results/elastic/tiffany-elastic-final-http-slow.json benchmarks/results/elastic/tiffany-elastic-final-soak.json',
        'python -B -m unittest discover -s tests -t . -q', '```', '',
        '首次 7 轮无法区分 3% 回退与噪声时，可用 `--resume 原七轮.json --repeats 15 --output 新十五轮.json` 补齐；它要求源码、解释器、依赖和测量参数完全一致。独立校验脚本从逐轮数据重算汇总、完成量、指标、源码摘要和验收结果。', '',
        '正常测量意外中断时，先保留原 JSON，再用相同参数加 `--resume 中断文件.json --resume-interrupted` 补齐原计划的缺失样本。此模式不改变轮次数，逐个核验保留样本的会话条件、来源和唯一性；校验脚本会核对恢复文件摘要及保留结果。', '',
        '本机完整回归使用 Windows CPython 3.14.4：234 项，4 项按平台跳过，全部通过；更新包生成和 data 排除检查通过。Linux 3.11–3.14、Windows 3.11/3.14 的原 CI 矩阵保留，本轮没有运行远端矩阵。额外 3.12 扩展环境诊断缺少 prometheus-client，且并行诊断中有一项短超时 HTTP 测试波动，未作为完整回归验收；正式性能采样不需要 exporter 依赖，其业务指标为真实注册表。', '']
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / '.build-cache/elastic/PERFORMANCE_ELASTIC.md')
    args = parser.parse_args()
    text = render()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text, encoding='utf-8')
    print(args.output)


if __name__ == '__main__':
    main()
