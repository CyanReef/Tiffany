"""Synchronous, lazy views over unmodified QQ gateway payloads."""

from collections.abc import Callable
from typing import Any

from core import Bot, ProviderContext, ProviderHandle
from fields import GROUP_ID, MESSAGE_ID, MESSAGE_TYPE, SELF_ID, TEXT, USER_ID


MESSAGE_EVENTS = {
    "GROUP_AT_MESSAGE_CREATE": "group",
    "C2C_MESSAGE_CREATE": "private",
}
NOTICE_EVENTS = frozenset({
    "GROUP_ADD_ROBOT", "GROUP_DEL_ROBOT", "GROUP_MSG_REJECT", "GROUP_MSG_RECEIVE",
    "FRIEND_ADD", "FRIEND_DEL", "C2C_MSG_REJECT", "C2C_MSG_RECEIVE",
})


def event_data(raw: dict[str, Any]) -> dict[str, Any]:
    data = raw.get("d")
    return data if isinstance(data, dict) else {}


def detect_event_kind(raw: dict[str, Any]) -> str:
    event = raw.get("t")
    if event in MESSAGE_EVENTS:
        return "message"
    return "notice" if event in NOTICE_EVENTS else "event"


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _text(ctx: ProviderContext) -> str:
    text = event_data(ctx.raw).get("content")
    return text.strip() if isinstance(text, str) else ""


def _message_type(ctx: ProviderContext) -> str | None:
    return MESSAGE_EVENTS.get(ctx.raw.get("t"))


def _user_id(ctx: ProviderContext) -> str | None:
    author = event_data(ctx.raw).get("author")
    if not isinstance(author, dict):
        return None
    key = "member_openid" if _message_type(ctx) == "group" else "user_openid"
    return _string(author.get(key))


def _group_id(ctx: ProviderContext) -> str | None:
    return _string(event_data(ctx.raw).get("group_openid"))


def _message_id(ctx: ProviderContext) -> str | None:
    return _string(event_data(ctx.raw).get("id"))


def register_qqofficial_fields(
    bot: Bot, platform: str, self_id: Callable[[], str | None],
) -> tuple[ProviderHandle, ...]:
    """Return owned handles so adapter teardown can revoke its declarations."""

    scope = bot.scope("protocol.qqofficial")
    if any(handle.platform == platform for handle in bot.providers.handles_for_owner(scope.owner)):
        raise ValueError("only one QQ official account per platform is supported")
    declarations = (
        (TEXT, _text), (MESSAGE_TYPE, _message_type), (USER_ID, _user_id),
        (GROUP_ID, _group_id), (MESSAGE_ID, _message_id),
        (SELF_ID, lambda ctx: self_id()),
    )
    handles: list[ProviderHandle] = []
    try:
        for field, provider in declarations:
            handles.append(scope.provide(field, provider, platform=platform))
    except BaseException:
        for handle in handles:
            handle.revoke()
        raise
    return tuple(handles)
