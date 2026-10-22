from core import ServiceKey
from .model import CommandSpec, Contribution


COMMANDS = ServiceKey("business.permissions.commands")


class Permissions:
    def __init__(self, config):
        self.admins = frozenset((a.adapter_id, a.scene, type(a.user_id), a.user_id) for a in config.admins)

    def allows(self, who):
        return who in self.admins


def install(scope, config):
    runtime = scope.bot.runtime

    async def status(ctx, command):
        await ctx.reply(f"state={runtime.state.value} queued={runtime.scheduler.queued} active={runtime.scheduler.active}")

    scope.service(COMMANDS, Contribution((CommandSpec("status", status, admin=True),), permission=Permissions(config)))
    return COMMANDS
