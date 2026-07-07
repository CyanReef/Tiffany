import asyncio
import json

from clients import create_client
from websockets.asyncio.server import serve

from core import Bot, Envelope


class OneBotWebSocketAdapter:
    def __init__(self, bot: Bot, host: str, port: int, platform: str):
        self.bot = bot
        self.host = host
        self.port = port
        self.platform = platform

    async def run(self) -> None:
        async with serve(self.handle, self.host, self.port):
            print(f"listening ws://{self.host}:{self.port}/ws")
            await asyncio.Future()

    async def handle(self, ws) -> None:
        print("客户端 connected")
        client = create_client("onebot_websocket", ws=ws)

        async for message in ws:
            print("RAW:", message)

            raw = json.loads(message)
            await self.bot.emit(Envelope(
                platform=self.platform,
                raw=raw,
                client=client,
            ))
