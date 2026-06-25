"""
Context 是 hook 看到的“消息视图”。

它不是传统框架里的完整 Event 对象。传统 Event 往往会提前解析文本、图片、用户、群、
引用消息等所有字段；Context 的设计目标正好相反：谁需要，谁触发解析。

这就是这个框架的核心思想：
    不为没有被关注的信息付出解析成本。
"""

from .Envelope import Envelope


class Context:
    """
    单条消息在框架内部流动时的上下文对象。

    一个 Context 对应一个 Envelope，也就是一条外部原始消息。
    所有 hook 都拿同一个 ctx，所以解析结果可以在 hook 之间共享。

    主要职责：
    1. 暴露 raw，让你随时能看到平台原始消息。
    2. 提供 text()、event_type() 这类按需解析方法。
    3. 缓存解析结果，避免多个 hook 重复解析同一个字段。
    4. 提供 stop()，让某个 hook 命中后可以中断后续 hook。
    """

    def __init__(self, envelope: Envelope):
        """
        创建上下文。

        为什么只传 Envelope？
            Context 不应该关心消息是从 socket 来的，还是从 HTTP 来的。
            它只关心“这里有一封原始消息信封”。

        cache 为什么是 dict？
            懒解析需要缓存。比如两个 hook 都调用 await ctx.text()，
            第一次会解析 raw，第二次直接从 cache 取，避免重复工作。

        stopped 为什么放在 ctx 上？
            是否中断后续 hook 是这条消息自己的状态，放在 Context 上最自然。
        """

        self.envelope = envelope
        self.cache = {}
        self.stopped = False

    @property
    def raw(self):
        """
        返回原始消息。

        为什么提供 raw？
            轻量框架不能把所有平台字段都抽象掉。NapCat/OneBot 有自己的特殊字段，
            如果框架暂时没有封装，用户仍然可以通过 ctx.raw 直接拿。
        """

        return self.envelope.raw

    def stop(self):
        """
        停止后续 hook 继续处理这条消息。

        为什么需要 stop？
            比如 /ping 已经被某个 hook 处理了，后面的通用聊天 hook 就没必要再运行。
            这既能减少无意义计算，也能避免多个 hook 对同一条消息重复响应。
        """

        self.stopped = True

    async def get(self, key, parser):
        """
        通用的“懒解析 + 缓存”工具方法。

        key:
            缓存字段名，比如 "text"、"event_type"。

        parser:
            真正解析字段的异步函数。只有缓存里没有 key 时才会执行。

        为什么 parser 设计成 async？
            现在解析 text 只是读 dict，不需要 await；但以后某些字段可能需要异步操作，
            比如下载图片、查数据库、调用平台 API。提前把接口设计成 async，后面不用大改。
        """

        if key not in self.cache:
            self.cache[key] = await parser()
        return self.cache[key]

    async def event_type(self):
        """
        获取事件大类，比如 OneBot/NapCat 里的 post_type。

        为什么不在 Context 初始化时就解析？
            因为不是每个 hook 都关心事件类型。只有真的调用 event_type() 时才解析，
            这样保持“按需付费”的设计。
        """

        async def parse():
            return self.raw.get("post_type")

        return await self.get("event_type", parse)

    async def message_type(self):
        """
        获取消息类型，比如 group/private。

        这个字段也是按需解析，并通过 get() 缓存。
        后续如果要做群聊 hook、私聊 hook，可以基于它做过滤。
        """

        async def parse():
            return self.raw.get("message_type")

        return await self.get("message_type", parse)

    async def text(self):
        """
        获取纯文本内容。

        NapCat 的 message 可能有两种常见形态：
        1. 字符串："hello"
        2. 消息段数组：[ {"type": "text", "data": {"text": "hello"}} ]

        为什么这里只提取 text 段？
            因为这个方法的名字叫 text()，它只应该关心文本。
            图片、@、表情等字段以后可以分别做 images()、mentions() 等懒解析方法。

        为什么放在 Context 里？
            hook 写业务时不应该到处写 raw["message"] 的兼容逻辑。
            把平台消息形态的细节收在 Context 里，hook 会更干净。
        """

        async def parse():
            message = self.raw.get("message", "")

            if isinstance(message, str):
                return message

            if isinstance(message, list):
                return "".join(
                    seg.get("data", {}).get("text", "")
                    for seg in message
                    if seg.get("type") == "text"
                )

            return ""

        return await self.get("text", parse)
