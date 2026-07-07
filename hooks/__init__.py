from core import Bot

from .print_text import register as register_print_text


def register_hooks(bot: Bot) -> None:
    register_print_text(bot)


__all__ = ["register_hooks"]
