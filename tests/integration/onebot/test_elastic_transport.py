"""Real sockets keep control traffic moving while the event budget is full."""
import asyncio
import json
import unittest

from websockets.asyncio.client import connect

from adapters.OneBotWebSocketAdapter import OneBotWebSocketAdapter
from core import Bot, SchedulerPolicy


class ElasticTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_budget_keeps_echo_ping_and_drain_alive(self):
        bot = Bot(scheduler=SchedulerPolicy(max_events=16, max_concurrency=1))
        adapter = OneBotWebSocketAdapter('127.0.0.1', 0, 'onebot', call_timeout=2)
        bot.install(adapter)
        gate, entered = asyncio.Event(), asyncio.Event()
        completed = []
        @bot.hook(on='message')
        async def handle(ctx):
            entered.set()
            await gate.wait()
            result = await ctx.client.call('get_status')
            self.assertTrue(result.ok)
            completed.append(ctx.raw['message_id'])
        await bot.start()
        port = adapter._server.sockets[0].getsockname()[1]
        try:
            async with connect(f'ws://127.0.0.1:{port}/ws') as ws:
                for i in range(17):
                    await ws.send(json.dumps({'post_type': 'message', 'message_type': 'private',
                                              'user_id': 1, 'self_id': 2, 'message_id': i,
                                              'message': 'hello'}))
                await asyncio.wait_for(entered.wait(), 2)
                async with asyncio.timeout(2):
                    while bot.metrics.snapshot().get('onebot_events_dropped_total',
                            {'platform': 'onebot', 'adapter': adapter.adapter_id, 'reason': 'overload'}) != 1:
                        await asyncio.sleep(.001)
                self.assertEqual(bot.runtime.scheduler.active + bot.runtime.scheduler.queued, 16)
                pong = await ws.ping()
                await asyncio.wait_for(pong, 1)
                direct = asyncio.create_task(adapter.active_client.call('get_status'))
                request = json.loads(await asyncio.wait_for(ws.recv(), 1))
                await ws.send(json.dumps({'echo': request['echo'], 'status': 'ok', 'retcode': 0, 'data': {}}))
                self.assertTrue((await asyncio.wait_for(direct, 1)).ok)
                self.assertEqual(completed, [])
                drain = asyncio.create_task(bot.stop('drain'))
                await asyncio.sleep(0)
                self.assertFalse(drain.done())
                gate.set()
                for _ in range(16):
                    request = json.loads(await asyncio.wait_for(ws.recv(), 2))
                    await ws.send(json.dumps({'echo': request['echo'], 'status': 'ok', 'retcode': 0, 'data': {}}))
                report = await asyncio.wait_for(drain, 5)
                self.assertTrue(report.successful)
                self.assertEqual(completed, list(range(16)))
                self.assertEqual(bot.runtime.scheduler.buffered_bytes, 0)
                self.assertEqual(bot.runtime._owner_events, {})
                self.assertEqual(bot.runtime.scheduler._adapter_idle, {})
        finally:
            gate.set()
            await bot.stop('abort')
