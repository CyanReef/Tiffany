import asyncio
import logging

from adapters import create_adapter
from core import Bot
from hooks import register_hooks
from settings import load_config


class TiffanyApplication:
    @staticmethod
    def run() -> None:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        asyncio.run(TiffanyApplication._run())

    @staticmethod
    async def _run() -> None:
        bot = TiffanyApplication._create_bot()
        bot.install(create_adapter(load_config().adapter))
        await bot.run_async()

    @staticmethod
    def _create_bot() -> Bot:
        bot = Bot()
        register_hooks(bot)
        return bot
