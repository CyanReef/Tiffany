"""Render benchmark JSON as PNG and SVG; requires the benchmark extra."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.ticker import FuncFormatter, MaxNLocator


ROOT = Path(__file__).resolve().parents[1]
INK = "#163247"
MUTED = "#52677A"
COLORS = ("#167D8D", "#3C9BA7", "#7BBEC4")


def draw_bars(ax, labels, values, lower, upper, *, units, formatter):
    errors = (
        [value - low for value, low in zip(values, lower)],
        [high - value for value, high in zip(values, upper)],
    )
    ax.barh(
        labels, values, height=0.52, color=COLORS[:len(values)],
        xerr=errors, error_kw={"ecolor": INK, "capsize": 4, "linewidth": 1.2},
    )
    ax.invert_yaxis()
    ax.set_xlim(0, max(upper) * 1.32)
    ax.set_xlabel(units, color=MUTED, labelpad=10)
    ax.xaxis.set_major_formatter(FuncFormatter(lambda x, _: formatter(x)))
    ax.xaxis.set_major_locator(MaxNLocator(nbins=4))
    ax.grid(axis="x", color="#DDE6EC", linewidth=0.8)
    ax.set_axisbelow(True)
    ax.tick_params(axis="both", length=0, colors=MUTED, labelsize=10)
    ax.tick_params(axis="y", pad=10)
    for spine in ax.spines.values():
        spine.set_visible(False)
    for index, (value, high) in enumerate(zip(values, upper)):
        ax.text(high + max(upper) * 0.035, index, formatter(value), va="center", color=INK, weight="bold")


def save(fig, output: Path, name: str):
    for extension in ("png", "svg"):
        path = output / f"{name}.{extension}"
        fig.savefig(path, dpi=180, facecolor=fig.get_facecolor())
        print(f"Saved {path}")
    plt.close(fig)


def chart_font(language: str) -> str:
    if language == "en":
        return "DejaVu Sans"
    for name in ("Microsoft YaHei", "Noto Sans CJK SC", "Source Han Sans SC", "PingFang SC", "WenQuanYi Micro Hei", "SimHei"):
        try:
            font_manager.findfont(name, fallback_to_default=False)
            return name
        except ValueError:
            continue
    raise SystemExit("Chinese font not found. Install Noto Sans CJK SC or use --language en.")


def render(data, output: Path, language: str):
    def label(english: str, chinese: str) -> str:
        return chinese if language == "zh-CN" else english

    output.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({"font.family": chart_font(language), "font.size": 10, "svg.hashsalt": "tiffany"})
    cases = {case["name"]: case for case in data["dispatch"]}
    method = data["method"]
    env = data["environment"]
    date = data["started_at_utc"][:10]
    footer = f"{env['cpu']}  |  Python {env['python']}  |  {date} UTC"
    sampling = label(
        f"Median of {method['repeats']} rounds; whiskers show min-max. {method['events_per_sample']:,} events/round.",
        f"{method['repeats']} 轮中位数；误差线：最小值到最大值。每轮 {method['events_per_sample']:,} 个事件。",
    )
    suffix = ".zh-CN" if language == "zh-CN" else ""

    fig, axes = plt.subplots(1, 2, figsize=(12, 5.4), facecolor="#F7FAFC")
    fig.subplots_adjust(left=0.09, right=0.96, top=0.70, bottom=0.33, wspace=0.45)
    fig.text(0.07, 0.92, label("Tiffany / Event dispatch", "Tiffany / 事件分发实测"), fontsize=23, weight="bold", color=INK)
    fig.text(0.07, 0.85, label(
        "Sequential Bot.emit: Envelope allocation → scheduler → hook completion",
        "串行 Bot.emit：创建 Envelope → 有界调度 → Hook 执行完成",
    ), color=MUTED, fontsize=11)
    for ax, title, names, labels in (
        (axes[0], label("Matching hooks per event", "每个事件执行的 Hook 数量"), ["matching_1", "matching_10", "matching_100"], [label("1 hook", "1 个"), label("10 hooks", "10 个"), label("100 hooks", "100 个")]),
        (axes[1], label("Unmatched hooks registered", "额外注册的无关 Hook 数量"), ["unmatched_0", "unmatched_100", "unmatched_1000"], [label("0 hooks", "0 个"), label("100 hooks", "100 个"), label("1,000 hooks", "1,000 个")]),
    ):
        selected = [cases[name] for name in names]
        ax.set_facecolor("#F7FAFC")
        ax.set_title(title, loc="left", color=INK, weight="bold", pad=22)
        draw_bars(
            ax, labels, [case["median_ops_per_second"] for case in selected],
            [case["min_ops_per_second"] for case in selected],
            [case["max_ops_per_second"] for case in selected],
            units=label("Events / second · higher is better", "事件 / 秒 · 越高越好"), formatter=lambda x: f"{x:,.0f}",
        )
    fig.text(0.07, 0.17, label("Right panel: 1 matching hook in every case; warmed route cache.", "右图各场景都执行 1 个 Hook；路由缓存已预热。"), color=MUTED)
    fig.text(0.07, 0.12, sampling + label(" Metrics on; trace off; no network I/O.", " 指标开启，Trace 关闭，无网络 I/O。"), color=MUTED, fontsize=9)
    fig.text(0.07, 0.06, footer, color=MUTED, fontsize=9)
    save(fig, output, "dispatch" + suffix)

    selected = data["fields"]
    fig, ax = plt.subplots(figsize=(10, 4.2), facecolor="#F7FAFC")
    fig.subplots_adjust(left=0.27, right=0.94, top=0.66, bottom=0.35)
    fig.text(0.07, 0.91, label("Tiffany / Lazy field reads", "Tiffany / 字段懒计算实测"), fontsize=23, weight="bold", color=INK)
    fig.text(0.07, 0.82, label("Context.resolve with a synchronous text.strip() provider", "Context.resolve，同步 text.strip() Provider"), color=MUTED, fontsize=11)
    ax.set_facecolor("#F7FAFC")
    draw_bars(
        ax, [label("First read\nprovider + cache write", "首次读取\n计算并写入缓存"), label("Cached read\ncache lookup", "缓存读取\n查询已缓存字段")],
        [case["median_ns_per_op"] / 1000 for case in selected],
        [1e6 / case["max_ops_per_second"] for case in selected],
        [1e6 / case["min_ops_per_second"] for case in selected],
        units=label("Microseconds / read · lower is better", "微秒 / 次 · 越低越好"), formatter=lambda x: f"{x:.2f}",
    )
    fig.text(0.07, 0.17, label(
        f"{method['field_contexts_per_sample']:,} preallocated contexts/round; {method['repeats']} rounds. Whiskers: min-max.",
        f"每轮预分配 {method['field_contexts_per_sample']:,} 个 Context，共 {method['repeats']} 轮；误差线：最小值到最大值。",
    ), color=MUTED, fontsize=9)
    fig.text(0.07, 0.12, label("Context construction excluded. Cached reads invoke the provider zero times.", "不计 Context 构造耗时；缓存读取期间 Provider 调用次数为 0。"), color=MUTED, fontsize=9)
    fig.text(0.07, 0.06, footer, color=MUTED, fontsize=9)
    save(fig, output, "fields" + suffix)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "benchmarks/results/local.json")
    parser.add_argument("--output", type=Path, default=ROOT / "docs/assets/performance")
    parser.add_argument("--language", choices=("all", "en", "zh-CN"), default="all")
    args = parser.parse_args()
    data = json.loads(args.input.read_text(encoding="utf-8"))
    languages = ("en", "zh-CN") if args.language == "all" else (args.language,)
    for language in languages:
        render(data, args.output, language)


if __name__ == "__main__":
    main()
