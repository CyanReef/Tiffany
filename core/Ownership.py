from __future__ import annotations

from collections.abc import Iterable


def same_owner(left: object, right: object) -> bool:
    """Compare ownership tokens without invoking arbitrary equality methods."""

    return left is right or (
        isinstance(left, str)
        and isinstance(right, str)
        and left == right
    )


class OwnerKey:
    """Hashable identity key which also preserves value semantics for names."""

    __slots__ = ("owner", "_hash")

    def __init__(self, owner: object) -> None:
        self.owner = owner
        self._hash = hash((str, owner)) if isinstance(owner, str) else id(owner)

    def __hash__(self) -> int:
        return self._hash

    def __eq__(self, other: object) -> bool:
        return isinstance(other, OwnerKey) and same_owner(self.owner, other.owner)


def unique_owners(owners: Iterable[object]) -> tuple[object, ...]:
    seen: set[OwnerKey] = set()
    result: list[object] = []
    for owner in owners:
        key = OwnerKey(owner)
        if key in seen:
            continue
        seen.add(key)
        result.append(owner)
    return tuple(result)
