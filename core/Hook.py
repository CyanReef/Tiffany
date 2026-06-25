"""
Hook 是框架里的最小功能单元。

在这个设计里，命令、权限、日志、AI 回复、群管理，未来都可以是 hook。
框架核心不需要知道“什么是命令”，它只需要知道“有一批 hook 要按顺序执行”。
"""

from dataclasses import dataclass
from typing import Awaitable, Callable

from .Context import Context


# Handler 表示真正处理消息的函数。
# 它接收 Context，并且是异步的，因为机器人业务经常要 await：发消息、查库、调 API。
Handler = Callable[[Context], Awaitable[None]]

# Predicate 表示 hook 的判断函数。
# 比如“只有文本等于 /ping 时才运行”。它也是异步的，方便以后查权限或查数据库。
Predicate = Callable[[Context], Awaitable[bool]]


@dataclass(slots=True)
class Hook:
    """
    一个 hook 的声明信息。

    name:
        hook 名字，用于日志、调试、报错定位。不要小看名字，hook 多了以后很救命。

    handle:
        真正执行业务逻辑的异步函数。

    on:
        预留的事件类型过滤字段。现在还没用上，后面可以用来区分 message、notice、request。

    needs:
        声明这个 hook 可能关心哪些字段，比如 ("text",)。
        目前只是元信息，未来调度器可以利用它做优化或调试追踪。

    priority:
        优先级。数字越大越先执行。
        例如权限/限流 hook 通常应该比普通业务 hook 更早运行。

    when:
        可选判断条件。返回 False 时，handle 不会执行。
        这样可以把“是否关心这条消息”和“真正处理消息”分开。
    """

    name: str
    handle: Handler
    on: str | None = None
    needs: tuple[str, ...] = ()
    priority: int = 0
    when: Predicate | None = None
