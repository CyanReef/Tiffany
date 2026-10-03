"""协议 Provider 与业务共用的字段键；提供者和消费者复用同一实例。"""

from core import Field


TEXT = Field[str]("message.text")
MESSAGE_TYPE = Field[str | None]("message.type")
# IDs retain the platform's native type. QQ OpenIDs are opaque strings,
# scoped to an application and message scene, not numeric QQ accounts.
USER_ID = Field[int | str | None]("message.user_id")
GROUP_ID = Field[int | str | None]("message.group_id")
MESSAGE_ID = Field[int | str | None]("message.id")
SELF_ID = Field[int | str | None]("bot.self_id")


__all__ = [
    "GROUP_ID",
    "MESSAGE_ID",
    "MESSAGE_TYPE",
    "SELF_ID",
    "TEXT",
    "USER_ID",
]
