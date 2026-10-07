"""Six real offline message paths; isolated workers, shared HTTP I/O latency."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import gc
import hashlib
import importlib.metadata
import json
import logging
import os
from pathlib import Path
import platform
import queue
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
from time import get_clock_info, perf_counter, perf_counter_ns, process_time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from benchmarks.compare import AstrBotEngine, NoneBotEngine, percentile, positive_integer
from benchmarks.expanded_engines import EntariEngine, GraiaEngine, TiffanyEngine16

FRAMEWORKS = ("tiffany", "astrbot", "nonebot", "koishi", "graia", "entari")
CASES = (
    {"name": "one_handler", "matching": 1, "unmatched": 0},
    {"name": "ten_handlers", "matching": 10, "unmatched": 0},
    {"name": "unmatched_100", "matching": 1, "unmatched": 100},
    {"name": "unmatched_1000", "matching": 1, "unmatched": 1000},
    {"name": "io_http_16_sessions", "matching": 1, "unmatched": 0, "sessions": 16},
)
ENGINES = {"tiffany": TiffanyEngine16, "astrbot": AstrBotEngine,
           "nonebot": NoneBotEngine, "graia": GraiaEngine, "entari": EntariEngine}
HELPERS = ("benchmarks/compare.py", "benchmarks/compare_expanded.py",
           "benchmarks/expanded_engines.py", "benchmarks/koishi_worker.cjs")


def tiffany_sources(root):
    return sorted((root / "core").rglob("*.py")) + [root / "adapters/onebot_fields.py", root / "fields.py"]


def fingerprint(paths, root):
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def case_order(round_number):
    remaining = list(CASES[1:])
    offset = round_number % len(remaining)
    return [CASES[0], *(remaining[offset:] + remaining[:offset])]


class Consumer:
    def __init__(self, http_session=None, io_url=None):
        self.http_session, self.io_url = http_session, io_url
        self.reset()

    def reset(self):
        self.calls = self.characters = self.unmatched_calls = 0
        self.service_times = []

    async def accept(self, text):
        if text != "hello":
            raise AssertionError(f"unexpected decoded message: {text!r}")
        if self.io_url:
            async with self.http_session.get(self.io_url) as response:
                if response.status != 200 or await response.text() != "hello":
                    raise AssertionError("invalid shared I/O response")
                self.service_times.append(int(response.headers["X-Service-Delay-Ns"]))
        self.calls += 1
        self.characters += len(text)


def verify(consumer, events, matching):
    expected = events * matching
    actual = (consumer.calls, consumer.characters, consumer.unmatched_calls)
    if actual != (expected, expected * 5, 0):
        raise AssertionError(f"incorrect work: {actual}, expected {(expected, expected * 5, 0)}")


async def measure(engine_cls, case, args, process):
    import aiohttp
    io_url = args.io_url if case.get("sessions") == 16 else None
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=16),
                                    timeout=aiohttp.ClientTimeout(total=30)) as http_session:
        consumer = Consumer(http_session, io_url)
        engine = engine_cls()
        setup_started = perf_counter()
        await engine.setup(case, consumer)
        setup_seconds = perf_counter() - setup_started
        try:
            sessions = case.get("sessions", 1)
            for index in range(args.warmup):
                await engine.emit(index, index % sessions)
            verify(consumer, args.warmup, case["matching"])
            if isinstance(engine, AstrBotEngine):
                await engine.close()  # settle the native asynchronous preference writer
            else:
                await asyncio.sleep(0)
            gc.collect()
            consumer.reset()
            latencies = []
            rss_before = process.memory_info().rss
            cpu_start, started = process_time(), perf_counter()

            async def producer(session):
                for index in range(session, args.events, sessions):
                    event_started = perf_counter_ns()
                    await engine.emit(index, session)
                    latencies.append(perf_counter_ns() - event_started)

            await asyncio.gather(*(producer(session) for session in range(sessions)))
            elapsed, cpu_seconds = perf_counter() - started, process_time() - cpu_start
            verify(consumer, args.events, case["matching"])
            result = dict(case, events=args.events, elapsed_seconds=elapsed,
                          events_per_second=args.events / elapsed, cpu_seconds=cpu_seconds,
                          latency_p50_us=percentile(latencies, .50),
                          latency_p95_us=percentile(latencies, .95),
                          latency_p99_us=percentile(latencies, .99), latency_max_us=max(latencies) / 1000,
                          rss_before_bytes=rss_before, rss_after_bytes=process.memory_info().rss,
                          setup_seconds=setup_seconds, verified_handler_calls=consumer.calls,
                          verified_characters=consumer.characters,
                          verified_unmatched_calls=consumer.unmatched_calls)
            if io_url:
                if len(consumer.service_times) != args.events:
                    raise AssertionError("incorrect HTTP request count")
                result.update(verified_http_calls=len(consumer.service_times),
                              service_delay_p50_us=percentile(consumer.service_times, .5),
                              service_delay_p95_us=percentile(consumer.service_times, .95))
            if isinstance(engine, TiffanyEngine16):
                snapshot = engine.bot.runtime.metrics.snapshot()
                received = snapshot.get("events_received_total", {"platform": "onebot", "adapter": "benchmark"})
                if received != args.events + args.warmup:
                    raise AssertionError(f"Tiffany metrics were not live: {received}")
                result["verified_metric_events"] = received
            if isinstance(engine, AstrBotEngine):
                result["pipeline_stages"] = engine.stage_names
                result["session_preferences"] = ("process-write cache" if engine.cached_preferences
                                                  else "SQLite default misses")
            return result
        finally:
            await engine.close()


async def worker(args):
    import psutil
    sys.path.insert(0, str(args.source_root))
    logging.basicConfig(level=logging.ERROR)
    cases = case_order(args.round)
    if args.framework == "astrbot":
        cases.append(dict(CASES[0], name="one_handler_sqlite_defaults", sqlite_defaults=True))
    result = {"framework": args.framework, "round": args.round, "cases": [],
              "runtime": sys.version, "event_loop": type(asyncio.get_running_loop()).__name__,
              "asyncio_debug": asyncio.get_running_loop().get_debug(), "gc_enabled": gc.isenabled(),
              "packages": {dist.metadata["Name"]: dist.version
                           for dist in importlib.metadata.distributions()},
              "tiffany_source_sha256": fingerprint(tiffany_sources(args.source_root), args.source_root)}
    for case in cases:
        result["cases"].append(await measure(ENGINES[args.framework], case, args, psutil.Process()))
    if args.framework == "astrbot":
        from astrbot.core import sp, db_helper
        await sp.close()
        await db_helper.engine.dispose()
    return result


async def serve():
    from aiohttp import web
    timer_period = None
    if sys.platform == "win32":
        import ctypes
        # Windows' default wait granularity can turn 5ms into 15ms. Change only
        # this service process's timer period, and restore it on normal cleanup.
        if ctypes.windll.winmm.timeBeginPeriod(1) != 0:
            raise RuntimeError("cannot enable a 1ms timer period for the I/O service")
        timer_period = 1

    async def delay(request):
        start = perf_counter_ns()
        # Enforce a real minimum even if the loop's monotonic timer wakes early.
        while (remaining := .005 - (perf_counter_ns() - start) / 1e9) > 0:
            await asyncio.sleep(remaining)
        return web.Response(text="hello", headers={"X-Service-Delay-Ns": str(perf_counter_ns() - start)})

    app = web.Application()
    app.router.add_get("/delay", delay)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    try:
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        print(json.dumps({"url": f"http://127.0.0.1:{port}/delay", "runtime": sys.version,
                          "monotonic_clock": vars(get_clock_info("monotonic")),
                          "windows_timer_period_ms": timer_period,
                          "aiohttp": importlib.metadata.version("aiohttp")}), flush=True)
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        if timer_period is not None:
            ctypes.windll.winmm.timeEndPeriod(timer_period)


def save(result, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run(args):
    source_hash = fingerprint(tiffany_sources(ROOT), ROOT)
    helper_hash = fingerprint([ROOT / name for name in HELPERS], ROOT)
    snapshot = ROOT / ".build-cache" / f"expanded-source-{source_hash[:16]}"
    if not snapshot.exists():
        for directory in ("core", "adapters"):
            shutil.copytree(ROOT / directory, snapshot / directory,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copy2(ROOT / "fields.py", snapshot / "fields.py")
    if fingerprint(tiffany_sources(snapshot), snapshot) != source_hash:
        raise AssertionError("source snapshot differs from the workspace")
    result = {"schema_version": 2, "started_at_utc": datetime.now(timezone.utc).isoformat(),
              "environment": {"os": platform.platform(), "cpu": platform.processor(),
                              "logical_cpus": os.cpu_count(), "tiffany_source_sha256": source_hash,
                              "benchmark_sha256": helper_hash,
                              "dependency_locks_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                                  for name in ("benchmarks/requirements-expanded.lock", "benchmarks/requirements-graia.lock",
                                               "benchmarks/koishi/package-lock.json")},
                              "node": subprocess.check_output([str(args.node), "--version"], text=True).strip()},
              "method": {"repeats": args.repeats, "events_per_case": args.events,
                         "warmup_per_case": args.warmup, "gc_enabled": True, "logging": "ERROR",
                         "llm": False, "platform_network": False, "io": "shared localhost HTTP server, enforced >=5ms perf_counter deadline",
                         "io_event_concurrency": 16, "io_sessions": 16,
                         "timing": "fresh native protocol dict, real parsing and dispatch through all handler completion",
                         "case_order": "fresh process/framework/round; baseline first; rotate other cases and framework order",
                         "latency": "closed-loop producer per session; no open-loop arrival queue measurement",
                         "memory": "worker RSS at warmed component boundary; shared HTTP server excluded",
                         "protocols": {"tiffany": "OneBot v11", "nonebot": "OneBot v11", "astrbot": "OneBot v11",
                                       "graia": "Mirai API HTTP", "entari": "Satori", "koishi": "Satori/MockBot"}},
              "samples": [], "summary": []}
    if args.resume:
        old = json.loads(args.output.read_text(encoding="utf-8"))
        if old["environment"]["tiffany_source_sha256"] != source_hash or old["environment"]["benchmark_sha256"] != helper_hash:
            raise ValueError("cannot resume with changed source or benchmark")
        if old["method"] != result["method"]:
            raise ValueError("cannot resume with different methodology")
        result = old
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    server = subprocess.Popen([str(args.server_python), "-B", str(Path(__file__).resolve()), "--serve"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", env=env)
    lines = queue.Queue()
    threading.Thread(target=lambda: lines.put(server.stdout.readline()), daemon=True).start()
    try:
        line = lines.get(timeout=20)
        if not line:
            raise RuntimeError(f"shared HTTP server failed: {server.stderr.read()[-4000:]}")
        server_info = json.loads(line)
        io_url = server_info.pop("url")
        result["environment"]["io_server"] = server_info
        completed = {(sample["round"], sample["framework"]) for sample in result["samples"]}
        for repeat in range(args.repeats):
            offset = repeat % len(FRAMEWORKS)
            for framework in FRAMEWORKS[offset:] + FRAMEWORKS[:offset]:
                if (repeat, framework) in completed:
                    continue
                with tempfile.TemporaryDirectory(prefix="Tiffany expanded ") as temporary:
                    child_env = dict(env, ASTRBOT_ROOT=temporary, ASTRBOT_DISABLE_METRICS="1")
                    if framework == "koishi":
                        options = {"events": args.events, "warmup": args.warmup, "round": repeat,
                                   "cases": case_order(repeat), "io_url": io_url, "node_env": str(args.node_env.resolve())}
                        command = [str(args.node), "--expose-gc", str(ROOT / "benchmarks/koishi_worker.cjs"), json.dumps(options)]
                    else:
                        interpreter = args.graia_python if framework == "graia" else args.python
                        command = [str(interpreter), "-B", str(Path(__file__).resolve()), "--worker", "--framework", framework,
                                   "--events", str(args.events), "--warmup", str(args.warmup), "--round", str(repeat),
                                   "--source-root", str(snapshot), "--io-url", io_url]
                    child = subprocess.run(command, cwd=temporary, env=child_env, capture_output=True,
                                           encoding="utf-8", timeout=600)
                    if child.returncode:
                        raise RuntimeError(f"{framework}: {child.stdout[-2000:]}\n{child.stderr[-7000:]}")
                    sample = json.loads(child.stdout.strip().splitlines()[-1])
                    if framework != "koishi" and sample["tiffany_source_sha256"] != source_hash:
                        raise AssertionError("worker tested a different source")
                    result["samples"].append(sample)
                    save(result, args.output)
                    baseline = next(case for case in sample["cases"] if case["name"] == "one_handler")
                    print(f"Round {repeat + 1}/{args.repeats}: {framework} verified; single handler {baseline['events_per_second']:,.0f} events/s", flush=True)
        result["summary"] = []
        for framework in FRAMEWORKS:
            names = [case["name"] for case in CASES]
            if framework == "astrbot":
                names.append("one_handler_sqlite_defaults")
            for name in names:
                cases = [case for sample in result["samples"] if sample["framework"] == framework
                         for case in sample["cases"] if case["name"] == name]
                if len(cases) != args.repeats:
                    raise AssertionError("missing measurement samples")
                row = {"framework": framework, "case": name, "samples": len(cases)}
                for key in ("events_per_second", "latency_p50_us", "latency_p95_us", "latency_p99_us",
                            "rss_before_bytes", "cpu_seconds", "service_delay_p50_us", "service_delay_p95_us"):
                    if key not in cases[0]:
                        continue
                    values = [case[key] for case in cases]
                    row[key] = {"median": statistics.median(values), "min": min(values), "max": max(values)}
                result["summary"].append(row)
        if fingerprint(tiffany_sources(ROOT), ROOT) != source_hash or fingerprint([ROOT / name for name in HELPERS], ROOT) != helper_hash:
            raise AssertionError("source changed during measurement; snapshot results retained but workspace differs")
        result["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        save(result, args.output)
        print(f"Saved {args.output}")
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)
        server.stdout.close()
        server.stderr.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=positive_integer, default=1000)
    parser.add_argument("--warmup", type=positive_integer, default=200)
    parser.add_argument("--repeats", type=positive_integer, default=7)
    parser.add_argument("--output", type=Path, default=ROOT / ".build-cache/benchmarks/comparison/framework-comparison-expanded.json")
    parser.add_argument("--python", type=Path, default=ROOT / ".build-cache/expanded-python-env/Scripts/python.exe")
    parser.add_argument("--graia-python", type=Path, default=ROOT / ".build-cache/graia-env/Scripts/python.exe")
    parser.add_argument("--server-python", type=Path, default=Path(sys.executable),
                        help="HTTP service interpreter (Windows: prefer 3.13+ high-resolution monotonic clock)")
    parser.add_argument("--node", type=Path, default=Path("C:/Program Files/nodejs/node.exe"))
    parser.add_argument("--node-env", type=Path, default=ROOT / ".build-cache/expanded-node-env")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--framework", choices=FRAMEWORKS, help=argparse.SUPPRESS)
    parser.add_argument("--round", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--source-root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    parser.add_argument("--io-url", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.serve:
        asyncio.run(serve())
    elif args.worker:
        print(json.dumps(asyncio.run(worker(args)), ensure_ascii=False))
    else:
        run(args)


if __name__ == "__main__":
    main()
