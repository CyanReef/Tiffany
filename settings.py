"""Parse TOML without reading credentials or rewriting the user's config."""
from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
import math
import os
from pathlib import Path
import tomllib
from collections.abc import Callable

from deployment.paths import PROJECT_ROOT, RuntimePaths
from shared.scheduler import SchedulerPolicy

CredentialResolver = Callable[[str, str], str | None]


def is_loopback(host: str) -> bool:
    normalized = host.strip().lower()
    if normalized == "localhost" or normalized.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(normalized.strip("[]")).is_loopback
    except ValueError:
        return False


def _integer(value, name, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")


def _positive(value, name):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")


def _string(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


@dataclass(slots=True)
class WebSocketConfig:
    host: str = "127.0.0.1"
    port: int = 6199
    workers: int | None = None
    call_timeout: float = 30.0
    pending_limit: int = 256
    token_env: str = "TIFFANY_ONEBOT_TOKEN"

    def __post_init__(self):
        _string(self.host, "adapter.websocket.host")
        _string(self.token_env, "adapter.websocket.token_env")
        _integer(self.port, "adapter.websocket.port", 0, 65535)
        if self.workers is not None:
            _integer(self.workers, "adapter.websocket.workers", 1, 65536)
        _integer(self.pending_limit, "adapter.websocket.pending_limit", 1, 65536)
        _positive(self.call_timeout, "adapter.websocket.call_timeout")

    def get_token(self, platform: str, resolver: CredentialResolver | None = None) -> str | None:
        token = os.environ.get(self.token_env) or (resolver("onebot", platform) if resolver else None)
        if not token and not is_loopback(self.host):
            raise ValueError("non-loopback OneBot binding requires a Token; run configure")
        return token


@dataclass(slots=True)
class QQOfficialConfig:
    app_id: str
    app_secret_env: str = "TIFFANY_QQBOT_SECRET"
    sandbox: bool = False
    workers: int | None = None
    max_inflight: int = 16
    call_timeout: float = 30.0

    def __post_init__(self):
        _string(self.app_id, "adapter.qqofficial.app_id")
        _string(self.app_secret_env, "adapter.qqofficial.app_secret_env")
        if type(self.sandbox) is not bool:
            raise ValueError("adapter.qqofficial.sandbox must be a boolean")
        if self.workers is not None:
            _integer(self.workers, "adapter.qqofficial.workers", 1, 65536)
        _integer(self.max_inflight, "adapter.qqofficial.max_inflight", 1, 65536)
        _positive(self.call_timeout, "adapter.qqofficial.call_timeout")

    def get_app_secret(self, resolver: CredentialResolver | None = None) -> str:
        secret = os.environ.get(self.app_secret_env) or (resolver("qqofficial", self.app_id) if resolver else None)
        if not secret:
            raise ValueError(f"QQ official secret is missing: {self.app_secret_env}; run login")
        return secret


@dataclass(slots=True)
class AdapterConfig:
    type: str
    platform: str
    websocket: WebSocketConfig | None = None
    qqofficial: QQOfficialConfig | None = None

    def __post_init__(self):
        _string(self.platform, "adapter.platform")


@dataclass(slots=True)
class BotConfig:
    name: str = "Tiffany"

    def __post_init__(self):
        _string(self.name, "bot.name")


@dataclass(slots=True)
class LoggingConfig:
    level: str = "INFO"
    max_bytes: int = 10 * 1024 * 1024
    backups: int = 10

    def __post_init__(self):
        if self.level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            raise ValueError("logging.level is invalid")
        _integer(self.max_bytes, "logging.max_bytes", 1024, 1024 * 1024 * 1024)
        _integer(self.backups, "logging.backups", 1, 100)


@dataclass(slots=True)
class MonitoringConfig:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 9464
    allow_remote: bool = False

    def __post_init__(self):
        if type(self.enabled) is not bool or type(self.allow_remote) is not bool:
            raise ValueError("monitoring.enabled and allow_remote must be booleans")
        _string(self.host, "monitoring.host")
        _integer(self.port, "monitoring.port", 0, 65535)
        if not self.allow_remote and not is_loopback(self.host):
            raise ValueError("non-loopback monitoring binding requires allow_remote=true")


@dataclass(slots=True)
class RestartConfig:
    enabled: bool = True
    window: float = 600.0
    max_restarts: int = 5
    delays: list[float] = field(default_factory=lambda: [2, 5, 15, 30, 60])
    stop_timeout: float = 45.0

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("restart.enabled must be a boolean")
        _positive(self.window, "restart.window")
        _positive(self.stop_timeout, "restart.stop_timeout")
        _integer(self.max_restarts, "restart.max_restarts", 1, 100)
        if not isinstance(self.delays, list) or not self.delays:
            raise ValueError("restart.delays must be a non-empty list")
        for delay in self.delays:
            _positive(delay, "restart.delays")


@dataclass(slots=True)
class AppConfig:
    bot: BotConfig
    adapter: AdapterConfig
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    monitoring: MonitoringConfig = field(default_factory=MonitoringConfig)
    restart: RestartConfig = field(default_factory=RestartConfig)
    scheduler: SchedulerPolicy = field(default_factory=SchedulerPolicy)

    def validate_credentials(self, resolver: CredentialResolver | None = None) -> tuple[str, ...]:
        if self.adapter.type == "qqofficial_websocket":
            if self.adapter.qqofficial is None:
                raise ValueError("qqofficial_websocket requires adapter.qqofficial configuration")
            return (self.adapter.qqofficial.get_app_secret(resolver),)
        if self.adapter.type == "onebot_websocket":
            if self.adapter.websocket is None:
                raise ValueError("onebot_websocket requires adapter.websocket configuration")
            token = self.adapter.websocket.get_token(self.adapter.platform, resolver)
            return (token,) if token else ()
        raise ValueError("unknown adapter type")


def parse_config(data: dict) -> AppConfig:
    try:
        adapter = data["adapter"]
        if adapter["type"] == "onebot_websocket":
            protocol = {"websocket": WebSocketConfig(**adapter.get("websocket", {}))}
        elif adapter["type"] == "qqofficial_websocket":
            protocol = {"qqofficial": QQOfficialConfig(**adapter["qqofficial"])}
        else:
            raise ValueError("unknown adapter type")
        return AppConfig(
            bot=BotConfig(**data.get("bot", {})),
            adapter=AdapterConfig(adapter["type"], adapter["platform"], **protocol),
            logging=LoggingConfig(**data.get("logging", {})),
            monitoring=MonitoringConfig(**data.get("monitoring", {})),
            restart=RestartConfig(**data.get("restart", {})),
            scheduler=SchedulerPolicy(**data.get("scheduler", {})),
        )
    except (KeyError, TypeError) as error:
        raise ValueError(f"invalid configuration structure ({type(error).__name__})") from None


def load_config(path: str | Path | None = None) -> AppConfig:
    config_path = RuntimePaths.resolve().config if path is None else Path(path)
    if not config_path.is_absolute():
        config_path = PROJECT_ROOT / config_path
    return parse_config(tomllib.loads(config_path.read_text(encoding="utf-8")))
