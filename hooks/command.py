from dataclasses import dataclass

from core import Bot, Context, Field
from fields import TEXT


@dataclass(frozen=True, slots=True)
class Command:
    name: str
    args: tuple[str, ...]


COMMAND = Field[Command | None]("command")


def _parser(prefix: str):
    def parse(ctx: Context) -> Command | None:
        text = ctx.resolve(TEXT).strip()
        if not text.startswith(prefix):
            return None

        body = text[len(prefix):].strip()
        if not body:
            return None

        name, *args = body.split()
        return Command(name=name.casefold(), args=tuple(args))

    return parse


def register_command_provider(bot: Bot, *, prefix: str = "/") -> None:
    if not prefix:
        raise ValueError("command prefix cannot be empty")
    bot.provide(COMMAND, _parser(prefix), requires=(TEXT,))


__all__ = ["COMMAND", "Command", "register_command_provider"]
