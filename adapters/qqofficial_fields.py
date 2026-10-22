"""Synchronous, lazy views over unmodified QQ gateway payloads."""

from collections.abc import Callable
from typing import Any

from core import Bot, Field, ProviderContext, ProviderHandle
from fields import GROUP_ID, MESSAGE_ID, MESSAGE_TYPE, SELF_ID, TEXT, USER_ID


MESSAGE_EVENTS = {
    "GROUP_AT_MESSAGE_CREATE": "group",
    "GROUP_MESSAGE_CREATE": "group",
    "C2C_MESSAGE_CREATE": "private",
    "AT_MESSAGE_CREATE": "channel",
    "MESSAGE_CREATE": "channel",
    "DIRECT_MESSAGE_CREATE": "direct",
}
NOTICE_EVENTS = frozenset({
    "GROUP_ADD_ROBOT", "GROUP_DEL_ROBOT", "GROUP_MSG_REJECT", "GROUP_MSG_RECEIVE",
    "FRIEND_ADD", "FRIEND_DEL", "C2C_MSG_REJECT", "C2C_MSG_RECEIVE",
    "GUILD_CREATE", "GUILD_UPDATE", "GUILD_DELETE",
    "CHANNEL_CREATE", "CHANNEL_UPDATE", "CHANNEL_DELETE",
    "GUILD_MEMBER_ADD", "GUILD_MEMBER_UPDATE", "GUILD_MEMBER_REMOVE",
    "MESSAGE_DELETE", "PUBLIC_MESSAGE_DELETE", "DIRECT_MESSAGE_DELETE",
    "MESSAGE_REACTION_ADD", "MESSAGE_REACTION_REMOVE",
    "MESSAGE_AUDIT_PASS", "MESSAGE_AUDIT_REJECT",
})
REPLY_EVENTS = frozenset({
    "GROUP_ADD_ROBOT", "GROUP_MSG_RECEIVE", "FRIEND_ADD", "C2C_MSG_RECEIVE",
    "INTERACTION_CREATE",
})
CHANNEL_ID = Field[str | None]("qqofficial.channel_id")
GUILD_ID = Field[str | None]("qqofficial.guild_id")
EVENT_ID = Field[str | None]("qqofficial.event_id")
ATTACHMENTS = Field[list]("qqofficial.attachments")
_USER_KEYS = {"group": "member_openid", "private": "user_openid"}
_TARGET_KEYS = {"group": "group_openid", "channel": "channel_id", "direct": "guild_id"}
_INTERACTION_SCENES = {"group": "group", "c2c": "private", "guild": "channel"}
_INTERACTION_TYPES = {0: "channel", 1: "group", 2: "private"}


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


def event_scene(raw: dict[str, Any]) -> str | None:
    event = raw.get("t")
    if event in MESSAGE_EVENTS:
        return MESSAGE_EVENTS[event]
    if event in NOTICE_EVENTS:
        if event.startswith("GROUP_"):
            return "group"
        if event.startswith(("FRIEND_", "C2C_")):
            return "private"
    if event == "INTERACTION_CREATE":
        data = event_data(raw)
        scene = _INTERACTION_SCENES.get(data.get("scene"))
        return scene or _INTERACTION_TYPES.get(data.get("chat_type"))
    return None


def event_user(raw: dict[str, Any]) -> str | None:
    data = event_data(raw)
    scene = event_scene(raw)
    author = data.get("author")
    if raw.get("t") in MESSAGE_EVENTS and isinstance(author, dict):
        key = _USER_KEYS.get(scene, "id")
        return _string(author.get(key))
    if scene == "group":
        return _string(data.get("group_member_openid") or data.get("op_member_openid"))
    if scene == "private":
        return _string(data.get("user_openid") or data.get("openid"))
    if raw.get("t") == "INTERACTION_CREATE":
        interaction = data.get("data")
        resolved = interaction.get("resolved") if isinstance(interaction, dict) else None
        return _string(resolved.get("user_id")) if isinstance(resolved, dict) else None
    return None


def event_target(raw: dict[str, Any]) -> tuple[str | None, str | None]:
    scene = event_scene(raw)
    data = event_data(raw)
    key = _TARGET_KEYS.get(scene)
    return scene, _string(data.get(key)) if key else event_user(raw)


def event_id(raw: dict[str, Any]) -> str | None:
    return _string(raw.get("id")) or (
        _string(event_data(raw).get("id")) if raw.get("t") == "INTERACTION_CREATE" else None
    )


def _text(ctx: ProviderContext) -> str:
    text = event_data(ctx.raw).get("content")
    if not isinstance(text, str):
        return ""
    text = text.strip()
    if ctx.raw.get("t") == "AT_MESSAGE_CREATE":
        self_id = ctx.resolve(SELF_ID)
        if self_id:
            for prefix in (f"<@!{self_id}>", f"<@{self_id}>"):
                if text.startswith(prefix):
                    return text[len(prefix):].strip()
    return text


def _message_type(ctx: ProviderContext) -> str | None:
    return event_scene(ctx.raw)


def _user_id(ctx: ProviderContext) -> str | None:
    return event_user(ctx.raw)


def _group_id(ctx: ProviderContext) -> str | None:
    return _string(event_data(ctx.raw).get("group_openid"))


def _message_id(ctx: ProviderContext) -> str | None:
    return _string(event_data(ctx.raw).get("id")) if ctx.raw.get("t") in MESSAGE_EVENTS else None


def _attachments(ctx: ProviderContext) -> list:
    attachments = event_data(ctx.raw).get("attachments")
    return attachments if isinstance(attachments, list) else []


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
        (CHANNEL_ID, lambda ctx: _string(event_data(ctx.raw).get("channel_id"))),
        (GUILD_ID, lambda ctx: _string(event_data(ctx.raw).get("guild_id"))),
        (EVENT_ID, lambda ctx: event_id(ctx.raw)), (ATTACHMENTS, _attachments),
    )
    handles: list[ProviderHandle] = []
    try:
        for field, provider in declarations:
            handles.append(scope.provide(field, provider, platform=platform,
                                         requires=(SELF_ID,) if field is TEXT else ()))
    except BaseException:
        for handle in handles:
            handle.revoke()
        raise
    return tuple(handles)
