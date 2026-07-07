import asyncio

from adapters import run_adapter
from core import Bot
from hooks import register_hooks


class TiffanyApplication:
    @staticmethod
    def run() -> None:
        asyncio.run(TiffanyApplication._run())

    @staticmethod
    async def _run() -> None:
        bot = TiffanyApplication._create_bot()
        await run_adapter(bot)

    @staticmethod
    def _create_bot() -> Bot:
        bot = Bot()
        register_hooks(bot)
        return bot
