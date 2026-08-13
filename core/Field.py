from dataclasses import dataclass
from typing import Generic, TypeVar


T = TypeVar("T")


@dataclass(frozen=True, slots=True, eq=False)
class Field(Generic[T]):
    """An identity-based, typed key for lazily resolved Context data."""

    name: str

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("field name cannot be empty")

    def __repr__(self) -> str:
        return f"Field({self.name!r})"
