from core import ServiceKey
from .model import CommandSpec, Contribution


COMMANDS = ServiceKey("business.help.commands")


class Help:
    def bind(self, prefix, names):
        self.text = "命令: " + " ".join(prefix + name for name in names)

    async def reply(self, ctx, command):
        await ctx.reply(self.text)


def install(scope, config):
    help_service = Help()
    scope.service(COMMANDS, Contribution((CommandSpec("help", help_service.reply),), help=help_service))
    return COMMANDS
