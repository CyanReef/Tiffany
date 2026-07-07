from core import Bot
from settings import AdapterConfig, load_config
from .Adapter import Adapter


def create_adapter(bot: Bot, config: AdapterConfig | None = None) -> Adapter:
    if config is None:
        config = load_config().adapter

    if config.type == "onebot_websocket":
        from .OneBotWebSocketAdapter import OneBotWebSocketAdapter

        return OneBotWebSocketAdapter(
            bot=bot,
            host=config.websocket.host,
            port=config.websocket.port,
            platform=config.platform,
        )

    raise ValueError(f"unknown adapter type: {config.type}")


async def run_adapter(bot: Bot) -> None:
    adapter = create_adapter(bot)
    await adapter.run()


__all__ = ["Adapter", "create_adapter", "run_adapter"]
