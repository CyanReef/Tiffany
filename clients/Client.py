from typing import Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from core import Context


class Client(Protocol):
    async def reply_text(self, ctx: "Context", text: str) -> None:
        ...
