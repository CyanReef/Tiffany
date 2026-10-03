"""Run the QQ example without changing the default OneBot configuration."""

import asyncio
import logging
from pathlib import Path

from adapters import create_adapter
from core import Bot
from hooks import register_hooks
from settings import load_config


async def main() -> None:
    config = load_config(Path(__file__).with_name("Tiffany.qqofficial.toml"))
    bot = Bot()
    register_hooks(bot)
    bot.install(create_adapter(config.adapter))
    await bot.run_async()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(main())
