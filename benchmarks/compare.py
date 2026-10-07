"""Compare real offline message paths in isolated processes; no platform login."""
from __future__ import annotations

import argparse
import asyncio
import gc
import hashlib
import importlib.metadata
import json
import logging
import math
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
from time import perf_counter, perf_counter_ns, process_time
from datetime import datetime, timezone
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
FRAMEWORKS = ("tiffany", "nonebot", "astrbot")
CASES = (
    {"name": "one_handler", "matching": 1, "unmatched": 0},
    {"name": "ten_handlers", "matching": 10, "unmatched": 0},
    {"name": "unmatched_100", "matching": 1, "unmatched": 100},
    {"name": "unmatched_1000", "matching": 1, "unmatched": 1000},
    {"name": "io_16_sessions", "matching": 1, "unmatched": 0,
     "sessions": 16, "delay": 0.005},
)


def raw_event(index: int, session: int) -> dict:
    return {"time": 1700000000, "self_id": 12345, "post_type": "message",
            "message_type": "private", "sub_type": "friend", "message_id": index,
            "user_id": 20000 + session, "font": 0, "raw_message": "hello",
            "sender": {"user_id": 20000 + session, "nickname": "benchmark"},
            "message": [{"type": "text", "data": {"text": "hello"}}]}


def percentile(values: list[int], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low, high = math.floor(position), math.ceil(position)
    return (ordered[low] + (ordered[high] - ordered[low]) * (position - low)) / 1000


class Consumer:
    def __init__(self, delay: float):
        self.delay = delay
        self.calls = 0
        self.characters = 0
        self.unmatched_calls = 0

    async def accept(self, text: str):
        if text != "hello":
            raise AssertionError(f"unexpected decoded message: {text!r}")
        if self.delay:
            await asyncio.sleep(self.delay)
        self.calls += 1
        self.characters += len(text)


class TiffanyEngine:
    async def setup(self, case, consumer):
        from core import Bot, Envelope
        from fields import TEXT
        from adapters.onebot_fields import detect_event_kind, register_onebot_fields

        self.bot = Bot()
        register_onebot_fields(self.bot, "onebot")

        async def handle(ctx):
            await consumer.accept(ctx.resolve(TEXT))

        async def unrelated(ctx):
            consumer.unmatched_calls += 1

        for index in range(case["matching"]):
            self.bot.register_hook(handle, name=f"matching_{index}", on="message")
        for index in range(case["unmatched"]):
            self.bot.register_hook(unrelated, name=f"notice_{index}", on="notice")
        self.Envelope, self.detect = Envelope, detect_event_kind
        await self.bot.__aenter__()

    async def emit(self, index, session):
        raw = raw_event(index, session)
        await self.bot.emit(self.Envelope(
            "onebot", raw, kind=self.detect(raw), adapter_id="benchmark",
            connection_id="offline", session_id=f"private:{raw['user_id']}",
        ))

    async def close(self):
        await self.bot.__aexit__(None, None, None)


class NoneBotEngine:
    initialized = False

    async def setup(self, case, consumer):
        import nonebot
        from nonebot.adapters.onebot.v11 import Adapter, Bot
        from nonebot.matcher import matchers
        from nonebot.log import logger

        if not self.initialized:
            nonebot.init(driver="~none+~httpx", log_level="ERROR", _env_file=None)
            type(self).initialized = True
        logger.remove()
        logger.add(sys.stderr, level="ERROR")
        matchers.clear()
        self.adapter = Adapter(nonebot.get_driver())
        self.bot = Bot(self.adapter, "12345")

        async def handle(event):
            await consumer.accept(event.get_plaintext())

        async def unrelated():
            consumer.unmatched_calls += 1

        # Event must be annotated for NoneBot's dependency injection.
        from nonebot.adapters.onebot.v11 import MessageEvent
        handle.__annotations__["event"] = MessageEvent
        for _ in range(case["matching"]):
            nonebot.on_message(priority=1, block=False).handle()(handle)
        for _ in range(case["unmatched"]):
            nonebot.on_notice(priority=1, block=False).handle()(unrelated)

    async def emit(self, index, session):
        event = self.adapter.json_to_event(raw_event(index, session))
        if event is None:
            raise AssertionError("NoneBot failed to parse the message")
        await self.bot.handle_event(event)

    async def close(self):
        from nonebot.matcher import matchers
        matchers.clear()


class UnusedResource:
    def __getattr__(self, name):
        raise AssertionError(f"disabled external resource was accessed: {name}")


class AstrBotEngine:
    async def setup(self, case, consumer):
        from astrbot.core import astrbot_config, db_helper, logger, sp
        from astrbot.core.pipeline.context import PipelineContext
        from astrbot.core.pipeline.scheduler import PipelineScheduler
        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import AiocqhttpAdapter
        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
        from astrbot.core.star.register.star_handler import get_handler_or_create
        from astrbot.core.star.filter.event_message_type import EventMessageType, EventMessageTypeFilter
        from astrbot.core.star.star import StarMetadata, star_map
        from astrbot.core.star.star_handler import EventType, star_handlers_registry
        from aiocqhttp import Event

        logger.setLevel(logging.ERROR)
        self.sp, self.db = sp, db_helper
        await sp.initialize()
        config = astrbot_config
        config["provider_settings"]["enable"] = False
        config["provider_stt_settings"]["enable"] = False
        config["provider_tts_settings"]["enable"] = False
        config["content_safety"]["enable"] = False
        config["platform_settings"]["rate_limit"]["count"] = 0
        config["platform_settings"]["enable_id_white_list"] = False
        config["platform_settings"]["segmented_reply"]["enable"] = False
        config["wake_prefix"] = []
        config["admins_id"] = []
        config["plugin_set"] = ["*"]
        star_handlers_registry.clear()
        module = "tiffany_comparison_plugin"
        star_map[module] = StarMetadata(name="benchmark", module_path=module, activated=True)

        def handler_at(index, matching):
            async def handle(event):
                if matching:
                    await consumer.accept(event.message_str)
                else:
                    consumer.unmatched_calls += 1
            handle.__name__ = f"handler_{index}"
            handle.__module__ = module
            return handle

        for index in range(case["matching"] + case["unmatched"]):
            matching = index < case["matching"]
            metadata = get_handler_or_create(
                handler_at(index, matching),
                EventType.AdapterMessageEvent if matching else EventType.OnLLMResponseEvent,
            )
            if matching:
                metadata.event_filters.append(EventMessageTypeFilter(EventMessageType.PRIVATE_MESSAGE))

        # Only unused LLM/conversation facilities are substituted. Real parsing,
        # filters, registry, nine pipeline stages and SQLite/preferences stay intact.
        plugin_context = SimpleNamespace(conversation_manager=UnusedResource(), get_config=lambda: config)
        self.scheduler = PipelineScheduler(PipelineContext(
            config, SimpleNamespace(context=plugin_context), "benchmark", db_helper,
        ))
        await self.scheduler.initialize()
        self.stage_names = [type(stage).__name__ for stage in self.scheduler.stages]
        if len(self.stage_names) != 9:
            raise AssertionError(f"unexpected AstrBot pipeline: {self.stage_names}")
        self.adapter = AiocqhttpAdapter(
            {"id": "benchmark", "type": "aiocqhttp", "ws_reverse_host": "127.0.0.1",
             "ws_reverse_port": 6199}, config["platform_settings"], asyncio.Queue(),
        )
        self.Event, self.MessageEvent = Event, AiocqhttpMessageEvent
        self.cached_preferences = not case.get("sqlite_defaults", False)
        for session in range(case.get("sessions", 1)):
            event = await self.event(0, session)
            # Remove values between cases before configuring the chosen variant.
            for key in ("session_plugin_config", "session_service_config"):
                await sp.remove_async("umo", event.unified_msg_origin, key)
                if self.cached_preferences:
                    await sp.put_async("umo", event.unified_msg_origin, key, {})
        await sp.flush()

    async def event(self, index, session):
        message = await self.adapter.convert_message(self.Event(raw_event(index, session)))
        if message is None:
            raise AssertionError("AstrBot rejected the test message")
        return self.MessageEvent(message.message_str, message, self.adapter.metadata,
                                 message.session_id, self.adapter.bot)

    async def emit(self, index, session):
        await self.scheduler.execute(await self.event(index, session))

    async def close(self):
        await self.sp.flush()
        for stage in self.scheduler.stages:
            recorder = getattr(stage, "_umo_auto_name_recorder", None)
            if recorder is not None and recorder._writer_task is not None:
                await recorder._writer_task


async def measure(engine_class, case, args, process):
    consumer = Consumer(case.get("delay", 0))
    engine = engine_class()
    setup_start = perf_counter()
    await engine.setup(case, consumer)
    setup_seconds = perf_counter() - setup_start
    sessions = case.get("sessions", 1)
    for index in range(args.warmup):
        await engine.emit(index, index % sessions)
    # Let real background setup and warmup work settle outside timing.
    if isinstance(engine, AstrBotEngine):
        await engine.close()
    else:
        await asyncio.sleep(0)
    gc.collect()
    consumer.calls = consumer.characters = consumer.unmatched_calls = 0
    latencies = []
    resident_before = process.memory_info().rss
    cpu_start, started = process_time(), perf_counter()

    async def producer(session):
        for index in range(session, args.events, sessions):
            event_started = perf_counter_ns()
            await engine.emit(index, session)
            latencies.append(perf_counter_ns() - event_started)

    await asyncio.gather(*(producer(session) for session in range(sessions)))
    elapsed, cpu_seconds = perf_counter() - started, process_time() - cpu_start
    expected = args.events * case["matching"]
    if (consumer.calls, consumer.characters, consumer.unmatched_calls) != (expected, expected * 5, 0):
        raise AssertionError(f"incorrect work: calls={consumer.calls}, expected={expected}, "
                             f"characters={consumer.characters}, unmatched={consumer.unmatched_calls}")
    result = dict(case, events=args.events, elapsed_seconds=elapsed,
                  events_per_second=args.events / elapsed, cpu_seconds=cpu_seconds,
                  latency_p50_us=percentile(latencies, .50), latency_p95_us=percentile(latencies, .95),
                  latency_p99_us=percentile(latencies, .99), latency_max_us=max(latencies) / 1000,
                  rss_before_bytes=resident_before, rss_after_bytes=process.memory_info().rss,
                  setup_seconds=setup_seconds, verified_handler_calls=consumer.calls,
                  verified_characters=consumer.characters,
                  verified_unmatched_calls=consumer.unmatched_calls)
    if isinstance(engine, AstrBotEngine):
        result["pipeline_stages"] = engine.stage_names
        result["session_preferences"] = "process-write cache" if engine.cached_preferences else "SQLite default misses"
    await engine.close()
    return result


async def worker(args):
    import psutil
    logging.basicConfig(level=logging.ERROR)
    sys.path.insert(0, str(ROOT))
    classes = {"tiffany": TiffanyEngine, "nonebot": NoneBotEngine, "astrbot": AstrBotEngine}
    # Keep a fresh-process baseline for RSS; rotate the remaining workloads.
    remaining = list(CASES[1:])
    offset = args.round % len(remaining)
    cases = [CASES[0], *(remaining[offset:] + remaining[:offset])]
    if args.framework == "astrbot":
        cases.append(dict(CASES[0], name="one_handler_sqlite_defaults", sqlite_defaults=True))
    result = {"framework": args.framework, "round": args.round, "cases": []}
    for case in cases:
        result["cases"].append(await measure(classes[args.framework], case, args, psutil.Process()))
    if args.framework == "astrbot":
        from astrbot.core import sp, db_helper
        await sp.close()
        await db_helper.engine.dispose()
    result["event_loop"] = type(asyncio.get_running_loop()).__name__
    result["asyncio_debug"] = asyncio.get_running_loop().get_debug()
    return result


def source_fingerprint():
    digest = hashlib.sha256()
    for path in sorted((ROOT / "core").rglob("*.py")) + [Path(__file__).resolve(), ROOT / "adapters/onebot_fields.py", ROOT / "fields.py"]:
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def run(args):
    result = {"schema_version": 1, "started_at_utc": datetime.now(timezone.utc).isoformat(),
              "environment": {"python": sys.version, "os": platform.platform(),
                              "cpu": platform.processor(), "logical_cpus": os.cpu_count(),
                              "tiffany_source_sha256": source_fingerprint()},
              "packages": {distribution.metadata['Name']: distribution.version
                           for distribution in importlib.metadata.distributions()},
              "method": {"repeats": args.repeats, "events_per_case": args.events,
                         "warmup_per_case": args.warmup, "gc_enabled": True,
                         "llm": False, "network": False, "logging": "ERROR",
                         "timing": "raw dict creation, real parsing, dispatch through handler completion",
                         "case_order": "baseline first, remaining cases rotate; framework order rotates; fresh process per framework per round",
                         "latency": "per-event wall time, closed-loop producers; not arrival-to-completion under open-loop load",
                         "memory": "worker RSS at warmed component boundary, not full application idle memory"},
              "samples": [], "summary": []}
    for repeat in range(args.repeats):
        offset = repeat % len(FRAMEWORKS)
        for framework in FRAMEWORKS[offset:] + FRAMEWORKS[:offset]:
            with tempfile.TemporaryDirectory(prefix="Tiffany framework comparison ") as temporary:
                environment = dict(os.environ, ASTRBOT_ROOT=temporary, ASTRBOT_DISABLE_METRICS="1", PYTHONUTF8="1",
                                   PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
                command = [sys.executable, "-B", str(Path(__file__).resolve()), "--worker",
                           "--framework", framework, "--events", str(args.events),
                           "--warmup", str(args.warmup), "--round", str(repeat)]
                child = subprocess.run(command, cwd=temporary, env=environment,
                                       capture_output=True, encoding="utf-8", timeout=600)
                if child.returncode:
                    raise RuntimeError(f"{framework} failed:\n{child.stdout[-2000:]}\n{child.stderr[-6000:]}")
                sample = json.loads(child.stdout.strip().splitlines()[-1])
                result["samples"].append(sample)
                print(f"Round {repeat + 1}/{args.repeats}: {framework} verified", flush=True)
    for framework in FRAMEWORKS:
        names = [case["name"] for case in CASES]
        if framework == "astrbot":
            names.append("one_handler_sqlite_defaults")
        for name in names:
            cases = [case for sample in result["samples"] if sample["framework"] == framework
                     for case in sample["cases"] if case["name"] == name]
            row = {"framework": framework, "case": name, "samples": len(cases)}
            for key in ("events_per_second", "latency_p50_us", "latency_p95_us", "latency_p99_us",
                        "rss_before_bytes", "cpu_seconds"):
                values = [case[key] for case in cases]
                row[key] = {"median": statistics.median(values), "min": min(values), "max": max(values)}
            result["summary"].append(row)
    result["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Saved {args.output}")


def positive_integer(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=positive_integer, default=1000)
    parser.add_argument("--warmup", type=positive_integer, default=100)
    parser.add_argument("--repeats", type=positive_integer, default=5)
    parser.add_argument("--output", type=Path, default=ROOT / ".build-cache/benchmarks/comparison/framework-comparison.json")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--framework", choices=FRAMEWORKS, help=argparse.SUPPRESS)
    parser.add_argument("--round", type=int, default=0, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(asyncio.run(worker(args)), ensure_ascii=False))
    else:
        run(args)


if __name__ == "__main__":
    main()
