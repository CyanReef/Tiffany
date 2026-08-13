from typing import Any

from core import Bot, Context
from fields import GROUP_ID, MESSAGE_ID, MESSAGE_TYPE, SELF_ID, TEXT, USER_ID


def detect_event_kind(raw: dict[str, Any]) -> str:
    """Perform only the routing probe required by the runtime."""

    post_type = raw.get("post_type")
    if isinstance(post_type, str):
        return post_type
    if "status" in raw and "retcode" in raw:
        return "response"
    return "event"


def _text(ctx: Context) -> str:
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


def _message_type(ctx: Context) -> str | None:
    return ctx.raw.get("message_type")


def _user_id(ctx: Context) -> int | None:
    return ctx.raw.get("user_id")


def _group_id(ctx: Context) -> int | None:
    return ctx.raw.get("group_id")


def _message_id(ctx: Context) -> int | None:
    return ctx.raw.get("message_id")


def _self_id(ctx: Context) -> int | None:
    return ctx.raw.get("self_id")


def register_onebot_fields(bot: Bot, platform: str) -> None:
    scope = bot.scope("protocol.onebot11")
    scope.provide(TEXT, _text, platform=platform)
    scope.provide(MESSAGE_TYPE, _message_type, platform=platform)
    scope.provide(USER_ID, _user_id, platform=platform)
    scope.provide(GROUP_ID, _group_id, platform=platform)
    scope.provide(MESSAGE_ID, _message_id, platform=platform)
    scope.provide(SELF_ID, _self_id, platform=platform)


__all__ = ["detect_event_kind", "register_onebot_fields"]
