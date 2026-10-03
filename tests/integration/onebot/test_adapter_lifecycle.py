"""Adapter installation and local WebSocket server lifecycle."""

import unittest

from adapters import create_adapter, install_adapter
from core import Bot
from settings import AdapterConfig, WebSocketConfig


class AdapterLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_install_adapter_integrates_with_runtime_lifecycle(self):
        bot = Bot()
        config = AdapterConfig(
            type="onebot_websocket",
            platform="napcat",
            websocket=WebSocketConfig(host="127.0.0.1", port=0),
        )
        adapter = install_adapter(bot, config)
        await bot.setup()
        await bot.start()
        self.assertIsNotNone(adapter._server)
        report = await bot.stop("abort")
        self.assertTrue(report.successful)

    async def test_direct_install_registers_shared_protocol_providers(self):
        from fields import TEXT

        bot = Bot()
        config = AdapterConfig(
            type="onebot_websocket",
            platform="napcat",
            websocket=WebSocketConfig(host="127.0.0.1", port=0),
        )
        adapter = create_adapter(config)
        self.assertEqual(
            bot.providers.handles_for_owner("protocol.onebot11"),
            (),
        )

        bot.install(adapter)
        await bot.setup()
        self.assertTrue(
            bot.providers.handles_for_owner("protocol.onebot11")
        )
        self.assertEqual(
            bot.providers.get(TEXT, "napcat").namespace,
            "protocol.onebot11",
        )
        await bot.start()
        await bot.stop("abort")

    async def test_runtime_drain_without_connection_terminates_cleanly(self):
        bot = Bot()
        config = AdapterConfig(
            type="onebot_websocket",
            platform="napcat",
            websocket=WebSocketConfig(host="127.0.0.1", port=0),
        )
        install_adapter(bot, config)
        await bot.setup()
        await bot.start()
        report = await bot.stop("drain")
        self.assertTrue(report.successful)


if __name__ == "__main__":
    unittest.main()
