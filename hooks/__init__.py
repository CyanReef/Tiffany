from core import Bot

from .command import register_command_provider
from .ping import register as register_ping


def register_hooks(bot: Bot) -> None:
    register_command_provider(bot)
    register_ping(bot)


__all__ = ["register_hooks"]
