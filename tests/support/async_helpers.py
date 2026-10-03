"""Shared synchronous runners for event-dispatch tests."""

import asyncio


def run(awaitable):
    return asyncio.run(awaitable)


async def emit_running(bot, envelope):
    async with bot:
        return await bot.emit(envelope)
