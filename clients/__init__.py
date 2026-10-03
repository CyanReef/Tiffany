"""外部动作调用层：导出客户端、调用结果、错误类型和工厂。"""

from .Client import Client
from .OneBotWebSocketClient import (
    OneBotActionError,
    OneBotCallResult,
    OneBotCallTimeoutError,
    OneBotClientError,
    OneBotClientNotRunningError,
    OneBotConnectionLostError,
    OneBotDisconnectedError,
    OneBotPendingLimitError,
    OneBotProtocolError,
    OneBotResponseError,
    OneBotSendError,
    OneBotWebSocketClient,
    PendingLimitExceeded,
    SendMessageResult,
)


def create_client(client_type: str, **kwargs) -> Client:
    if client_type == "onebot_websocket":
        return OneBotWebSocketClient(**kwargs)

    if client_type == "qqofficial":
        from .QQOfficialClient import QQOfficialClient

        return QQOfficialClient(**kwargs)

    raise ValueError(f"unknown client type: {client_type}")


__all__ = [
    "Client",
    "OneBotActionError",
    "OneBotCallResult",
    "OneBotCallTimeoutError",
    "OneBotClientError",
    "OneBotClientNotRunningError",
    "OneBotConnectionLostError",
    "OneBotDisconnectedError",
    "OneBotPendingLimitError",
    "OneBotProtocolError",
    "OneBotResponseError",
    "OneBotSendError",
    "OneBotWebSocketClient",
    "PendingLimitExceeded",
    "SendMessageResult",
    "create_client",
]
