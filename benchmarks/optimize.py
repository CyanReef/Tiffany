"""Compare complete Tiffany paths against a saved source snapshot, with real metrics."""
from __future__ import annotations

import argparse
import asyncio
import cProfile
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import logging
import os
from pathlib import Path
import platform
import pstats
import shutil
import statistics
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
CASES = (
    {"name": "one_handler", "matching": 1, "unmatched": 0},
    {"name": "ten_handlers", "matching": 10, "unmatched": 0},
    {"name": "unmatched_1000", "matching": 1, "unmatched": 1000},
    {"name": "onebot_admission", "matching": 1, "unmatched": 0, "onebot_admission": True},
    {"name": "io_4", "matching": 1, "unmatched": 0, "sessions": 16, "delay": .005, "limit": 4},
    {"name": "io_16", "matching": 1, "unmatched": 0, "sessions": 16, "delay": .005, "limit": 16},
)


def snapshot(destination):
    destination = destination.resolve()
    if destination.exists():
        raise ValueError("snapshot exists; refusing to overwrite")
    if destination == ROOT or ROOT.is_relative_to(destination):
        raise ValueError("snapshot cannot contain the source repository")
    files = list(ROOT.glob("*.py"))
    for directory in ("core", "adapters", "clients", "hooks", "shared", "deployment", "benchmarks"):
        files.extend((ROOT / directory).rglob("*.py"))
    manifest = {}
    for source in files:
        relative = source.relative_to(ROOT)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        manifest[relative.as_posix()] = hashlib.sha256(source.read_bytes()).hexdigest()
    (destination / "baseline-manifest.json").write_text(json.dumps({"files": manifest}, indent=2) + "\n", encoding="utf-8")
    print(f"Saved {destination} ({len(manifest)} source files)")


async def worker(args):
    sys.path.insert(0, str(args.source_root.resolve()))
    from benchmarks.compare import Consumer, TiffanyEngine, measure, raw_event, source_fingerprint
    import psutil
    logging.basicConfig(level=logging.ERROR)

    class CheckedEngine(TiffanyEngine):
        async def setup(self, case, consumer):
            self.case = case
            await super().setup(case, consumer)
            self.bot.runtime.scheduler.configure_adapter("benchmark", case.get("limit", 4))
            self.adapter = None
            if case.get("onebot_admission"):
                from adapters.OneBotWebSocketAdapter import OneBotWebSocketAdapter

                self.adapter = OneBotWebSocketAdapter(
                    "127.0.0.1", 0, "onebot", adapter_id="benchmark",
                )
                await self.adapter.setup(self.bot.runtime)

        async def emit(self, index, session):
            if self.adapter is None:
                return await super().emit(index, session)
            raw = raw_event(index, session)
            envelope = self.Envelope(
                "onebot", raw, kind=self.detect(raw), adapter_id="benchmark",
                connection_id="offline", session_id=self.adapter._session_id(raw),
            )
            if hasattr(self.adapter, "_submit_event") and hasattr(self.bot.runtime, "submit"):
                admitted = self.adapter._submit_event(envelope)
            else:
                admitted = await self.adapter._emit(envelope)
            if admitted is None:
                raise AssertionError("OneBot event was rejected")
            await admitted

        async def close(self):
            metrics = self.bot.runtime.metrics.snapshot()
            expected = args.events + args.warmup
            labels = {"platform": "onebot", "adapter": "benchmark"}
            assert metrics.get("events_received_total", labels) == expected
            assert metrics.get("event_duration_seconds_count", labels) == expected
            for index in range(self.case["matching"]):
                assert metrics.get("hook_executions_total", {"platform": "onebot", "hook": f"matching_{index}", "reason": "completed"}) == expected
            assert metrics.get("event_queue_depth") == metrics.get("events_active") == 0
            if self.adapter is not None:
                await self.adapter.teardown()
            await super().close()
            assert not self.bot.runtime._owner_events

    cases = [dict(case) for case in CASES if case["name"] in args.cases]
    remaining = cases[1:]
    if remaining:
        offset = args.round % len(remaining)
        cases = [cases[0], *(remaining[offset:] + remaining[:offset])]
    result = {"source_sha256": source_fingerprint(), "cases": [],
              "event_loop": type(asyncio.get_running_loop()).__name__}
    for case in cases:
        row = await measure(CheckedEngine, case, args, psutil.Process())
        row["microseconds_per_event"] = row["elapsed_seconds"] * 1e6 / args.events
        row["verified_real_metrics"] = True
        result["cases"].append(row)
    if args.profile_output:
        # Separate sampling, with its own warmup; profiler never affects the
        # throughput/latency samples above.
        consumer, engine = Consumer(0), TiffanyEngine()
        await engine.setup({"matching": 1, "unmatched": 0}, consumer)
        for index in range(args.warmup):
            await engine.emit(index, 0)
        profiler = cProfile.Profile()
        profiler.enable()
        for index in range(3000):
            await engine.emit(index, 0)
        profiler.disable()
        await engine.close()
        args.profile_output.parent.mkdir(parents=True, exist_ok=True)
        profiler.dump_stats(str(args.profile_output))
        stats = pstats.Stats(profiler)
        rows = []
        for (filename, line, name), (_, calls, own, cumulative, _) in stats.stats.items():
            path = Path(filename)
            if path.is_relative_to(args.source_root) and "core" in path.parts:
                rows.append({"file": path.relative_to(args.source_root).as_posix(), "line": line,
                             "function": name, "calls": calls, "own_seconds": own,
                             "cumulative_seconds": cumulative})
        result["profile"] = {"events": 3000, "rows": sorted(rows, key=lambda row: row["own_seconds"], reverse=True)}
    return result


