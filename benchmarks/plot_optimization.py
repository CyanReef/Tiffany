"""Render complete-path optimization results without rerunning benchmarks."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

from .plot import chart_font

ROOT = Path(__file__).resolve().parents[1]
BACKGROUND, INK, MUTED = "#F7FAFC", "#163247", "#52677A"
COLORS = {"before": "#7B8CA2", "after": "#168795"}
VERSIONS = (("before", "优化前"), ("after", "指标 + 调度优化"))


def render(data, output):
    rows = {(row["version"], row["case"]): row for row in data["summary"]}
    plt.rcParams.update({"font.family": chart_font("zh-CN"), "font.size": 10,
                         "svg.hashsalt": "tiffany-optimization"})
    fig, axes = plt.subplots(2, 2, figsize=(14, 9.5), facecolor=BACKGROUND)
    fig.subplots_adjust(left=.13, right=.96, top=.80, bottom=.19, hspace=.57, wspace=.34)
    fig.text(.06, .945, "Tiffany：真实指标与独立事件 Task 保留后的性能", fontsize=22,
             weight="bold", color=INK)
    method = data["method"]
    fig.text(.06, .90,
             f"完整 Bot.emit()；{method['repeats']} 轮交替子进程，每场景 {method['events_per_case']:,} 个事件，预热 {method['warmup_per_case']} 个",
             color=MUTED, fontsize=11)

    def bars(ax, cases, labels, key, title, unit, divisor=1, precision=0):
        ax.set_facecolor(BACKGROUND)
        for spine in ax.spines.values():
            spine.set_visible(False)
        ax.tick_params(length=0, colors=MUTED)
        ax.set_axisbelow(True)
        ax.grid(axis="x", color="#DDE6EC")
        largest = 0
        for version, legend in VERSIONS:
            offset = -.17 if version == "before" else .17
            selected = [rows[(version, case)][key] for case in cases]
            values = [row["median"] / divisor for row in selected]
            positions = [index + offset for index in range(len(cases))]
            errors = ([max(0, row["median"] - row["min"]) / divisor for row in selected],
                      [max(0, row["max"] - row["median"]) / divisor for row in selected])
            ax.barh(positions, values, height=.29, xerr=errors, color=COLORS[version],
                    error_kw={"capsize": 2, "ecolor": INK}, label=legend)
            largest = max(largest, *(row["max"] / divisor for row in selected))
            for position, value, row in zip(positions, values, selected):
                ax.annotate(f"{value:,.{precision}f}", (row["max"] / divisor, position),
                            xytext=(6, 0), textcoords="offset points", va="center", color=INK, fontsize=9)
        ax.set_yticks(range(len(cases)), labels)
        ax.invert_yaxis()
        ax.set_xlim(0, largest * 1.27)
        ax.xaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:,.0f}"))
        ax.set_title(title, loc="left", color=INK, weight="bold", pad=14)
        ax.set_xlabel(unit, color=MUTED)

    cpu_cases = ("one_handler", "ten_handlers", "unmatched_1000")
    cpu_labels = ("1 个 Hook", "10 个 Hook", "1 + 1,000 无关")
    bars(axes[0, 0], cpu_cases, cpu_labels, "events_per_second", "串行轻量业务：吞吐量", "事件 / 秒，越高越好")
    bars(axes[0, 1], cpu_cases, cpu_labels, "latency_p95_us", "串行轻量业务：各轮 P95 中位数", "微秒，越低越好", precision=1)
    bars(axes[1, 0], ("io_4", "io_16"), ("默认 4 并发", "显式 16 并发"),
         "events_per_second", "16 会话 / 每次 asyncio.sleep(5 ms)", "事件 / 秒，越高越好")
    bars(axes[1, 1], ("one_handler",), ("预热后组件",), "rss_before_bytes",
         "工作集：解释器与被测组件", "MiB，非完整应用内存", divisor=1024 ** 2, precision=1)
    handles, labels = axes[0, 0].get_legend_handles_labels()
    fig.legend(handles, labels, frameon=False, loc="upper left", bbox_to_anchor=(.055, .865), ncol=2)
    fig.text(.06, .115, "柱值为 7 轮中位数；误差线为最小–最大，不是置信区间。P95 每轮单独计算后取中位数。", color=MUTED)
    fig.text(.06, .075, "Windows / CPython 3.14.4；真实解析与分发，处理次数和指标核验通过。无网络回复与 LLM。", color=MUTED, fontsize=9)
    fig.text(.06, .035, "I/O 受系统定时器影响；闭环负载结果不代表 QQ 平台或部署服务器的容量。", color=MUTED, fontsize=9)
    output.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("png", "svg"):
        path = output.with_suffix("." + suffix)
        fig.savefig(path, dpi=180, facecolor=BACKGROUND)
        print(f"Saved {path}")
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / ".build-cache/benchmarks/optimization/tiffany-optimization.json")
    parser.add_argument("--output", type=Path, default=ROOT / ".build-cache/benchmarks/optimization/tiffany-optimization")
    args = parser.parse_args()
    render(json.loads(args.input.read_text(encoding="utf-8")), args.output)


if __name__ == "__main__":
    main()
