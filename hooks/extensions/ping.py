from core import ServiceKey
from fields import SELF_ID, USER_ID
from .model import CommandSpec, Contribution


COMMANDS = ServiceKey("business.ping.commands")


async def reply_ping(ctx, command):
    user = ctx.resolve(USER_ID)
    if user is not None and user == ctx.resolve(SELF_ID):
        return
    # Preserve the old ping's resolution of SELF_ID even with a missing user.
    if user is None:
        ctx.resolve(SELF_ID)
    await ctx.reply("pong")


def install(scope, config):
    scope.service(COMMANDS, Contribution((CommandSpec("ping", reply_ping, health=True),)))
    return COMMANDS