def run(args):
    baseline = args.baseline.resolve()
    manifest = json.loads((baseline / "baseline-manifest.json").read_text(encoding="utf-8"))
    for name, expected in manifest["files"].items():
        if hashlib.sha256((baseline / name).read_bytes()).hexdigest() != expected:
            raise ValueError(f"baseline was modified: {name}")
    result = {"started_at_utc": datetime.now(timezone.utc).isoformat(),
              "environment": {"python": sys.version, "os": platform.platform(),
                              "logical_cpus": os.cpu_count()},
              "packages": {item.metadata["Name"]: item.version for item in importlib.metadata.distributions()},
              "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "method": {"repeats": args.repeats, "events_per_case": args.events,
                         "warmup_per_case": args.warmup, "metrics": "real, immediate",
                         "gc_enabled": True, "network": False, "case_order": "first baseline, others rotate",
                         "version_order": "alternating", "processes": "fresh per version per round"},
              "samples": [], "summary": []}
    versions = (("before", baseline), ("after", args.candidate.resolve()))
    for repeat in range(args.repeats):
        for version, source in (versions if repeat % 2 == 0 else versions[::-1]):
            with tempfile.TemporaryDirectory(prefix="Tiffany optimization ") as temporary:
                command = [sys.executable, "-B", str(Path(__file__).resolve()), "--worker",
                           "--source-root", str(source), "--events", str(args.events), "--warmup", str(args.warmup),
                           "--round", str(repeat), "--cases", *args.cases]
                if repeat == args.repeats - 1:
                    command += ["--profile-output", str(ROOT / f".build-cache/optimization/profiles/{args.output.stem}-{version}.prof")]
                child = subprocess.run(command, cwd=temporary, capture_output=True, encoding="utf-8", timeout=300,
                                       env=dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1"))
            if child.returncode:
                raise RuntimeError(f"{version}: {child.stdout[-2000:]}\n{child.stderr[-6000:]}")
            sample = json.loads(child.stdout.strip().splitlines()[-1])
            sample.update(version=version, round=repeat)
            result["samples"].append(sample)
            print(f"Round {repeat + 1}/{args.repeats}: {version} verified", flush=True)
    for version, _ in versions:
        for name in args.cases:
            cases = [case for sample in result["samples"] if sample["version"] == version
                     for case in sample["cases"] if case["name"] == name]
            row = {"version": version, "case": name, "samples": len(cases)}
            for key in ("events_per_second", "microseconds_per_event", "latency_p50_us", "latency_p95_us",
                        "latency_p99_us", "rss_before_bytes", "cpu_seconds"):
                values = [case[key] for case in cases]
                row[key] = dict(median=statistics.median(values), min=min(values), max=max(values))
            result["summary"].append(row)
    result["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Saved {args.output}")


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--baseline", type=Path, default=ROOT / ".build-cache/optimization/before")
    parser.add_argument("--candidate", type=Path, default=ROOT)
    parser.add_argument("--events", type=positive, default=5000)
    parser.add_argument("--warmup", type=positive, default=200)
    parser.add_argument("--repeats", type=positive, default=7)
    parser.add_argument("--cases", nargs="+", choices=[case["name"] for case in CASES], default=[case["name"] for case in CASES])
    parser.add_argument("--output", type=Path, default=ROOT / ".build-cache/benchmarks/optimization/tiffany-optimization.json")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--source-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--round", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--profile-output", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.snapshot:
        snapshot(args.snapshot)
    elif args.worker:
        print(json.dumps(asyncio.run(worker(args))))
    else:
        run(args)


if __name__ == "__main__":
    main()
