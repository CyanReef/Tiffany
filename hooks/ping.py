from core import Bot
from fields import SELF_ID, USER_ID

from .command import COMMAND


def register(bot: Bot) -> None:
    @bot.hook(
        name="ping",
        on="message",
        needs=(COMMAND, USER_ID, SELF_ID),
    )
    async def ping(ctx):
        command = ctx.resolve(COMMAND)
        if command is None or command.name != "ping":
            return

        user_id = ctx.resolve(USER_ID)
        self_id = ctx.resolve(SELF_ID)
        if user_id is not None and user_id == self_id:
            return

        await ctx.reply("pong")
        ctx.stop()
