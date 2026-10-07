"""Render framework comparison results without rerunning any measurements."""
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
LABELS = {"tiffany": "Tiffany", "nonebot": "NoneBot 2.5.0", "astrbot": "AstrBot 4.28.2"}
COLORS = {"tiffany": "#167D8D", "nonebot": "#5376B8", "astrbot": "#BA733C"}
ORDER = ("tiffany", "astrbot", "nonebot")
BACKGROUND, INK, MUTED = "#F7FAFC", "#163247", "#52677A"


def render(data, concurrency, output):
    plt.rcParams.update({"font.family": chart_font("zh-CN"), "font.size": 10,
                         "svg.hashsalt": "tiffany-comparison"})
    rows = {(row["framework"], row["case"]): row for row in data["summary"]}
    if concurrency["method"]["event_concurrency"] != 16:
        raise ValueError("the I/O chart requires a matched concurrency of 16")
    if concurrency["tiffany_source_sha256"] != data["environment"]["tiffany_source_sha256"]:
        raise ValueError("the measurements use different Tiffany sources")
    for row in concurrency["summary"]:
        rows[(row["framework"], row["case"])] = row
    fig, axes = plt.subplots(2, 2, figsize=(13, 9.5), facecolor=BACKGROUND)
    fig.subplots_adjust(left=.10, right=.96, top=.82, bottom=.21, hspace=.55, wspace=.32)
    fig.text(.075, .94, "Tiffany / AstrBot / NoneBot：离线消息路径实测", fontsize=21,
             weight="bold", color=INK)
    fig.text(.075, .895, "相同私聊文本、真实解析与分发、校验处理次数；无网络回复与 LLM", color=MUTED)
    for ax in axes.flat:
        ax.set_facecolor(BACKGROUND)
        for spine in ax.spines.values():
            spine.set_visible(False)
        ax.tick_params(length=0, colors=MUTED)
        ax.set_axisbelow(True)
        ax.grid(axis="x", color="#DDE6EC")

    for ax, case, title in (
        (axes[0, 0], "one_handler", "单处理器吞吐量"),
        (axes[0, 1], "io_16_sessions", "16 会话 / 16 并发、每次等待 5 ms"),
    ):
        selected = [rows[(framework, case)]["events_per_second"] for framework in ORDER]
        values = [row["median"] for row in selected]
        errors = ([row["median"] - row["min"] for row in selected],
                  [row["max"] - row["median"] for row in selected])
        ax.barh([LABELS[f] for f in ORDER], values, xerr=errors,
                color=[COLORS[f] for f in ORDER], height=.52,
                error_kw={"capsize": 3, "ecolor": INK})
        ax.invert_yaxis()
        ax.set_title(title, loc="left", color=INK, weight="bold", pad=16)
        ax.set_xlabel("事件 / 秒，越高越好", color=MUTED)
        ax.set_xlim(0, max(row["max"] for row in selected) * 1.30)
        ax.xaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:,.0f}"))
        for index, (value, row) in enumerate(zip(values, selected)):
            ax.text(row["max"] + ax.get_xlim()[1] * .015, index, f"{value:,.0f}",
                    va="center", color=INK)

    ax = axes[1, 0]
    names = ("one_handler", "unmatched_100", "unmatched_1000")
    for framework in ORDER:
        ax.plot(range(3), [rows[(framework, case)]["events_per_second"]["median"] for case in names],
                marker="o", linewidth=2, color=COLORS[framework], label=LABELS[framework])
    ax.set_xticks(range(3), ["0", "100", "1,000"])
    ax.set_yscale("log")
    ax.set_title("额外无关处理器：吞吐量随数量变化", loc="left", color=INK, weight="bold", pad=16)
    ax.set_xlabel("额外处理器数量；各框架使用原生事件分类", color=MUTED)
    ax.set_ylabel("事件 / 秒（对数刻度）", color=MUTED)
    ax.grid(axis="y", color="#DDE6EC")
    ax.legend(frameon=False, fontsize=9)

    ax = axes[1, 1]
    values = [rows[(framework, "one_handler")]["latency_p95_us"]["median"] for framework in ORDER]
    ax.barh([LABELS[f] for f in ORDER], values, color=[COLORS[f] for f in ORDER], height=.52)
    ax.invert_yaxis()
    ax.set_xlim(0, max(values) * 1.32)
    ax.set_title("单处理器：各轮 P95 延迟的中位数", loc="left", color=INK, weight="bold", pad=16)
    ax.set_xlabel("微秒，越低越好", color=MUTED)
    for index, value in enumerate(values):
        ax.text(value + max(values) * .025, index, f"{value:,.0f} µs", va="center", color=INK)

    method = data["method"]
    fig.text(.075, .135,
             f"{method['repeats']} 轮中位数；吞吐误差线为最小–最大。每轮每场景 {method['events_per_case']:,} 个事件。",
             color=MUTED, fontsize=10)
    fig.text(.075, .095,
             "AstrBot 使用会话配置写入缓存；SQLite 默认读取另列于报告。I/O 使用补测数据；无关处理器分类不同。",
             color=MUTED, fontsize=9)
    fig.text(.075, .055,
             f"Windows / CPython 3.14.4 / {data['started_at_utc'][:10]} UTC；受控闭环负载，不代表平台或完整应用容量。",
             color=MUTED, fontsize=9)
    output.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("png", "svg"):
        destination = output.with_suffix("." + suffix)
        fig.savefig(destination, dpi=180, facecolor=BACKGROUND)
        print(f"Saved {destination}")
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / ".build-cache/benchmarks/comparison/framework-comparison.json")
    parser.add_argument("--concurrency-input", type=Path, default=ROOT / ".build-cache/benchmarks/comparison/framework-concurrency.json")
    parser.add_argument("--output", type=Path, default=ROOT / ".build-cache/benchmarks/comparison/framework-comparison")
    args = parser.parse_args()
    render(json.loads(args.input.read_text(encoding="utf-8")),
           json.loads(args.concurrency_input.read_text(encoding="utf-8")), args.output)


if __name__ == "__main__":
    main()
