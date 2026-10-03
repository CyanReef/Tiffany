"""Dispatch tracing, sampling, bounded storage and payload privacy."""

import asyncio
import unittest
from dataclasses import fields

from core import Bot, Envelope
from core.Trace import DispatchRecord, TraceRecorder


class TraceTests(unittest.IsolatedAsyncioTestCase):
    async def test_trace_is_opt_in_bounded_and_payload_free(self):
        bot = Bot()

        @bot.application.hook(name="trace-me", source="hooks.py:10")
        async def traced(ctx):
            pass

        await bot.setup()
        await bot.start()
        raw = {"secret": "must-not-be-retained"}
        await bot.emit(Envelope(
            "test", raw, connection_id="connection", session_id="session"
        ))
        self.assertEqual(bot.dispatcher.trace.snapshot(), ())
        bot.dispatcher.trace.enable()
        await bot.emit(Envelope(
            "test", raw, connection_id="connection", session_id="session"
        ))
        record = bot.dispatcher.trace.snapshot()[0]
        await bot.stop()
        self.assertEqual(record.result, "completed")
        self.assertEqual(record.connection_id, "connection")
        self.assertTrue(record.session_id.startswith("session:"))
        self.assertNotEqual(record.session_id, "session")
        self.assertEqual(record.hook_source, "hooks.py:10")
        self.assertEqual(record.hook_order, 0)
        self.assertIsNone(record.error_summary)
        self.assertFalse(hasattr(record, "raw"))
        self.assertFalse(hasattr(record, "traceback"))
        field_names = {field.name for field in fields(record)}
        self.assertNotIn("service_result", field_names)
        self.assertNotIn("user_id", field_names)

    async def test_trace_modes_sampling_order_and_bounded_buffer(self):
        trace = TraceRecorder(mode="sample", sample_rate=1.0, capacity=2)
        bot = Bot()
        bot.dispatcher.trace = trace

        @bot.application.hook(
            name="first",
            priority=10,
            source="hooks.py:20",
        )
        async def first(ctx):
            pass

        @bot.application.hook(name="second", source="hooks.py:30")
        async def second(ctx):
            pass

        await bot.setup()
        await bot.start()
        await bot.emit(Envelope("test", {}, event_id="sampled-event"))
        records = trace.snapshot()
        self.assertEqual(trace.mode, "sample")
        self.assertEqual([record.hook_order for record in records], [0, 1])
        self.assertEqual(
            [record.hook_source for record in records],
            ["hooks.py:20", "hooks.py:30"],
        )

        await bot.emit(Envelope("test", {}, event_id="next-event"))
        self.assertEqual(len(trace.snapshot()), 2)
        self.assertEqual(trace.dropped, 2)
        self.assertTrue(trace.disable())
        self.assertEqual(trace.mode, "off")
        self.assertFalse(trace.disable())
        self.assertTrue(trace.enable())
        self.assertEqual(trace.mode, "all")
        await bot.stop()

        never = TraceRecorder(mode="sample", sample_rate=0.0)
        self.assertFalse(never.record(_record(event_id="not-sampled")))
        self.assertEqual(never.snapshot(), ())

        first_reference = trace.session_reference("private-user-123")
        self.assertEqual(
            first_reference,
            trace.session_reference("private-user-123"),
        )
        self.assertNotIn("private-user-123", first_reference)

    async def test_trace_error_summary_does_not_retain_exception_message(self):
        bot = Bot()
        bot.dispatcher.trace.enable()

        @bot.hook(name="failure", on_error="continue")
        async def failure(ctx):
            raise RuntimeError(
                "secret-token user_id=123 service-result=private"
            )

        await bot.setup()
        await bot.start()
        with self.assertLogs("core.Dispatcher", level="ERROR"):
            await bot.emit(Envelope("test", {"secret": "raw-secret"}))
        record = bot.dispatcher.trace.snapshot()[0]
        await bot.stop()
        self.assertEqual(record.error_type, "RuntimeError")
        self.assertEqual(record.error_summary, "RuntimeError during handler")
        rendered = repr(record)
        self.assertNotIn("secret-token", rendered)
        self.assertNotIn("raw-secret", rendered)
        self.assertNotIn("service-result", rendered)

    async def test_trace_records_timeout_and_skipped_results(self):
        bot = Bot()
        bot.dispatcher.trace.enable()

        @bot.hook(
            name="timeout",
            priority=20,
            timeout=0.01,
            on_timeout="continue",
        )
        async def timeout(ctx):
            await asyncio.sleep(1)

        @bot.hook(name="stop", priority=10)
        async def stop(ctx):
            ctx.stop()

        @bot.hook(name="skipped")
        async def skipped(ctx):
            self.fail("stopped dispatch must not execute later hooks")

        await bot.setup()
        await bot.start()
        with self.assertLogs("core.Dispatcher", level="WARNING"):
            await bot.emit(Envelope("test", {}))
        records = bot.dispatcher.trace.snapshot()
        await bot.stop()
        self.assertEqual(
            [record.result for record in records],
            ["timeout", "stopped", "skipped"],
        )
        self.assertEqual(records[0].error_type, "HookTimeoutError")
        self.assertEqual(
            records[0].error_summary,
            "HookTimeoutError during handler",
        )
        self.assertEqual([record.hook_order for record in records], [0, 1, 2])


def _record(*, event_id: str) -> DispatchRecord:
    return DispatchRecord(
        event_id=event_id,
        connection_id=None,
        session_id=None,
        platform="test",
        adapter_id="adapter",
        hook_id=1,
        hook_name="hook",
        registry_version=1,
        phase="handler",
        result="completed",
        duration_seconds=0.001,
        hook_source="hooks.py:1",
        hook_order=0,
    )


if __name__ == "__main__":
    unittest.main()
