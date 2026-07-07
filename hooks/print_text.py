from core import Bot


def register(bot: Bot) -> None:
    @bot.hook(needs=("text",))
    async def print_text(ctx):
        """
        一个最小示例 hook：打印文本内容，并把同一段文本回复回去。

        为什么这里调用 await ctx.text()？
            因为 text 是懒解析字段。只有这个 hook 真的要看文本时，Context 才会从 raw 里提取文本。

        为什么调用 await ctx.reply(text)？
            hook 不直接操作 OneBot action，而是通过 Context 委托给当前消息绑定的 client。
        """

        text = await ctx.text()
        if text:
            print("TEXT:", text)
            await ctx.reply(text)
