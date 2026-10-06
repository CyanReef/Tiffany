import asyncio
import unittest

import aiohttp
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from adapters.OneBotWebSocketAdapter import OneBotWebSocketAdapter
from core import Bot, Envelope, OpenMetricsExporter, RuntimeState, ServiceKey


class HealthAndAuthTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_metrics_readiness_connection_and_token_auth(self):
        bot = Bot()
        adapter = bot.install(OneBotWebSocketAdapter("127.0.0.1", 0, "test", token="test-token"))
        monitor = OpenMetricsExporter(enabled=True, port=0,
            live=lambda: bot.runtime.state == RuntimeState.RUNNING,
            ready=lambda: adapter.ready and bot.runtime.scheduler.accepting)
        bot.service(ServiceKey("monitor"), monitor)
        @bot.hook(name="exported")
        async def exported(ctx):
            pass
        await bot.start()
        await bot.emit(Envelope("test", {}, adapter_id="test"))
        bot.metrics.inc("test_events_total", labels={"adapter": "test"})
        port = monitor._site._server.sockets[0].getsockname()[1]
        ws_port = adapter._server.sockets[0].getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        try:
            async with aiohttp.ClientSession() as client:
                async with client.get(base + "/livez") as response:
                    self.assertEqual(response.status, 200)
                async with client.get(base + "/readyz") as response:
                    self.assertEqual(response.status, 503)
                async with client.get(base + "/metrics") as response:
                    content = await response.text()
                    self.assertIn('test_events_total{adapter="test"} 1.0', content)
                    self.assertIn('events_received_total{adapter="test",platform="test"} 1.0', content)
                    self.assertIn('event_duration_seconds_count{adapter="test",platform="test"} 1.0', content)
                    self.assertIn('hook_executions_total{hook="exported",platform="test",reason="completed"} 1.0', content)
                    self.assertIn('hook_duration_seconds_count{hook="exported",platform="test",reason="completed"} 1.0', content)
                    self.assertIn('events_active{adapter="test"} 0.0', content)
                    self.assertIn('event_queue_depth{adapter="test"} 0.0', content)
                    self.assertTrue(content.endswith("# EOF\n"))
                    self.assertIn("application/openmetrics-text", response.headers["Content-Type"])
                for headers in (None, {"Authorization": "Bearer wrong"},
                                [("Authorization", "Bearer test-token"), ("Authorization", "Bearer test-token")]):
                    with self.assertRaises(InvalidStatus) as error:
                        async with connect(f"ws://127.0.0.1:{ws_port}", additional_headers=headers):
                            pass
                    self.assertEqual(error.exception.response.status_code, 401)
                async with connect(f"ws://127.0.0.1:{ws_port}", additional_headers={"Authorization": "Bearer test-token"}):
                    async with client.get(base + "/readyz") as response:
                        self.assertEqual(response.status, 200)
                    bot.runtime.scheduler.stop_admission()
                    async with client.get(base + "/readyz") as response:
                        self.assertEqual(response.status, 503)
                async with asyncio.timeout(2):
                    while adapter.ready:
                        await asyncio.sleep(0.01)
                async with client.get(base + "/readyz") as response:
                    self.assertEqual(response.status, 503)
        finally:
            self.assertTrue((await bot.stop()).successful)

    async def test_callback_failure_returns_unavailable(self):
        def broken():
            raise RuntimeError("health")
        bot = Bot()
        monitor = OpenMetricsExporter(enabled=True, port=0, ready=broken)
        bot.service(ServiceKey("monitor"), monitor)
        await bot.start()
        port = monitor._site._server.sockets[0].getsockname()[1]
        try:
            async with aiohttp.ClientSession() as client:
                async with client.get(f"http://127.0.0.1:{port}/readyz") as response:
                    self.assertEqual(response.status, 503)
        finally:
            await bot.stop()
