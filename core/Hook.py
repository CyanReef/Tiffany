"""
Hook 是框架里的最小功能单元。

在这个设计里，命令、权限、日志、AI 回复、群管理，未来都可以是 hook。
框架核心不需要知道“什么是命令”，它只需要知道“有一批 hook 要按顺序执行”。
"""

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal, TypeAlias

from .Context import Context
from .Field import Field


# Handler 表示真正处理消息的函数。
# 它接收 Context，并且是异步的，因为机器人业务经常要 await：发消息、查库、调 API。
Handler = Callable[[Context], Awaitable[None]]

# Predicate 表示 hook 的判断函数。纯判断可以同步执行，需要 I/O 时也可异步。
Predicate = Callable[[Context], bool | Awaitable[bool]]
HookErrorPolicy: TypeAlias = Literal["abort", "continue"]


@dataclass(frozen=True, slots=True)
class Hook:
    """
    一个 hook 的声明信息。

    name:
        hook 名字，用于日志、调试、报错定位。不要小看名字，hook 多了以后很救命。

    handle:
        真正执行业务逻辑的异步函数。

    on:
        adapter 提供的事件路由类型，例如 message、notice、request。
        Dispatcher 会在 when 和字段解析之前过滤不匹配的 hook。

    needs:
        声明 handle 可能读取的 Field，用于启动时校验 provider 依赖图。
        声明本身不会触发解析；只有 ctx.resolve(field) 才会产生解析成本。

    priority:
        优先级。数字越大越先执行。
        例如权限/限流 hook 通常应该比普通业务 hook 更早运行。

    when:
        可选判断条件。返回 False 时，handle 不会执行。
        这样可以把“是否关心这条消息”和“真正处理消息”分开。

    platform:
        可选平台范围。未设置时 hook 可运行于所有平台。

    on_error:
        abort 会终止当前事件；continue 会记录错误并继续后续 hook。

    timeout:
        可选执行时限（秒），覆盖 when 和 handle 的总执行时间。

    on_timeout:
        超时时的独立策略。未设置时继承 on_error。

    uses:
        Hook 声明使用的扩展能力或 service key。它只是元数据，
        Dispatcher 不会因此触发解析或 I/O。
    """

    name: str
    handle: Handler
    on: str | None = None
    needs: tuple[Field[Any], ...] = ()
    priority: int = 0
    when: Predicate | None = None
    platform: str | None = None
    on_error: HookErrorPolicy = "abort"
    timeout: float | None = None
    on_timeout: HookErrorPolicy | None = None
    uses: tuple[Any, ...] = ()
    owner: object = "application"
    source: str | None = None

    def __post_init__(self) -> None:
        if self.on_error not in ("abort", "continue"):
            raise ValueError(f"unknown hook error policy: {self.on_error!r}")
        if self.on_timeout not in (None, "abort", "continue"):
            raise ValueError(
                f"unknown hook timeout policy: {self.on_timeout!r}"
            )
        if self.timeout is not None and self.timeout <= 0:
            raise ValueError("hook timeout must be greater than zero")
        object.__setattr__(self, "needs", tuple(self.needs))
        object.__setattr__(self, "uses", tuple(self.uses))
