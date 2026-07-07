from .Client import Client


def create_client(client_type: str, **kwargs) -> Client:
    if client_type == "onebot_websocket":
        from .OneBotWebSocketClient import OneBotWebSocketClient

        return OneBotWebSocketClient(**kwargs)

    raise ValueError(f"unknown client type: {client_type}")


__all__ = ["Client", "create_client"]
