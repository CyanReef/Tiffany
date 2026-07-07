from typing import Protocol


class Adapter(Protocol):
    async def run(self) -> None:
        ...
