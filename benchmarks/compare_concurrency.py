"""Repeat the I/O workload with an explicit event concurrency of 16 in all engines."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from benchmarks.compare import (
    AstrBotEngine, CASES, FRAMEWORKS, NoneBotEngine, TiffanyEngine,
    measure, positive_integer, source_fingerprint,
)


class TiffanyLimit16(TiffanyEngine):
    async def setup(self, case, consumer):
        await super().setup(case, consumer)
        self.bot.runtime.scheduler.global_limit = 16
        self.bot.runtime.scheduler.configure_adapter("benchmark", active_limit=16)
        if self.bot.runtime.scheduler.global_limit != 16:
            raise AssertionError("unexpected global concurrency limit")


async def worker(args):
    import psutil
    engines = {"tiffany": TiffanyLimit16, "nonebot": NoneBotEngine, "astrbot": AstrBotEngine}
    case = dict(CASES[-1], event_concurrency=16)
    result = await measure(engines[args.framework], case, args, psutil.Process())
    if args.framework == "astrbot":
        from astrbot.core import sp, db_helper
        await sp.close()
        await db_helper.engine.dispose()
    return {"framework": args.framework, "cases": [result],
            "event_loop": type(asyncio.get_running_loop()).__name__}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=positive_integer, default=1000)
    parser.add_argument("--warmup", type=positive_integer, default=100)
    parser.add_argument("--repeats", type=positive_integer, default=5)
    parser.add_argument("--output", type=Path, default=ROOT / "benchmarks/results/archive/comparison/framework-concurrency.json")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--framework", choices=FRAMEWORKS, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(asyncio.run(worker(args)), ensure_ascii=False))
        return
    result = {"schema_version": 1, "started_at_utc": datetime.now(timezone.utc).isoformat(),
              "method": {"repeats": args.repeats, "events_per_case": args.events,
                         "warmup_per_case": args.warmup, "event_concurrency": 16,
                         "producer_sessions": 16, "handler_sleep_seconds": .005,
                         "gc_enabled": True, "network": False, "llm": False},
              "tiffany_source_sha256": source_fingerprint(),
              "supplement_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "samples": [], "summary": []}
    for repeat in range(args.repeats):
        offset = repeat % len(FRAMEWORKS)
        for framework in FRAMEWORKS[offset:] + FRAMEWORKS[:offset]:
            with tempfile.TemporaryDirectory(prefix="Tiffany matched concurrency ") as temporary:
                env = dict(os.environ, ASTRBOT_ROOT=temporary, ASTRBOT_DISABLE_METRICS="1",
                           PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
                child = subprocess.run(
                    [sys.executable, "-B", str(Path(__file__).resolve()), "--worker",
                     "--framework", framework, "--events", str(args.events), "--warmup", str(args.warmup)],
                    cwd=temporary, env=env, capture_output=True, encoding="utf-8", timeout=180,
                )
                if child.returncode:
                    raise RuntimeError(f"{framework}: {child.stdout[-2000:]}\n{child.stderr[-6000:]}")
                sample = json.loads(child.stdout.strip().splitlines()[-1])
                sample["round"] = repeat
                result["samples"].append(sample)
                print(f"Matched concurrency round {repeat + 1}/{args.repeats}: {framework} verified", flush=True)
    for framework in FRAMEWORKS:
        cases = [sample["cases"][0] for sample in result["samples"] if sample["framework"] == framework]
        row = {"framework": framework, "case": "io_16_sessions", "samples": len(cases)}
        for key in ("events_per_second", "latency_p50_us", "latency_p95_us", "latency_p99_us",
                    "rss_before_bytes", "cpu_seconds"):
            values = [case[key] for case in cases]
            row[key] = {"median": statistics.median(values), "min": min(values), "max": max(values)}
        result["summary"].append(row)
    result["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Saved {args.output}")


if __name__ == "__main__":
    main()
