"""Standalone commit-before-ack notes; no account or network needed."""
import asyncio
from pathlib import Path
import tempfile

from core import APP_PATHS, Bot, Envelope
from fields import MESSAGE_TYPE, SELF_ID, TEXT, USER_ID
from hooks import install_extensions
from shared.extensions import ExtensionsConfig
from shared.paths import RuntimePaths


class ConsoleClient:
    async def reply(self, ctx, text):
        print(text)


async def main():
    with tempfile.TemporaryDirectory() as directory:
        bot = Bot()
        bot.service(APP_PATHS, RuntimePaths(Path(directory)))
        for key, name in ((TEXT, "text"), (USER_ID, "user"), (SELF_ID, "self"), (MESSAGE_TYPE, "scene")):
            bot.provide(key, lambda ctx, name=name: ctx.raw[name])
        await install_extensions(bot, ExtensionsConfig.parse({"enabled": ["commands", "ping", "help", "notes"]}))
        async with bot:
            for text in ("ping", "/help", "/note set hello Tiffany", "/note get", "/note delete"):
                await bot.emit(Envelope("demo", {"text": text, "user": 1, "self": 99, "scene": "private"},
                    client=ConsoleClient(), kind="message", session_id="demo:user:1"))


if __name__ == "__main__":
    asyncio.run(main())
