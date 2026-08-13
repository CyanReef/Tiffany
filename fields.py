from core import Field


TEXT = Field[str]("message.text")
MESSAGE_TYPE = Field[str | None]("message.type")
USER_ID = Field[int | None]("message.user_id")
GROUP_ID = Field[int | None]("message.group_id")
MESSAGE_ID = Field[int | None]("message.id")
SELF_ID = Field[int | None]("bot.self_id")


__all__ = [
    "GROUP_ID",
    "MESSAGE_ID",
    "MESSAGE_TYPE",
    "SELF_ID",
    "TEXT",
    "USER_ID",
]
