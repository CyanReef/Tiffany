"""One isolated Python framework worker; progress is JSON-lines on stdout."""
from __future__ import annotations

import argparse
import asyncio
import gc
import importlib.metadata
import json
import logging
from pathlib import Path
import sys
from time import perf_counter, perf_counter_ns, process_time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from benchmarks.compare import percentile
from benchmarks.representative.cases import parts


def send(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


class Consumer:
    def __init__(self, case, client, url):
        self.expected = "".join(parts(case))
        self.case, self.client, self.url = case, client, url
        self.gate = asyncio.Event()
        self.reset()

    def reset(self):
        self.calls = self.characters = self.unmatched_calls = self.predicate_calls = 0
        self.service_times = []
        self.started, self.finished = [], []
        self.active = self.peak_active = self.session_overlap = 0
        self.sessions_active = {}

    async def accept(self, text, index=None, session=None):
        assert text == self.expected, f"incorrect text, expected length {len(self.expected)}, got {len(text)}"
        if self.case.get("burst"):
            self.started.append(index)
            self.active += 1
            self.peak_active = max(self.peak_active, self.active)
            n = self.sessions_active.get(session, 0) + 1
            self.sessions_active[session] = n
            self.session_overlap = max(self.session_overlap, n)
            await self.gate.wait()
            self.active -= 1
            self.sessions_active[session] -= 1
            self.finished.append(index)
        if self.case.get("http"):
            async with self.client.get(self.url) as response:
                assert response.status == 200 and await response.text() == "hello"
                self.service_times.append(int(response.headers["X-Service-Delay-Ns"]))
        self.calls += 1
        self.characters += len(text)

    def verify(self, events):
        expected = events * self.case["matching"]
        assert (self.calls, self.characters, self.unmatched_calls, self.predicate_calls) == (
            expected, expected * len(self.expected), 0, events * self.case.get("predicates", 0))
        assert len(self.service_times) == (events if self.case.get("http") else 0)
        return {"verified_handler_calls": self.calls, "verified_characters": self.characters,
                "verified_unmatched_calls": self.unmatched_calls,
                "verified_predicate_calls": self.predicate_calls,
                "verified_http_calls": len(self.service_times)}


async def settle(engine):
    from benchmarks.representative.engines import AstrBotEngine
    if isinstance(engine, AstrBotEngine):
        await engine.close()
    else:
        await asyncio.sleep(0)


async def burst(engine, case, consumer):
    async def emit(index):
        try:
            await engine.emit(index, index % case["sessions"])
            return True
        except Exception as exc:
            if type(exc).__name__ == "RuntimeOverloadedError":
                return False
            raise

    tasks = [asyncio.create_task(emit(index)) for index in range(case["burst"])]
    previous, stable_since = -1, perf_counter()
    deadline = perf_counter() + 10
    while perf_counter() < deadline:
        await asyncio.sleep(.01)
        current = len(consumer.started)
        if current != previous:
            previous, stable_since = current, perf_counter()
        if current and perf_counter() - stable_since >= .1:
            break
    else:
        raise AssertionError("burst did not reach a stable blocked boundary")
    blocked = len(consumer.started)
    rejected_before_release = sum(task.done() and task.result() is False for task in tasks)
    native_state = {}
    from benchmarks.representative.engines import TiffanyEngine
    if isinstance(engine, TiffanyEngine):
        scheduler = engine.bot.runtime.scheduler
        native_state = {"native_active": scheduler.active, "native_queued": scheduler.queued,
                        "capacity": scheduler.capacity, "session_backlog": getattr(scheduler, "session_backlog", None),
                        "buffer_budget_bytes": getattr(scheduler, "buffer_budget_bytes", None),
                        "global_limit": scheduler.global_limit,
                        "adapter_limit": scheduler._adapter_limits.get("benchmark") or scheduler.global_limit}
        assert native_state["native_active"] + native_state["native_queued"] + rejected_before_release == case["burst"]
    consumer.gate.set()
    outcomes = await asyncio.wait_for(asyncio.gather(*tasks), timeout=60)
    accepted = sum(outcomes)
    assert accepted == consumer.calls == len(set(consumer.finished))
    assert consumer.active == 0 and not consumer.unmatched_calls
    fifo = all([index for index in consumer.finished if index % case["sessions"] == session] ==
               sorted(index for index in consumer.finished if index % case["sessions"] == session)
               for session in range(case["sessions"]))
    return {"submitted": case["burst"], "completed": accepted, "rejected": len(outcomes) - accepted,
            "blocked_handlers": blocked, "peak_active_handlers": consumer.peak_active,
            "max_same_session_overlap": consumer.session_overlap, "observed_completion_fifo": fifo,
            **native_state, **consumer.verify(accepted)}


async def measure(framework, case, options, process, first):
    import aiohttp
    from benchmarks.representative.engines import ENGINES
    send({"phase": "case_start", "case": case["name"]})
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=64),
                                    timeout=aiohttp.ClientTimeout(total=60)) as client:
        consumer = Consumer(case, client, options["io_url"])
        engine = ENGINES[framework]()
        setup_start = perf_counter()
        await engine.setup(case, consumer)
        setup_seconds = perf_counter() - setup_start
        if first:
            send({"phase": "ready"})
        try:
            if case.get("burst"):
                result = dict(case, **await burst(engine, case, consumer))
            else:
                sessions = case.get("sessions", 1)
                for index in range(options["warmup"]):
                    await engine.emit(index, index % sessions)
                consumer.verify(options["warmup"])
                await settle(engine)
                gc.collect()
                consumer.reset()
                rss = process.memory_info().rss
                latencies = []
                send({"phase": "timing", "case": case["name"]})
                cpu_start, started = process_time(), perf_counter()

                async def producer(session):
                    for index in range(session, options["events"], sessions):
                        event_start = perf_counter_ns()
                        await engine.emit(index, session)
                        latencies.append(perf_counter_ns() - event_start)

                await asyncio.gather(*(producer(session) for session in range(sessions)))
                elapsed, cpu_seconds = perf_counter() - started, process_time() - cpu_start
                result = dict(case, events=options["events"], elapsed_seconds=elapsed,
                              events_per_second=options["events"] / elapsed, cpu_seconds=cpu_seconds,
                              cpu_us_per_event=cpu_seconds / options["events"] * 1e6,
                              latency_p50_us=percentile(latencies, .5), latency_p95_us=percentile(latencies, .95),
                              latency_p99_us=percentile(latencies, .99), latency_max_us=max(latencies) / 1000,
                              rss_before_bytes=rss, **consumer.verify(options["events"]))
                if consumer.service_times:
                    result.update(service_delay_p50_us=percentile(consumer.service_times, .5),
                                  service_delay_p95_us=percentile(consumer.service_times, .95),
                                  service_delay_min_us=min(consumer.service_times) / 1000)
                    assert min(consumer.service_times) >= 5_000_000
                if framework == "tiffany":
                    metric = engine.bot.metrics.snapshot().get(
                        "events_received_total", {"platform": "onebot", "adapter": "benchmark"})
                    assert metric == options["events"] + options["warmup"]
                    result["verified_metric_events"] = metric
            result.update(rss_after_bytes=process.memory_info().rss, setup_seconds=setup_seconds)
            if framework == "astrbot":
                result.update(pipeline_stages=engine.stage_names,
                              session_preferences="process-write cache" if engine.cached_preferences else "SQLite default misses")
            return result
        finally:
            await engine.close()


