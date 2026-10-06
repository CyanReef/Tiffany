"""Elastic admission, reserve protection and budget ownership."""
import asyncio
import unittest
from unittest.mock import patch

from core import Bot, Envelope, RuntimeOverloadedError, SchedulerPolicy


class ElasticSchedulerTests(unittest.IsolatedAsyncioTestCase):
    def assert_empty(self, bot):
        scheduler = bot.runtime.scheduler
        self.assertEqual((scheduler.active, scheduler.queued, scheduler.buffered_bytes), (0, 0, 0))
        self.assertEqual(scheduler._lanes, {})
        self.assertEqual(scheduler._ready, {})
        self.assertEqual(list(scheduler._runnable), [])
        self.assertEqual(scheduler._runnable_set, set())
        self.assertEqual(bot.runtime._owner_events, {})

    async def test_default_bursts_complete_fifo_without_duplicate_or_overlap(self):
        for sessions, count in [(1, 128), (1, 256), (1, 512), (1, 1024), (1, 4096), (16, 4096), (64, 4096)]:
            with self.subTest(sessions=sessions, count=count):
                bot = Bot()
                gate = asyncio.Event()
                entered, finished, active = [], [], set()
                @bot.hook()
                async def handle(ctx):
                    session = ctx.envelope.session_id
                    self.assertNotIn(session, active)
                    active.add(session)
                    entered.append(ctx.raw['index'])
                    try:
                        await gate.wait()
                        finished.append(ctx.raw['index'])
                    finally:
                        active.remove(session)
                async with bot:
                    futures = [bot.runtime.submit(Envelope('test', {'index': i}, session_id=str(i % sessions)), reject=True)
                               for i in range(count)]
                    await asyncio.sleep(0)
                    self.assertEqual(len(entered), sessions)
                    self.assertEqual(bot.runtime.scheduler.active, sessions)
                    gate.set()
                    await asyncio.gather(*futures)
                    self.assertEqual(len(set(finished)), count)
                    for session in range(sessions):
                        self.assertEqual([i for i in finished if i % sessions == session], list(range(session, count, sessions)))
                    self.assert_empty(bot)

    async def test_hot_session_cannot_take_reserved_slots(self):
        bot = Bot(scheduler=SchedulerPolicy(max_events=64))
        bot.runtime.scheduler.global_limit = 0
        async with bot:
            hot = [bot.runtime.submit(Envelope('test', {}, session_id='hot'), reject=True) for _ in range(56)]
            with patch.object(type(bot.runtime), '_capture_event', side_effect=AssertionError('reject must not capture')):
                self.assertIsNone(bot.runtime.submit(Envelope('test', {}, session_id='hot')))
            cold = [bot.runtime.submit(Envelope('test', {}, session_id=f'cold-{i}'), reject=True) for i in range(8)]
            self.assertFalse(bot.runtime.scheduler.accepting)
            with self.assertRaisesRegex(RuntimeOverloadedError, 'capacity'):
                bot.runtime.submit(Envelope('test', {}, session_id='another'), reject=True)
            bot.runtime.scheduler.global_limit = 64
            await asyncio.gather(*hot, *cold)
            self.assert_empty(bot)

    async def test_default_overload_bounds_tasks_and_recovers_after_drain(self):
        bot = Bot()
        gate = asyncio.Event()
        calls = 0
        @bot.hook()
        async def work(ctx):
            nonlocal calls
            await gate.wait()
            calls += 1
        async with bot:
            accepted = []
            rejected = 0
            for i in range(9000):
                future = bot.runtime.submit(Envelope('test', {}, session_id=str(i % 256)))
                if future is None:
                    rejected += 1
                else:
                    accepted.append(future)
            self.assertEqual((len(accepted), rejected), (8192, 808))
            scheduler = bot.runtime.scheduler
            self.assertEqual((scheduler.active, scheduler.queued, scheduler.buffered_bytes), (64, 8128, 8192 * 4096))
            self.assertEqual(len(bot.runtime.tasks.tasks_for('events')), 64)
            gate.set()
            await asyncio.gather(*accepted)
            self.assertEqual(calls, 8192)
            self.assert_empty(bot)
            self.assertIsNotNone(bot.runtime.submit(Envelope('test', {}, session_id='recovered'), reject=True))

    async def test_byte_reserve_large_event_and_unscoped_protection(self):
        bot = Bot(scheduler=SchedulerPolicy(buffer_budget_bytes=65536))
        bot.runtime.scheduler.global_limit = 0
        async with bot:
            hot = [bot.runtime.submit(Envelope('test', {}, session_id='hot', admission_bytes=4096), reject=True) for _ in range(14)]
            self.assertIsNone(bot.runtime.submit(Envelope('test', {}, session_id='hot', admission_bytes=4096)))
            self.assertIsNone(bot.runtime.submit(Envelope('test', {}, admission_bytes=4096)))
            self.assertIsNone(bot.runtime.submit(Envelope('test', {}, session_id='large', admission_bytes=8193)))
            cold = bot.runtime.submit(Envelope('test', {}, session_id='cold', admission_bytes=8192), reject=True)
            self.assertEqual(bot.runtime.scheduler.buffered_bytes, 65536)
            self.assertFalse(bot.runtime.scheduler.accepting)
            bot.runtime.scheduler.global_limit = 64
            await asyncio.gather(*hot, cold)
            self.assert_empty(bot)
            with self.assertRaisesRegex(RuntimeOverloadedError, 'buffer_budget'):
                bot.runtime.submit(Envelope('test', {}, admission_bytes=65537), reject=True)

    async def test_cancel_queued_last_item_keeps_active_lane_charge(self):
        bot = Bot()
        entered, gate = asyncio.Event(), asyncio.Event()
        @bot.hook()
        async def work(ctx):
            entered.set()
            await gate.wait()
        async with bot:
            first = bot.runtime.submit(Envelope('test', {}, session_id='same', admission_bytes=100), reject=True)
            await entered.wait()
            waiter = asyncio.create_task(bot.emit(Envelope('test', {}, session_id='same', admission_bytes=200)))
            await asyncio.sleep(0)
            waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiter
            self.assertEqual(bot.runtime.scheduler.buffered_bytes, 100)
            gate.set()
            await first
            self.assert_empty(bot)

    async def test_synchronous_result_cancellation_does_not_cancel_work(self):
        bot = Bot()
        done = asyncio.Event()
        @bot.hook()
        async def work(ctx):
            done.set()
        async with bot:
            future = bot.runtime.submit(Envelope('test', {}), reject=True)
            future.cancel()
            await done.wait()
            self.assert_empty(bot)

    async def test_inherited_limits_follow_global_and_adapter_drain_waits(self):
        bot = Bot(scheduler=SchedulerPolicy(max_concurrency=1))
        gate = asyncio.Event()
        @bot.hook()
        async def work(ctx):
            await gate.wait()
        async with bot:
            futures = [bot.runtime.submit(Envelope('test', {}, adapter_id='a', session_id=str(i)), reject=True) for i in range(4)]
            drain = asyncio.create_task(bot.runtime.scheduler.wait_adapter_idle('a'))
            await asyncio.sleep(0)
            self.assertFalse(drain.done())
            bot.runtime.scheduler.global_limit = 4
            self.assertEqual(bot.runtime.scheduler.active, 4)
            bot.runtime.scheduler.global_limit = 1
            self.assertEqual(bot.runtime.scheduler.active, 4)
            bot.runtime.scheduler.capacity = 2
            bot.runtime.scheduler.buffer_budget_bytes = 8192
            self.assertEqual(bot.runtime.scheduler.buffered_bytes, 16384)
            self.assertFalse(bot.runtime.scheduler.accepting)
            self.assertIsNone(bot.runtime.submit(Envelope('test', {}, adapter_id='a', session_id='new')))
            gate.set()
            await asyncio.gather(*futures, drain)
            self.assert_empty(bot)
            self.assertEqual(bot.runtime.scheduler._adapter_idle, {})

    async def test_saturated_adapter_is_excluded_from_runnable_rotation(self):
        bot = Bot(scheduler=SchedulerPolicy(max_concurrency=2))
        bot.runtime.scheduler.configure_adapter('slow', 1)
        gate, fast_done = asyncio.Event(), asyncio.Event()
        @bot.hook()
        async def work(ctx):
            if ctx.envelope.adapter_id == 'slow':
                await gate.wait()
            else:
                fast_done.set()
        async with bot:
            futures = [bot.runtime.submit(Envelope('test', {}, adapter_id='slow', session_id=str(i)), reject=True) for i in range(100)]
            self.assertNotIn('slow', bot.runtime.scheduler._runnable_set)
            fast = bot.runtime.submit(Envelope('test', {}, adapter_id='fast'), reject=True)
            await asyncio.wait_for(fast_done.wait(), 1)
            gate.set()
            await asyncio.gather(*futures, fast)
            self.assert_empty(bot)

    async def test_invalid_charge_does_not_reserve_resources(self):
        async with Bot() as bot:
            for charge in [0, -1, True, 1.5, '100']:
                with self.subTest(charge=charge), self.assertRaises(ValueError):
                    bot.runtime.submit(Envelope('test', {}, admission_bytes=charge))
                self.assert_empty(bot)

    async def test_unscoped_event_cannot_collide_with_session_name(self):
        bot = Bot(scheduler=SchedulerPolicy(max_concurrency=2))
        gate = asyncio.Event()
        @bot.hook()
        async def work(ctx):
            await gate.wait()
        async with bot:
            scoped = bot.runtime.submit(Envelope('test', {}, session_id='__unscoped__:1'), reject=True)
            unscoped = bot.runtime.submit(Envelope('test', {}), reject=True)
            self.assertEqual(bot.runtime.scheduler.active, 2)
            gate.set()
            await asyncio.gather(scoped, unscoped)
            self.assert_empty(bot)

    async def test_cancelled_drain_releases_waiter_but_other_drain_stays(self):
        bot = Bot()
        gate = asyncio.Event()
        @bot.hook()
        async def work(ctx):
            await gate.wait()
        async with bot:
            future = bot.runtime.submit(Envelope('test', {}, adapter_id='a'), reject=True)
            first = asyncio.create_task(bot.runtime.scheduler.wait_adapter_idle('a'))
            second = asyncio.create_task(bot.runtime.scheduler.wait_adapter_idle('a'))
            await asyncio.sleep(0)
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            self.assertEqual(bot.runtime.scheduler._adapter_idle['a'].users, 1)
            second.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await second
            self.assertEqual(bot.runtime.scheduler._adapter_idle, {})
            gate.set()
            await future
            self.assert_empty(bot)

    def test_policy_validation(self):
        for key in ('max_events', 'buffer_budget_bytes', 'max_concurrency'):
            for value in (0, -1, True, 1.5, '64'):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    SchedulerPolicy(**{key: value})


if __name__ == '__main__':
    unittest.main()
