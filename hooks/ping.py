from core import Bot
from fields import SELF_ID, TEXT, USER_ID


def register(bot: Bot) -> None:
    @bot.hook(
        name="ping",
        on="message",
        needs=(TEXT, USER_ID, SELF_ID),
    )
    async def ping(ctx):
        if ctx.resolve(TEXT).strip().casefold() != "ping":
            return

        user_id = ctx.resolve(USER_ID)
        self_id = ctx.resolve(SELF_ID)
        if user_id is not None and user_id == self_id:
            return

        await ctx.reply("pong")
        ctx.stop()