async def main(options):
    import psutil
    logging.basicConfig(level=logging.ERROR)
    from benchmarks.compare_expanded import fingerprint
    from benchmarks.representative.sources import tiffany_sources
    # Historical helpers add the live repository to sys.path when imported.
    # Put the immutable production snapshot first after those imports and
    # before loading any framework components.
    sys.path.insert(0, options["source_root"])
    for index, case in enumerate(options["cases"]):
        result = await measure(options["framework"], case, options, psutil.Process(), index == 0)
        send({"phase": "case_result", "result": result})
    if options["framework"] == "astrbot":
        from astrbot.core import sp, db_helper
        await sp.close()
        await db_helper.engine.dispose()
    snapshot = Path(options["source_root"]).resolve()
    source_modules = {}
    for name, module in tuple(sys.modules.items()):
        if name.split(".")[0] not in ("core", "adapters", "clients", "deployment", "shared", "fields", "settings"):
            continue
        location = getattr(module, "__file__", None)
        if location:
            source_modules[name] = Path(location).resolve().relative_to(snapshot).as_posix()
    send({"phase": "complete", "runtime": sys.version,
          "event_loop": type(asyncio.get_running_loop()).__name__,
          "asyncio_debug": asyncio.get_running_loop().get_debug(),
          "tiffany_source_sha256": fingerprint(tiffany_sources(Path(options["source_root"])), Path(options["source_root"])),
          "source_modules": source_modules,
          "packages": {name: importlib.metadata.version(name) for name in
                       ("astrbot", "nonebot2", "nonebot-adapter-onebot", "aiohttp", "psutil")}})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("options", help="JSON from the coordinator")
    asyncio.run(main(json.loads(parser.parse_args().options)))
