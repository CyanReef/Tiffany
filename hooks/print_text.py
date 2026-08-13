import logging

from core import Bot
from fields import TEXT


logger = logging.getLogger(__name__)


def register(bot: Bot) -> None:
    """Opt-in diagnostic hook; it is intentionally not part of defaults."""

    @bot.hook(name="print_text", on="message", needs=(TEXT,))
    async def print_text(ctx):
        text = ctx.resolve(TEXT)
        if text:
            logger.info("TEXT: %s", text)
