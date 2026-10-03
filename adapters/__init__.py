"""事件接入层的组合入口：根据配置构造和安装协议适配器。"""

from settings import AdapterConfig, CredentialResolver, load_config
from .Adapter import Adapter


def create_adapter(
    config: AdapterConfig | None = None,
    *,
    credential_resolver: CredentialResolver | None = None,
) -> Adapter:
    """Construct without registration or network I/O.

    With no config, read the application config. A credential resolver may
    read local secrets; connections and listeners are created during start.
    """

    if config is None:
        config = load_config().adapter
    elif not isinstance(config, AdapterConfig):
        raise TypeError("create_adapter expects an AdapterConfig")

    if config.type == "onebot_websocket":
        if config.websocket is None:
            raise ValueError("onebot_websocket requires adapter.websocket configuration")
        from .OneBotWebSocketAdapter import OneBotWebSocketAdapter

        adapter = OneBotWebSocketAdapter(
            host=config.websocket.host,
            port=config.websocket.port,
            platform=config.platform,
            workers=config.websocket.workers,
            queue_size=config.websocket.queue_size,
            call_timeout=config.websocket.call_timeout,
            pending_limit=config.websocket.pending_limit,
            token=config.websocket.get_token(config.platform, credential_resolver),
        )
        return adapter

    if config.type == "qqofficial_websocket":
        if config.qqofficial is None:
            raise ValueError("qqofficial_websocket requires adapter.qqofficial configuration")
        qqofficial = config.qqofficial
        secret = qqofficial.get_app_secret(credential_resolver)
        from .QQOfficialWebSocketAdapter import QQOfficialWebSocketAdapter

        return QQOfficialWebSocketAdapter(
            qqofficial.app_id, secret, config.platform,
            sandbox=qqofficial.sandbox, workers=qqofficial.workers,
            call_timeout=qqofficial.call_timeout,
        )

    raise ValueError(f"unknown adapter type: {config.type}")


async def run_adapter(bot: object) -> None:
    install_adapter(bot, load_config().adapter)
    await bot.run_async()


def install_adapter(bot: object, config: AdapterConfig | None = None) -> Adapter:
    """Read config when needed, construct an adapter and register it with Bot."""

    if config is None:
        config = load_config().adapter
    adapter = create_adapter(config)
    bot.install(adapter)
    return adapter


__all__ = ["Adapter", "create_adapter", "install_adapter", "run_adapter"]
