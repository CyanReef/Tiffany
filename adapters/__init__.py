from settings import AdapterConfig, load_config
from .Adapter import Adapter


def create_adapter(
    config: AdapterConfig | None = None,
) -> Adapter:
    """Construct an adapter without registration or I/O side effects."""

    if config is None:
        config = load_config().adapter
    elif not isinstance(config, AdapterConfig):
        raise TypeError("create_adapter expects an AdapterConfig")

    if config.type == "onebot_websocket":
        from .OneBotWebSocketAdapter import OneBotWebSocketAdapter

        adapter = OneBotWebSocketAdapter(
            host=config.websocket.host,
            port=config.websocket.port,
            platform=config.platform,
            workers=config.websocket.workers,
            queue_size=config.websocket.queue_size,
            call_timeout=config.websocket.call_timeout,
            pending_limit=config.websocket.pending_limit,
        )
        return adapter

    raise ValueError(f"unknown adapter type: {config.type}")


async def run_adapter(bot: object) -> None:
    install_adapter(bot, load_config().adapter)
    await bot.run_async()


def install_adapter(bot: object, config: AdapterConfig | None = None) -> Adapter:
    """Compatibility helper which installs a side-effect-free adapter."""

    if config is None:
        config = load_config().adapter
    adapter = create_adapter(config)
    bot.install(adapter)
    return adapter


__all__ = ["Adapter", "create_adapter", "install_adapter", "run_adapter"]
