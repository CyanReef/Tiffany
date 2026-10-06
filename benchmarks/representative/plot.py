"""Render the representative comparison in the original card-based layout."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import FancyBboxPatch, Patch
from matplotlib.ticker import FuncFormatter, NullLocator

from benchmarks.plot import chart_font
from benchmarks.representative.cases import FRAMEWORKS
from benchmarks.representative.presentation import (
    FIGURE_COUNT, IO_SESSIONS, PEAK_MEMORY_CASES, STORAGE_CASES,
)
from benchmarks.representative.validate import validate

ROOT = Path(__file__).resolve().parents[2]
LABELS = {"tiffany": "Tiffany", "nonebot": "NoneBot", "astrbot": "AstrBot", "koishi": "Koishi"}
ROLES = {"tiffany": "原始事件 / 按需字段", "nonebot": "Python 插件框架", "astrbot": "AI 机器人应用", "koishi": "Node.js 插件框架"}
COLORS = {"tiffany": "#0A9981", "nonebot": "#5475EA", "astrbot": "#D8872C", "koishi": "#9A68D3"}
MARKERS = {"tiffany": "o", "nonebot": "s", "astrbot": "^", "koishi": "D"}
BG, INK, MUTED, GRID = "#F3F5F8", "#172E45", "#67768A", "#E7EBF0"
MIB = 1024 ** 2


def frame(title, subtitle, number, size=(16, 10)):
    fig = plt.figure(figsize=size, facecolor=BG)
    fig.text(.045, .949, title, color=INK, fontsize=25, weight="bold")
    fig.text(.045, .906, subtitle, color=MUTED, fontsize=11)
    fig.text(.954, .952, f"{number:02d} / {FIGURE_COUNT:02d}", ha="right", color=MUTED, fontsize=11)
    return fig


def card(fig, box):
    fig.add_artist(FancyBboxPatch(
        (box[0], box[1]), box[2], box[3], transform=fig.transFigure,
        boxstyle="round,pad=0.008,rounding_size=0.014", facecolor="white",
        edgecolor="none", zorder=-1,
    ))


def panel(fig, box, title, note, *, bottom=.085, top=.19):
    card(fig, box)
    fig.text(box[0] + .019, box[1] + box[3] - .037,
             title, color=INK, fontsize=13, weight="bold")
    fig.text(box[0] + .019, box[1] + box[3] - .071,
             note, color=MUTED, fontsize=9)
    ax = fig.add_axes([box[0] + .057, box[1] + bottom,
                       box[2] - .08, box[3] - top - bottom + .085])
    ax.set_facecolor("white")
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.tick_params(length=0, colors=MUTED, labelsize=9)
    ax.set_axisbelow(True)
    ax.grid(axis="x", color=GRID, linewidth=.8)
    return ax


def legend(fig, y=.865):
    fig.legend(
        handles=[Line2D([], [], color=COLORS[f], marker=MARKERS[f],
                        linewidth=2, label=LABELS[f]) for f in FRAMEWORKS],
        loc="center left", bbox_to_anchor=(.044, y), ncol=4,
        frameon=False, fontsize=11, columnspacing=3.6,
    )


def footer(fig, data):
    fig.text(.045, .037,
             f"{data['method']['repeats']} 轮中位数 · 误差线与阴影表示最小—最大范围，非置信区间 · 测试条件与指标定义见报告",
             color=MUTED, fontsize=9)


def save(fig, folder, name):
    folder.mkdir(parents=True, exist_ok=True)
    for suffix in ("png", "svg"):
        fig.savefig(folder / f"framework-representative-{name}.{suffix}",
                    dpi=300, facecolor=BG)
    plt.close(fig)


def render(data, folder):
    validate(data)
    plt.rcParams.update({
        "font.family": chart_font("zh-CN"), "font.size": 10,
        "svg.hashsalt": "tiffany-representative", "axes.unicode_minus": False,
    })
    rows = {(row["framework"], row["case"]): row for row in data["summary"]}

    def values(case, metric, divisor=1):
        selected = [rows[(f, case)][metric] for f in FRAMEWORKS]
        medians = [r["median"] / divisor for r in selected]
        errors = (
            [v - r["min"] / divisor for v, r in zip(medians, selected)],
            [r["max"] / divisor - v for v, r in zip(medians, selected)],
        )
        return medians, errors, selected

    def curve(ax, framework, cases, xs, metric="events_per_second", divisor=1):
        records = [rows[(framework, case)][metric] for case in cases]
        ax.plot(xs, [r["median"] / divisor for r in records],
                color=COLORS[framework], marker=MARKERS[framework],
                markersize=4, linewidth=1.8)
        ax.fill_between(xs, [r["min"] / divisor for r in records],
                        [r["max"] / divisor for r in records],
                        color=COLORS[framework], alpha=.1)

    fig = frame("消息处理性能与资源占用", "单处理器场景 · 吞吐条形图使用统一量程 · 完整数值表见报告", 1)
    rate_limit = max(rows[(f, "one_handler")]["events_per_second"]["max"]
                     for f in FRAMEWORKS) * 1.02
    for i, framework in enumerate(FRAMEWORKS):
        x, y, width, height = .045 + i * .235, .621, .216, .245
        card(fig, (x, y, width, height))
        fig.text(x + .016, y + .203, LABELS[framework],
                 color=COLORS[framework], weight="bold", fontsize=18)
        fig.text(x + .016, y + .175, ROLES[framework], color=MUTED, fontsize=9)
        rate = rows[(framework, "one_handler")]["events_per_second"]
        fig.text(x + .016, y + .12, f"{rate['median']:,.0f}",
                 color=INK, weight="bold", fontsize=28)
        fig.text(x + .016, y + .093, "事件 / 秒 · 越大越好", color=MUTED, fontsize=9)
        bar = fig.add_axes([x + .016, y + .061, width - .033, .019])
        bar.barh([0], [rate["median"]], color=COLORS[framework], height=.75,
                 xerr=[[rate["median"] - rate["min"]], [rate["max"] - rate["median"]]],
                 error_kw={"elinewidth": .8, "ecolor": INK, "capsize": 2})
        bar.set_xlim(0, rate_limit)
        bar.set_ylim(-.5, .5)
        bar.set_axis_off()
        fig.text(x + .016, y + .039, f"各轮范围  {rate['min']:,.0f}–{rate['max']:,.0f}",
                 color=MUTED, fontsize=9)
        initialization = statistics.median(
            s["component_ready_seconds"] for s in data["samples"] if s["framework"] == framework
        )
        fig.text(x + .016, y + .014, f"测试进程初始化   {initialization:.2f} s",
                 color=INK, fontsize=9)

    ax = panel(fig, (.045, .109, .289, .471),
               "(a) 事件完成延迟", "µs · 越小越好 · 横轴为对数刻度")
    for i, framework in enumerate(FRAMEWORKS):
        quantiles = [rows[(framework, "one_handler")][key]["median"]
                     for key in ("latency_p50_us", "latency_p95_us", "latency_p99_us")]
        ax.plot(quantiles, [i] * 3, color=COLORS[framework], linewidth=2, alpha=.5)
        for quantile, marker in zip(quantiles, ("o", "s", "D")):
            ax.scatter(quantile, i, marker=marker, s=43, color=COLORS[framework],
                       edgecolors="white", linewidth=.6, zorder=3)
        ax.annotate(f"P99 {quantiles[2]:,.0f}", (quantiles[2], i),
                    xytext=(5, 9), textcoords="offset points", color=INK, fontsize=8)
    ax.set_yticks(range(4), [LABELS[f] for f in FRAMEWORKS])
    ax.set_ylim(3.55, -.55)
    ax.set_xscale("log")
    ax.set_xlim(20, max(rows[(f, "one_handler")]["latency_p99_us"]["median"]
                       for f in FRAMEWORKS) * 2.3)
    ax.set_xticks([30, 100, 300, 1000, 3000], ["30", "100", "300", "1,000", "3,000"])
    ax.xaxis.set_minor_locator(NullLocator())
    ax.legend(
        handles=[Line2D([], [], marker=marker, color=MUTED, linestyle="", label=label)
                 for marker, label in (("o", "P50"), ("s", "P95"), ("D", "P99"))],
        loc="upper left", frameon=False, ncol=3, bbox_to_anchor=(-.23, -.16),
        fontsize=8, columnspacing=1,
    )

    ax = panel(fig, (.358, .109, .289, .471),
               "(b) 进程工作集", "MiB · 预热后 / 采样峰值 · 越小越好")
    medians, _, _ = values("one_handler", "rss_before_bytes", MIB)
    peaks, _, _ = values("one_handler", "sampled_peak_rss_bytes", MIB)
    ax.barh(range(4), medians, color=[COLORS[f] for f in FRAMEWORKS], height=.46)
    ax.scatter(peaks, range(4), marker="|", s=130, color=INK, zorder=3)
    ax.set_yticks(range(4), [LABELS[f] for f in FRAMEWORKS])
    ax.set_ylim(3.55, -.55)
    ax.set_xlim(0, max(peaks) * 1.31)
    for i, (value, peak) in enumerate(zip(medians, peaks)):
        ax.text(peak + max(peaks) * .035, i, f"{value:.1f} / {peak:.1f}",
                va="center", color=INK, fontsize=8)

    ax = panel(fig, (.672, .109, .289, .471),
               "(c) 单位事件 CPU 时间", "µs / 事件 · 越小越好 · 包含进程全部线程")
    medians, errors, selected = values("one_handler", "cpu_us_per_event")
    ax.barh(range(4), medians, color=[COLORS[f] for f in FRAMEWORKS],
            height=.46, xerr=errors,
            error_kw={"elinewidth": .9, "capsize": 2, "ecolor": INK})
    ax.set_yticks(range(4), [LABELS[f] for f in FRAMEWORKS])
    ax.set_ylim(3.55, -.55)
    ax.set_xlim(0, max(r["max"] for r in selected) * 1.35)
    for i, (value, record) in enumerate(zip(medians, selected)):
        ax.text(record["max"] + ax.get_xlim()[1] * .025, i, f"{value:,.0f}",
                va="center", color=INK, fontsize=9)
    footer(fig, data)
    save(fig, folder, "overview")

    fig = frame("处理器、过滤规则与消息规模",
                "六类工作负载 · 保留各框架原生解析、过滤和字段读取路径", 2, (16, 12))
    legend(fig)
    plots = (
        ("(a) 匹配处理器数量", "每条事件执行全部匹配处理器",
         ["one_handler", "ten_handlers", "fifty_handlers"], [1, 10, 50],
         ["1", "10", "50"], "处理器数量（对数）", True),
        ("(b) 其他类别处理器数量", "额外处理器监听其他事件类别",
         ["one_handler", "category_100", "category_1000"], [0, 100, 1000],
         ["0", "100", "1,000"], "额外处理器数量", False),
        ("(c) 恒假过滤规则数量", "规则返回 False，对应处理器不执行",
         ["one_handler", "predicate_100", "predicate_1000"], [0, 100, 1000],
         ["0", "100", "1,000"], "额外规则数量", False),
        ("(d) ASCII 文本长度", "每条事件读取一次完整文本",
         ["one_handler", "text_4k", "text_64k"], [5, 4096, 65536],
         ["5 B", "4 KiB", "64 KiB"], "文本长度（对数）", True),
        ("(e) 文本分段数量", "OneBot 文本段 / Satori 文本元素",
         ["one_handler", "segments_20", "segments_200"], [1, 20, 200],
         ["1", "20", "200"], "文本分段数量（对数）", True),
        ("(f) 文本字段读取次数", "每条事件执行一个处理器",
         ["one_handler", "reads_16"], [1, 16],
         ["1", "16"], "每事件读取次数", False),
    )
    for i, (title, note, cases, xs, labels, xlabel, log_x) in enumerate(plots):
        column, row = i % 3, i // 3
        ax = panel(fig, (.045 + column * .3135, .472 if row == 0 else .105, .29, .334),
                   title, note)
        for framework in FRAMEWORKS:
            curve(ax, framework, cases, xs)
        if log_x:
            ax.set_xscale("log")
            ax.xaxis.set_minor_locator(NullLocator())
        ax.set_xticks(xs, labels)
        ax.set_xlabel(xlabel, color=MUTED, fontsize=8)
        ax.set_yscale("log")
        ax.yaxis.set_minor_locator(NullLocator())
        ax.yaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:,.0f}"))
        ax.set_ylabel("事件 / 秒 · 越大越好 · 对数", color=MUTED, fontsize=8)
        ax.grid(axis="y", color=GRID)
    footer(fig, data)
    save(fig, folder, "scaling")

    fig = frame("HTTP I/O 性能与默认突发接纳状态",
                "I/O 请求共用至少等待 5 ms 的 HTTP 服务 · 突发场景使用框架默认限制", 3, (16, 12))
    legend(fig)
    for column, metric, title, divisor, note in (
        (0, "events_per_second", "(a) I/O 事件吞吐量", 1, "事件 / 秒 · 越大越好"),
        (1, "latency_p95_us", "(b) I/O 事件完成延迟", 1000, "P95 ms · 越小越好 · 对数刻度"),
    ):
        ax = panel(fig, (.045 + column * .471, .475, .445, .33), title, note)
        sessions = IO_SESSIONS
        for framework in FRAMEWORKS:
            curve(ax, framework, [f"io_{n}" for n in sessions], sessions, metric, divisor)
        ax.set_xscale("log", base=2)
        ax.xaxis.set_minor_locator(NullLocator())
        ax.set_xticks(sessions, [str(n) for n in sessions])
        ax.set_xlabel("闭环会话生产者数量（对数）", color=MUTED, fontsize=9)
        ax.grid(axis="y", color=GRID)
        if column:
            ax.set_yscale("log")
            ax.yaxis.set_minor_locator(NullLocator())
            ax.yaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:g}"))
        else:
            ax.set_ylim(bottom=0)
            ax.yaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:,.0f}"))

    states = (
        ("已进入处理器（等待释放）", "#0A9981"),
        ("已接纳但未进入处理器", "#B4C5DC"),
        ("接纳拒绝", "#E4796A"),
    )
    for column, name, title in (
        (0, "burst_single", "(c) 单会话突发 · 提交 128 条"),
        (1, "burst_multi", "(d) 16 会话突发 · 提交 512 条"),
    ):
        ax = panel(fig, (.045 + column * .471, .135, .445, .296), title,
                   "进入数量持续至少 100 ms 不再变化后观测，再释放处理器",
                   bottom=.057, top=.17)
        entered, rejected, submitted = [], [], []
        for framework in FRAMEWORKS:
            observed = [
                (c["blocked_handlers"], c["rejected"], c["submitted"])
                for s in data["samples"] if s["framework"] == framework
                for c in s["cases"] if c["name"] == name
            ]
            if len(set(observed)) != 1:
                raise ValueError("burst states vary across repeats; show their distribution instead")
            active, refused, total = observed[0]
            entered.append(active)
            rejected.append(refused)
            submitted.append(total)
        waiting = [n - a - r for n, a, r in zip(submitted, entered, rejected)]
        bottom = [0] * len(FRAMEWORKS)
        for index, (counts, (_, color)) in enumerate(zip((entered, waiting, rejected), states)):
            ax.bar(range(4), counts, bottom=bottom, color=color, width=.46)
            for x, (base, count, total) in enumerate(zip(bottom, counts, submitted)):
                if not count:
                    continue
                if count >= total * .08:
                    ax.text(x, base + count / 2, str(count), ha="center", va="center",
                            color="white" if index == 0 else INK, fontsize=9)
                else:
                    ax.annotate(
                        str(count), (x, base + count / 2), xytext=(-21, 8),
                        textcoords="offset points", ha="right", va="bottom",
                        color=INK, fontsize=9,
                        arrowprops={"arrowstyle": "-", "color": INK, "linewidth": .7},
                    )
            bottom = [base + count for base, count in zip(bottom, counts)]
        ax.set_xticks(range(4), [LABELS[f] for f in FRAMEWORKS])
        ax.set_ylim(0, submitted[0] * 1.08)
        ax.grid(axis="y", color=GRID)
        ax.set_ylabel("事件数（条）", color=MUTED, fontsize=9)
    fig.legend(
        handles=[Patch(color=color, label=label) for label, color in states],
        loc="center left", bbox_to_anchor=(.044, .094), ncol=3, frameon=False,
        fontsize=9, columnspacing=2,
    )
    footer(fig, data)
    save(fig, folder, "load")

    fig = frame("突发事件完成数与同会话并发",
                "完成数在释放信号后核验 · 同会话并发在等待信号期间观测", 4, (16, 12))
    for row, case, label in (
        (0, "burst_single", "单会话 · 提交 128 条"),
        (1, "burst_multi", "16 会话 · 提交 512 条"),
    ):
        for column, metric, title, unit in (
            (0, "completed", "释放后完成事件数", "条"),
            (1, "max_same_session_overlap", "同会话处理器最大并发", "个"),
        ):
            index = row * 2 + column
            ax = panel(fig, (.045 + column * .471, .472 if row == 0 else .105, .445, .334),
                       f"({chr(97 + index)}) {title}", f"{label} · 单位：{unit}")
            counts, _, _ = values(case, metric)
            ax.barh(range(4), counts, color=[COLORS[f] for f in FRAMEWORKS], height=.46)
            ax.set_yticks(range(4), [LABELS[f] for f in FRAMEWORKS])
            ax.set_ylim(3.55, -.55)
            ax.set_xlim(0, max(counts) * 1.2)
            ax.set_xlabel(f"{title}（{unit}）", color=MUTED, fontsize=9)
            for i, count in enumerate(counts):
                observations = [
                    c[metric] for s in data["samples"] if s["framework"] == FRAMEWORKS[i]
                    for c in s["cases"] if c["name"] == case
                ]
                if len(set(observations)) != 1:
                    raise ValueError("burst completion or concurrency varies; show its distribution instead")
                ax.text(count + max(counts) * .035, i, f"{count:.0f}",
                        va="center", color=INK, fontsize=9)
    fig.text(.045, .037, "各轮计数一致 · 完成数与并发数是不同观测量 · 对应数值表见报告",
             color=MUTED, fontsize=9)
    save(fig, folder, "burst")

    fig = frame("进程初始化、内存与 HTTP 服务端等待",
                "初始化与内存反映测试进程 · 服务端等待用于核验共享 I/O 条件", 5, (16, 12))
    legend(fig)
    ax = panel(fig, (.045, .472, .445, .334), "(a) 测试进程初始化时间",
               "s · 越小越好 · 包含测试程序与 SDK 初始化")
    initializations = [
        [s["component_ready_seconds"] for s in data["samples"] if s["framework"] == f]
        for f in FRAMEWORKS
    ]
    medians = [statistics.median(v) for v in initializations]
    maximum = max(max(v) for v in initializations)
    errors = ([m - min(v) for m, v in zip(medians, initializations)],
              [max(v) - m for m, v in zip(medians, initializations)])
    ax.barh(range(4), medians, color=[COLORS[f] for f in FRAMEWORKS], height=.46,
            xerr=errors, error_kw={"elinewidth": .9, "capsize": 2, "ecolor": INK})
    ax.set_yticks(range(4), [LABELS[f] for f in FRAMEWORKS])
    ax.set_ylim(3.55, -.55)
    ax.set_xlim(0, maximum * 1.2)
    ax.set_xlabel("时间（s）", color=MUTED, fontsize=9)
    for i, (value, observed) in enumerate(zip(medians, initializations)):
        ax.text(max(observed) + maximum * .025, i, f"{value:.2f}",
                va="center", color=INK, fontsize=9)

    ax = panel(fig, (.516, .472, .445, .334), "(b) 各场景采样峰值工作集",
               "MiB · 越小越好 · 包含进程内保留的缓存和堆空间")
    peak_max = 0
    for i, framework in enumerate(FRAMEWORKS):
        records = [rows[(framework, case)]["sampled_peak_rss_bytes"] for case, _ in PEAK_MEMORY_CASES]
        medians = [r["median"] / MIB for r in records]
        peak_max = max(peak_max, *(r["max"] / MIB for r in records))
        ys = [j + (i - 1.5) * .14 for j in range(len(PEAK_MEMORY_CASES))]
        errors = ([m - r["min"] / MIB for m, r in zip(medians, records)],
                  [r["max"] / MIB - m for m, r in zip(medians, records)])
        ax.errorbar(medians, ys, xerr=errors, color=COLORS[framework],
                    marker=MARKERS[framework], linestyle="", markersize=4,
                    elinewidth=.8, capsize=2)
    ax.set_yticks(range(len(PEAK_MEMORY_CASES)), [label for _, label in PEAK_MEMORY_CASES])
    ax.tick_params(axis="y", labelsize=8)
    ax.set_ylim(len(PEAK_MEMORY_CASES) - .5, -.5)
    ax.set_xlim(0, peak_max * 1.08)
    ax.set_xlabel("采样峰值工作集（MiB）", color=MUTED, fontsize=9)

    for column, metric, title in (
        (0, "service_delay_p50_us", "(c) 服务端实际等待 P50"),
        (1, "service_delay_p95_us", "(d) 服务端实际等待 P95"),
    ):
        ax = panel(fig, (.045 + column * .471, .105, .445, .334), title,
                   "ms · 每请求至少等待 5 ms · 不等同于事件完成延迟")
        for framework in FRAMEWORKS:
            curve(ax, framework, [f"io_{n}" for n in IO_SESSIONS], IO_SESSIONS, metric, 1000)
        ax.axhline(5, color=MUTED, linestyle="--", linewidth=.8)
        ax.set_xscale("log", base=2)
        ax.xaxis.set_minor_locator(NullLocator())
        ax.set_xticks(IO_SESSIONS, [str(n) for n in IO_SESSIONS])
        ax.set_xlabel("闭环会话生产者数量（对数）", color=MUTED, fontsize=9)
        ax.set_ylabel("实际等待（ms）", color=MUTED, fontsize=9)
        ax.set_ylim(bottom=4.9)
        ax.grid(axis="y", color=GRID)
    footer(fig, data)
    save(fig, folder, "resources")

    fig = frame("AstrBot 配置读取路径对比",
                "同一消息处理路径 · 配置写入缓存与 SQLite 默认配置缺失分别测量", 6, (16, 8))
    for column, metric, title, divisor, note, digits in (
        (0, "events_per_second", "(a) 事件吞吐量", 1, "事件 / 秒 · 越大越好", 0),
        (1, "latency_p95_us", "(b) P95 事件完成延迟", 1000, "ms · 越小越好", 2),
    ):
        ax = panel(fig, (.045 + column * .471, .19, .445, .57), title, note)
        records = [rows[("astrbot", case)][metric] for case, _ in STORAGE_CASES]
        medians = [r["median"] / divisor for r in records]
        errors = ([m - r["min"] / divisor for m, r in zip(medians, records)],
                  [r["max"] / divisor - m for m, r in zip(medians, records)])
        maximum = max(r["max"] / divisor for r in records)
        ax.barh(range(2), medians, color=[COLORS["astrbot"], "#BF7431"], height=.38,
                xerr=errors, error_kw={"elinewidth": .9, "capsize": 2, "ecolor": INK})
        ax.set_yticks(range(2), ["配置写入\n缓存", "SQLite 默认\n配置缺失"])
        ax.set_ylim(1.5, -.5)
        ax.set_xlim(0, maximum * 1.23)
        for i, (value, record) in enumerate(zip(medians, records)):
            ax.text(record["max"] / divisor + maximum * .025, i, f"{value:,.{digits}f}",
                    va="center", color=INK, fontsize=10)
        if column == 0:
            ax.xaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:,.0f}"))
    footer(fig, data)
    save(fig, folder, "storage")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "benchmarks/results/representative/framework-comparison-representative.json")
    parser.add_argument("--output", type=Path, default=ROOT / "docs/assets/performance")
    args = parser.parse_args()
    render(json.loads(args.input.read_text(encoding="utf-8")), args.output)
