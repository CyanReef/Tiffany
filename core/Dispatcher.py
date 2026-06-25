"""
Dispatcher 负责把一条 Context 分发给所有 hook。

它是框架的交通调度员：
消息来了以后，按优先级依次问每个 hook：你要不要处理？处理完是否停止？

Dispatcher 不负责解析消息，也不负责具体业务。
这样内核会很薄，功能可以通过 hook 自然扩展。
"""

from .Context import Context
from .Hook import Hook


class Dispatcher:
    """
    Hook 调度器。

    它维护一个 hook 列表，并负责按顺序执行这些 hook。
    """

    def __init__(self):
        """
        初始化调度器。

        为什么 hooks 用 list？
            第一版最简单，按优先级排序后顺序执行即可。
            以后如果 hook 数量非常多，再考虑按事件类型建立索引。
        """

        self.hooks: list[Hook] = []

    def add(self, hook: Hook):
        """
        注册一个 hook。

        为什么每次 add 后都排序？
            这样 dispatch 时不用再排序，消息热路径更简单。
            当前 hook 数量通常不会很多，所以注册阶段排序成本可以接受。
        """

        self.hooks.append(hook)
        self.hooks.sort(key=lambda h: h.priority, reverse=True)

    async def dispatch(self, ctx: Context):
        """
        将一条消息上下文交给 hook 链处理。

        执行顺序：
        1. 如果 ctx.stopped，停止调度。
        2. 如果 hook 有 when，先执行 when 判断。
        3. when 通过后执行 hook.handle。

        为什么不在这里主动解析 text？
            Dispatcher 只负责调度，不应该替 hook 决定需要哪些字段。
            如果某个 hook 不调用 ctx.text()，文本就不会被解析。
        """

        for hook in self.hooks:
            if ctx.stopped:
                break

            if hook.when is not None:
                ok = await hook.when(ctx)
                if not ok:
                    continue

            await hook.handle(ctx)
