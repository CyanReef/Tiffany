import asyncio
from websockets.asyncio.server import serve

HOST = "127.0.0.1"
PORT = 6199


async def handler(ws):
    print("NapCat connected")

    async for message in ws:
        print(message)


async def main():
    async with serve(handler, HOST, PORT):
        print(f"listening ws://{HOST}:{PORT}/ws")
        await asyncio.Future()


asyncio.run(main())