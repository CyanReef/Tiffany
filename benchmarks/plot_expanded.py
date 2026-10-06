"""Plot the six-framework dataset without changing or rerunning measurements."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

from benchmarks.plot import chart_font

ROOT = Path(__file__).resolve().parents[1]
ORDER = ("tiffany", "entari", "astrbot", "nonebot", "koishi", "graia")
LABELS = {"tiffany": "Tiffany", "astrbot": "AstrBot 4.28.2", "nonebot": "NoneBot 2.5.0",
          "koishi": "Koishi 4.18.11", "graia": "Ariadne 0.11.7", "entari": "Entari 0.18.6"}
COLORS = {"tiffany": "#147E87", "entari": "#7462A5", "astrbot": "#B67B3A", "nonebot": "#4F79B3",
          "koishi": "#418761", "graia": "#BB5870"}
BACKGROUND, INK, MUTED = "#F7FAFC", "#163247", "#52677A"


def render(data, output):
    if len(data["samples"]) != data["method"]["repeats"] * 6 or not data.get("finished_at_utc"):
        raise ValueError("the six-framework dataset is incomplete")
    rows = {(row["framework"], row["case"]): row for row in data["summary"]}
    plt.rcParams.update({"font.family": chart_font("zh-CN"), "font.size": 10,
                         "svg.hashsalt": "tiffany-expanded-comparison"})
    fig, axes = plt.subplots(3, 2, figsize=(15, 14), facecolor=BACKGROUND)
    fig.subplots_adjust(left=.14, right=.96, top=.87, bottom=.16, hspace=.58, wspace=.48)
    fig.text(.075, .958, "六个机器人框架：真实离线消息路径", fontsize=24, weight="bold", color=INK)
    fig.text(.075, .919, "相同文本与处理次数 / 原生解析与调度 / I/O 共用本机 HTTP 延迟服务", color=MUTED, fontsize=12)
    for ax in axes.flat:
        ax.set_facecolor(BACKGROUND)
        for spine in ax.spines.values():
            spine.set_visible(False)
        ax.tick_params(length=0, colors=MUTED)
        ax.set_axisbelow(True)
        ax.grid(axis="x", color="#DDE6EC")

    def bars(ax, case, metric, title, unit, divisor=1, errors=True):
        selected = [rows[(framework, case)][metric] for framework in ORDER]
        values = [row["median"] / divisor for row in selected]
        ranges = ([value - row["min"] / divisor for value, row in zip(values, selected)],
                  [row["max"] / divisor - value for value, row in zip(values, selected)])
        ax.barh([LABELS[f] for f in ORDER], values, xerr=ranges if errors else None,
                color=[COLORS[f] for f in ORDER], height=.56,
                error_kw={"capsize": 2.5, "ecolor": INK, "elinewidth": .9})
        ax.invert_yaxis()
        limit = max(row["max"] / divisor for row in selected) * 1.33
        ax.set_xlim(0, limit)
        ax.set_title(title, loc="left", color=INK, weight="bold", pad=13)
        ax.set_xlabel(unit, color=MUTED)
        ax.xaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:,.0f}"))
        for index, (value, row) in enumerate(zip(values, selected)):
            ax.text(row["max"] / divisor + limit * .018, index,
                    f"{value:,.1f}" if divisor > 1 else f"{value:,.0f}", va="center", color=INK, fontsize=9)

    bars(axes[0, 0], "one_handler", "events_per_second", "一个处理器", "事件 / 秒，越高越好")
    bars(axes[0, 1], "ten_handlers", "events_per_second", "十个处理器均执行", "事件 / 秒，越高越好")
    ax = axes[1, 0]
    names = ("one_handler", "unmatched_100", "unmatched_1000")
    for framework in ORDER:
        ax.plot(range(3), [rows[(framework, case)]["events_per_second"]["median"] for case in names],
                marker="o", linewidth=1.8, color=COLORS[framework], label=LABELS[framework])
    ax.set_xticks(range(3), ["0", "100", "1,000"])
    ax.set_yscale("log")
    ax.set_title("无关处理器增加时的吞吐量", loc="left", color=INK, weight="bold", pad=13)
    ax.set_xlabel("额外处理器数量；使用各自原生事件分类", color=MUTED)
    ax.set_ylabel("事件 / 秒（对数刻度）", color=MUTED)
    ax.grid(axis="y", color="#DDE6EC")
    ax.legend(frameon=False, fontsize=8, ncol=2, loc="lower left")
    bars(axes[1, 1], "io_http_16_sessions", "events_per_second", "16 会话 / 每条请求服务至少等待 5 ms",
         "事件 / 秒，越高越好")
    bars(axes[2, 0], "one_handler", "latency_p95_us", "单处理器 P95 延迟", "微秒，越低越好")
    bars(axes[2, 1], "one_handler", "rss_before_bytes", "预热后组件进程工作集", "MiB，越低越好",
         divisor=1024 ** 2)
    method = data["method"]
    fig.text(.075, .108,
             f"{method['repeats']} 轮中位数；每轮每场景 {method['events_per_case']:,} 个事件、预热 {method['warmup_per_case']} 个。误差线为最小–最大。",
             color=MUTED, fontsize=10)
    fig.text(.075, .078,
             "Windows / Python 3.12.14；Koishi 为 Node.js 24.3.0。AstrBot 使用配置写入缓存；SQLite 默认路径另列于报告。",
             color=MUTED, fontsize=10)
    fig.text(.075, .048,
             "协议、语言与内置职责存在差异；仅比较离线组件路径，不代表完整应用、平台回复或 LLM 性能。",
             color=MUTED, fontsize=10)
    output.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("png", "svg"):
        destination = output.with_suffix("." + suffix)
        fig.savefig(destination, dpi=170, facecolor=BACKGROUND)
        print(f"Saved {destination}")
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "benchmarks/results/archive/comparison/framework-comparison-expanded.json")
    parser.add_argument("--output", type=Path, default=ROOT / "docs/assets/performance/framework-comparison-expanded")
    args = parser.parse_args()
    render(json.loads(args.input.read_text(encoding="utf-8")), args.output)


if __name__ == "__main__":
    main()
