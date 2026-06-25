"""
项目入口文件。

现在它只做最小的事情：
1. 在本机 127.0.0.1:6199 开 WebSocket 服务端。
2. 等待客户端连接
3. 收到客户端发来的原始 JSON 字符串。
4. 打印 RAW，然后包装成 Envelope 送进 Bot 内核。

注意：main.py 不负责解析消息，也不负责业务逻辑。
这些工作应该放到 core.Context 和 hook 里。
"""

import asyncio
import json

from websockets.asyncio.server import serve

from core import Bot, Envelope


HOST = "127.0.0.1"
PORT = 6199

# Bot 是框架内核的门面。这里创建一个全局 bot，后面用它注册 hook。
bot = Bot()


@bot.hook(needs=("text",))
async def print_text(ctx):
    """
    一个最小示例 hook：打印文本内容。

    为什么这里调用 await ctx.text()？
        因为 text 是懒解析字段。只有这个 hook 真的要看文本时，Context 才会从 raw 里提取文本。

    为什么 needs 写 ("text",)？
        现在 needs 还没有参与调度优化，但它表达了这个 hook 的意图：我关心文本。
        以后做调试、性能统计、预筛选时，这个声明会很有价值。
    """

    text = await ctx.text()
    if text != "" and text is not None:
        print("TEXT:", text)


async def handler(ws):
    """
    WebSocket 连接处理函数。

    客户端 连上来以后，websockets 库会为这条连接调用 handler。
    async for message in ws 表示不断等待对方发来的消息。

    为什么这里不直接解析业务？
        入口层只负责接线：socket -> raw -> Envelope -> bot.emit。
        这样以后把 客户端 换成别的平台时，内核不用改。
    """

    print("客户端 connected")

    async for message in ws:
        print("RAW:", message)

        raw = json.loads(message)
        await bot.emit(Envelope(platform="napcat", raw=raw))


async def main():
    """
    启动 WebSocket 服务端。

    async with serve(...):
        启动服务，并在退出这个代码块时自动关闭服务。

    await asyncio.Future():
        创建一个永远不会完成的 Future，让程序一直运行。
        否则服务刚启动，main 函数就结束了，程序也会退出。
    """

    async with serve(handler, HOST, PORT):
        print(f"listening ws://{HOST}:{PORT}/ws")
        await asyncio.Future()


# asyncio.run 会启动事件循环，用来运行 async/await 程序。
asyncio.run(main())
