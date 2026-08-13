from typing import Protocol, TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core import Context


class Client(Protocol):
    async def setup(self, runtime: object) -> None:
        ...

    async def start(self) -> None:
        ...

    async def stop(self, mode: str = "drain") -> None:
        ...

    async def teardown(self) -> None:
        ...

    async def reply(self, ctx: "Context", text: str) -> Any:
        ...

    async def reply_text(self, ctx: "Context", text: str) -> Any:
        ...
