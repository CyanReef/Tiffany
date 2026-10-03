from typing import Any

from core import Bot, ProviderContext
from fields import GROUP_ID, MESSAGE_ID, MESSAGE_TYPE, SELF_ID, TEXT, USER_ID


def detect_event_kind(raw: dict[str, Any]) -> str:
    """Perform only the routing probe required by the runtime."""

    post_type = raw.get("post_type")
    if isinstance(post_type, str):
        return post_type
    if "status" in raw and "retcode" in raw:
        return "response"
    return "event"


def _text(ctx: ProviderContext) -> str:
    message = ctx.raw.get("message", "")
    if isinstance(message, str):
        return message
    if not isinstance(message, list):
        return ""

    parts: list[str] = []
    for segment in message:
        if not isinstance(segment, dict) or segment.get("type") != "text":
            continue
        data = segment.get("data")
        if isinstance(data, dict):
            text = data.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def _message_type(ctx: ProviderContext) -> str | None:
    return ctx.raw.get("message_type")


def _user_id(ctx: ProviderContext) -> int | None:
    return ctx.raw.get("user_id")


def _group_id(ctx: ProviderContext) -> int | None:
    return ctx.raw.get("group_id")


def _message_id(ctx: ProviderContext) -> int | None:
    return ctx.raw.get("message_id")


def _self_id(ctx: ProviderContext) -> int | None:
    return ctx.raw.get("self_id")


def register_onebot_fields(bot: Bot, platform: str) -> tuple:
    scope = bot.scope("protocol.onebot11")
    declarations = (
        (TEXT, _text), (MESSAGE_TYPE, _message_type), (USER_ID, _user_id),
        (GROUP_ID, _group_id), (MESSAGE_ID, _message_id), (SELF_ID, _self_id),
    )
    prior_ids = {handle.id for handle in bot.providers.registrations()}
    handles = []
    try:
        for field, provider in declarations:
            handles.append(scope.provide(field, provider, platform=platform))
    except BaseException:
        for handle in handles:
            if handle.id not in prior_ids:
                handle.revoke()
        raise
    return tuple(handles)


__all__ = ["detect_event_kind", "register_onebot_fields"]
