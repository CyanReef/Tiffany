from core import Bot


def register(bot: Bot) -> None:
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
