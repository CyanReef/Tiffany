from typing import Literal, Protocol


class Adapter(Protocol):
    async def setup(self, runtime: object) -> None:
        ...

    async def start(self) -> None:
        ...

    async def stop(self, mode: Literal["drain", "abort"] = "drain") -> None:
        ...

    async def teardown(self) -> None:
        ...
