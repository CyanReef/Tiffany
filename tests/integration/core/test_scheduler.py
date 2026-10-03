"""Runtime admission, concurrency limits and session fairness."""

import asyncio
import unittest

from core import Bot, Envelope, RuntimeOverloadedError


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = Bot()
        self.active = 0
        self.maximum = 0
        self.by_adapter = {}
        self.adapter_maximum = {}
        self.gate = asyncio.Event()

        @self.bot.hook()
        async def track(ctx):
            adapter = ctx.envelope.adapter_id
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            self.by_adapter[adapter] = self.by_adapter.get(adapter, 0) + 1
            self.adapter_maximum[adapter] = max(
                self.adapter_maximum.get(adapter, 0),
                self.by_adapter[adapter],
            )
            try:
                await self.gate.wait()
            finally:
                self.by_adapter[adapter] -= 1
                self.active -= 1

        await self.bot.setup()
        await self.bot.start()

    async def asyncTearDown(self):
        self.gate.set()
        await self.bot.stop("abort")

    async def test_global_and_adapter_limits(self):
        futures = []
        for index in range(40):
            adapter = f"adapter-{index % 5}"
            future = await self.bot.runtime.emit(
                Envelope(
                    "test", {}, adapter_id=adapter,
                    session_id=f"session-{index}",
                ),
                reject=True,
                wait=False,
            )
            futures.append(future)
        await asyncio.sleep(0.02)
        self.assertEqual(self.maximum, 16)
        self.assertLessEqual(max(self.adapter_maximum.values()), 4)
        self.gate.set()
        await asyncio.gather(*futures)

    async def test_session_fifo_and_cross_session_parallelism(self):
        self.gate.set()
        order = []
        running = set()
        overlap = asyncio.Event()
        bot = Bot()

        @bot.hook()
        async def track(ctx):
            marker = ctx.raw["marker"]
            session = ctx.envelope.session_id
            running.add(session)
            if len(running) > 1:
                overlap.set()
            await asyncio.sleep(0.01)
            order.append(marker)
            running.remove(session)

        await bot.setup()
        await bot.start()
        futures = []
        for marker, session in ((1, "hot"), (2, "hot"), (3, "cold")):
            futures.append(await bot.runtime.emit(
                Envelope("test", {"marker": marker}, session_id=session),
                reject=True,
                wait=False,
            ))
        await asyncio.gather(*futures)
        await bot.stop()
        self.assertTrue(overlap.is_set())
        self.assertLess(order.index(1), order.index(2))

    async def test_single_worker_preserves_session_fifo_and_cross_session_fairness(self):
        bot = Bot()
        entered, release = asyncio.Event(), asyncio.Event()
        order = []
        active = maximum = 0
        bot.runtime.scheduler.configure_adapter("bridge", 1)

        @bot.hook()
        async def track(ctx):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            try:
                marker = ctx.raw["marker"]
                order.append(marker)
                if marker == "A1":
                    entered.set()
                    await release.wait()
            finally:
                active -= 1

        await bot.start()
        try:
            futures = [await bot.runtime.emit(
                Envelope("test", {"marker": "A1"}, adapter_id="bridge",
                         connection_id="same-connection", session_id="A"), wait=False,
            )]
            await asyncio.wait_for(entered.wait(), timeout=1)
            for marker, session in (("A2", "A"), ("B1", "B")):
                futures.append(await bot.runtime.emit(
                    Envelope("test", {"marker": marker}, adapter_id="bridge",
                             connection_id="same-connection", session_id=session), wait=False,
                ))
            release.set()
            await asyncio.gather(*futures)
            self.assertEqual(maximum, 1)
            self.assertEqual(order, ["A1", "B1", "A2"])
        finally:
            release.set()
            await bot.stop("abort")

    async def test_capacity_and_session_backlog_reject_newest(self):
        scheduler = self.bot.runtime.scheduler
        scheduler.capacity = 2
        scheduler.session_backlog = 1
        first = await self.bot.runtime.emit(
            Envelope("test", {}, session_id="same"),
            reject=True,
            wait=False,
        )
        await asyncio.sleep(0)
        second = await self.bot.runtime.emit(
            Envelope("test", {}, session_id="same"),
            reject=True,
            wait=False,
        )
        with self.assertRaises(RuntimeOverloadedError):
            await self.bot.runtime.emit(
                Envelope("test", {}, session_id="same"),
                reject=True,
                wait=False,
            )
        dropped = await self.bot.runtime.emit(
            Envelope("test", {}, session_id="same"),
            reject=False,
            wait=False,
        )
        self.assertIsNone(dropped)
        self.gate.set()
        await asyncio.gather(first, second)

    async def test_total_capacity_counts_active_events(self):
        scheduler = self.bot.runtime.scheduler
        scheduler.capacity = 1
        first = await self.bot.runtime.emit(
            Envelope("test", {}, session_id="first"),
            reject=True,
            wait=False,
        )
        await asyncio.sleep(0)
        self.assertEqual(scheduler.active, 1)

        with self.assertRaises(RuntimeOverloadedError):
            await self.bot.runtime.emit(
                Envelope("test", {}, session_id="second"),
                reject=True,
                wait=False,
            )

        self.assertEqual(
            self.bot.metrics.snapshot().get(
                "event_queue_watermark",
                {"adapter": "application"},
            ),
            1,
        )
        self.gate.set()
        await first


if __name__ == "__main__":
    unittest.main()
