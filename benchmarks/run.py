"""Measure full event dispatch and isolated field resolution with stdlib tools."""

from __future__ import annotations

import argparse
import asyncio
import gc
import hashlib
import importlib.metadata
import json
import os
import platform
import statistics
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

from core import Bot, Context, Envelope, Field, ProviderContext


ROOT = Path(__file__).resolve().parents[1]
TEXT = Field[str]("benchmark.text")
ROUTES = (
    ("matching_1", 1, 0),
    ("matching_10", 10, 0),
    ("matching_100", 100, 0),
    ("unmatched_0", 1, 0),
    ("unmatched_100", 1, 100),
    ("unmatched_1000", 1, 1000),
)


async def measure_dispatch(matching: int, unmatched: int, events: int, warmup: int):
    bot = Bot()
    handled = 0

    async def handle(ctx):
        nonlocal handled
        handled += 1

    for index in range(matching):
        bot.register_hook(handle, name=f"message_{index}", on="message")
    for index in range(unmatched):
        bot.register_hook(handle, name=f"notice_{index}", on="notice")

    async with bot:
        for _ in range(warmup):
            await bot.emit(Envelope("benchmark", {"text": "hello"}, kind="message"))
        gc.collect()
        started = perf_counter()
        for _ in range(events):
            await bot.emit(Envelope("benchmark", {"text": "hello"}, kind="message"))
        elapsed = perf_counter() - started
    if handled != matching * (warmup + events):
        raise RuntimeError("dispatch did not execute the expected hooks")
    return elapsed


def measure_fields(read: str, contexts: int, warmup: int):
    bot = Bot()
    provider_calls = 0

    def read_text(ctx: ProviderContext) -> str:
        nonlocal provider_calls
        provider_calls += 1
        return ctx.raw["text"].strip()

    bot.provide(TEXT, read_text)
    envelope = Envelope("benchmark", {"text": "  hello  "})
    for _ in range(warmup):
        ctx = Context(envelope, bot.providers)
        ctx.resolve(TEXT)
        if read == "cached":
            ctx.resolve(TEXT)

    # Both cases traverse distinct Context objects; allocation is outside timing.
    views = [Context(envelope, bot.providers) for _ in range(contexts)]
    if read == "cached":
        for ctx in views:
            ctx.resolve(TEXT)
    calls_before = provider_calls
    gc.collect()
    started = perf_counter()
    for ctx in views:
        ctx.resolve(TEXT)
    elapsed = perf_counter() - started
    calls = provider_calls - calls_before
    expected = contexts if read == "first" else 0
    if calls != expected or any(ctx.resolve(TEXT) != "hello" for ctx in views):
        raise RuntimeError("field resolution did not preserve cache semantics")
    return elapsed, calls


def git_output(*args: str) -> str | None:
    try:
        return subprocess.check_output(
            ["git", *args], cwd=ROOT, text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def environment():
    cpu = platform.processor() or "unknown"
    if os.name == "nt":
        import winreg

        try:
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
            ) as key:
                cpu = winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
        except OSError:
            pass
    # Record the measured files even when the working tree has uncommitted work.
    sources = sorted((ROOT / "core").rglob("*.py")) + [Path(__file__).resolve()]
    digest = hashlib.sha256()
    for source in sources:
        digest.update(source.relative_to(ROOT).as_posix().encode())
        digest.update(source.read_bytes())
    status = git_output("status", "--porcelain")
    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "python_build": platform.python_build(),
        "os": platform.platform(),
        "cpu": cpu,
        "logical_cpus": os.cpu_count(),
        "websockets": importlib.metadata.version("websockets"),
        "event_loop": type(asyncio.get_running_loop()).__name__,
        "asyncio_debug": asyncio.get_running_loop().get_debug(),
        "gc_enabled": gc.isenabled(),
        "git_head": git_output("rev-parse", "HEAD"),
        "working_tree_dirty": status is not None and bool(status),
        "source_sha256": digest.hexdigest(),
    }


def summarize(elapsed: list[float], operations: int):
    rates = [operations / seconds for seconds in elapsed]
    return {
        "operations_per_sample": operations,
        "elapsed_seconds": elapsed,
        "median_ops_per_second": statistics.median(rates),
        "min_ops_per_second": min(rates),
        "max_ops_per_second": max(rates),
        "median_ns_per_op": statistics.median(elapsed) / operations * 1e9,
    }


async def run(args):
    result = {
        "schema_version": 1,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "environment": environment(),
        "method": {
            "repeats": args.repeats,
            "events_per_sample": args.events,
            "warmup_per_case": args.warmup,
            "field_contexts_per_sample": args.contexts,
            "trace_enabled": False,
            "metrics_enabled": True,
            "dispatch": "sequential Bot.emit; Envelope allocation through hook completion",
            "fields": "one resolve per preallocated Context; text strip provider",
            "case_order": "rotate dispatch cases; alternate first/cached field order",
            "timing": "perf_counter; setup, warmup and shutdown excluded; GC enabled",
        },
        "dispatch": [],
        "fields": [],
    }
    dispatch_samples = {name: [] for name, _, _ in ROUTES}
    field_samples = {name: [] for name in ("first", "cached")}
    field_calls = {name: [] for name in field_samples}
    for repeat in range(args.repeats):
        offset = repeat % len(ROUTES)
        for name, matching, unmatched in ROUTES[offset:] + ROUTES[:offset]:
            dispatch_samples[name].append(
                await measure_dispatch(matching, unmatched, args.events, args.warmup)
            )
        order = ("first", "cached") if repeat % 2 == 0 else ("cached", "first")
        for name in order:
            elapsed, calls = measure_fields(name, args.contexts, args.warmup)
            field_samples[name].append(elapsed)
            field_calls[name].append(calls)
        print(f"Completed round {repeat + 1}/{args.repeats}", flush=True)
    for name, matching, unmatched in ROUTES:
        result["dispatch"].append({
            "name": name, "matching_hooks": matching, "unmatched_hooks": unmatched,
            **summarize(dispatch_samples[name], args.events),
        })
    for name in field_samples:
        result["fields"].append({
            "name": name, "provider_calls_per_sample": field_calls[name],
            **summarize(field_samples[name], args.contexts),
        })
    result["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Saved {args.output}")
    for case in result["dispatch"]:
        print(f"{case['name']:>16}: {case['median_ops_per_second']:,.0f} events/s")
    for case in result["fields"]:
        print(f"{case['name']:>16}: {case['median_ns_per_op']:,.0f} ns/read")


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=positive_int, default=3000)
    parser.add_argument("--contexts", type=positive_int, default=50000)
    parser.add_argument("--warmup", type=positive_int, default=200)
    parser.add_argument("--repeats", type=positive_int, default=7)
    parser.add_argument("--output", type=Path, default=ROOT / ".build-cache/benchmarks/local.json")
    asyncio.run(run(parser.parse_args()), debug=False)


if __name__ == "__main__":
    main()
