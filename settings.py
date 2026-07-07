from dataclasses import dataclass
from pathlib import Path
import tomllib


@dataclass(slots=True)
class WebSocketConfig:
    host: str
    port: int


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
            ),
        ),
    )
