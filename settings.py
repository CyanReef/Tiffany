from dataclasses import dataclass
from pathlib import Path
import tomllib


@dataclass(slots=True)
class WebSocketConfig:
    host: str
    port: int
    workers: int = 4
    queue_size: int = 256
    call_timeout: float = 30.0
    pending_limit: int = 256

    def __post_init__(self) -> None:
        if self.workers < 1:
            raise ValueError("adapter.websocket.workers must be at least 1")
        if self.workers > 4:
            raise ValueError("adapter.websocket.workers cannot exceed 4")
        if self.queue_size < 1:
            raise ValueError("adapter.websocket.queue_size must be at least 1")
        if self.queue_size > 256:
            raise ValueError("adapter.websocket.queue_size cannot exceed 256")
        if self.call_timeout <= 0:
            raise ValueError("adapter.websocket.call_timeout must be greater than zero")
        if self.pending_limit < 1:
            raise ValueError("adapter.websocket.pending_limit must be at least 1")


@dataclass(slots=True)
class AdapterConfig:
    type: str
    platform: str
    websocket: WebSocketConfig


@dataclass(slots=True)
class BotConfig:
    name: str


@dataclass(slots=True)
class AppConfig:
    bot: BotConfig
    adapter: AdapterConfig


def load_config(path: str | Path = "Tiffany.toml") -> AppConfig:
    config_path = Path(path)
    if not config_path.is_absolute():
        config_path = Path(__file__).with_name(str(config_path))

    data = tomllib.loads(config_path.read_text(encoding="utf-8"))

    return AppConfig(
        bot=BotConfig(
            name=data["bot"]["name"],
        ),
        adapter=AdapterConfig(
            type=data["adapter"]["type"],
            platform=data["adapter"]["platform"],
            websocket=WebSocketConfig(
                host=data["adapter"]["websocket"]["host"],
                port=data["adapter"]["websocket"]["port"],
                workers=data["adapter"]["websocket"].get("workers", 4),
                queue_size=data["adapter"]["websocket"].get("queue_size", 256),
                call_timeout=data["adapter"]["websocket"].get("call_timeout", 30.0),
                pending_limit=data["adapter"]["websocket"].get("pending_limit", 256),
            ),
        ),
    )
