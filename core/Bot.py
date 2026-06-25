"""
Bot 是框架对外暴露的门面。

用户通常不需要直接操作 Dispatcher、Hook、Context。
Bot 把这些底层对象包装成更好用的 API，比如 @bot.hook(...) 和 bot.emit(...)
"""

from .Context import Context
from .Dispatcher import Dispatcher
from .Envelope import Envelope
from .Hook import Handler, Hook, Predicate


class Bot:
    """
    机器人内核入口。

    你可以把 Bot 理解成“hook runtime”：
    它本身不懂 QQ、不懂命令、不懂 AI，只负责收集 hook，并在消息进来时运行它们。
    """

    def __init__(self):
        """
        创建 Bot。

        为什么 Bot 里面只有 Dispatcher？
            第一版内核要保持极小。Bot 只负责提供好用的外壳，真正执行 hook 的工作交给 Dispatcher。
        """

        self.dispatcher = Dispatcher()

    def hook(
        self,
        *,
        name: str | None = None,
        on: str | None = None,
        needs: tuple[str, ...] = (),
        priority: int = 0,
        when: Predicate | None = None,
    ):
        """
        注册一个 hook 的装饰器。

        用法示例：
            @bot.hook(needs=("text",))
            async def print_text(ctx):
                print(await ctx.text())

        为什么用装饰器？
            Python 用户很熟悉 @xxx 这种写法，写业务 hook 会很自然。
            函数定义在哪里，hook 注册就在哪里，代码也更直观。

        为什么这里不直接接收 func，而是返回 decorator？
            因为 @bot.hook(...) 先接收配置参数，再接收被装饰的函数。
            所以需要两层函数：外层收配置，内层收真正的 handler。
        """

        def decorator(func: Handler):
            """
            接收用户写的异步函数，并把它包装成 Hook 注册进 Dispatcher。

            name or func.__name__:
                如果用户没手动传名字，就用函数名当 hook 名字，方便调试。
            """

            self.dispatcher.add(Hook(
                name=name or func.__name__,
                on=on,
                needs=needs,
                priority=priority,
                when=when,
                handle=func,
            ))
            return func

        return decorator

    async def emit(self, envelope: Envelope):
        """
        把一条外部消息送进内核。

        emit 是 Adapter 和 Core 的边界：
        socket/http/webhook 这些接入层拿到 raw 消息后，包装成 Envelope，然后调用 bot.emit()。

        为什么这里创建 Context？
            Envelope 是原始消息容器，Context 是 hook 使用的懒解析视图。
            每条消息都应该有自己的 Context 和 cache，不能跨消息共享。
        """

        ctx = Context(envelope)
        await self.dispatcher.dispatch(ctx)
