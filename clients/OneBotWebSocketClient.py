import json
from typing import Any


class OneBotWebSocketClient:
    def __init__(self, ws):
        self.ws = ws

    async def reply_text(self, ctx, text: str) -> None:
        await self.send_text(ctx.raw, text)

    async def send_text(self, raw: dict[str, Any], text: str) -> None:
        params: dict[str, Any] = {
            "message_type": raw.get("message_type"),
            "message": text,
        }

        if raw.get("message_type") == "group":
            params["group_id"] = raw.get("group_id")
        elif raw.get("message_type") == "private":
            params["user_id"] = raw.get("user_id")
        else:
            raise ValueError(f"unsupported message_type: {raw.get('message_type')}")

        await self.call("send_msg", params)

    async def call(self, action: str, params: dict[str, Any]) -> None:
        payload = {
            "action": action,
            "params": params,
        }
        await self.ws.send(json.dumps(payload, ensure_ascii=False))
