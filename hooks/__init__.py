"""业务组合入口：选择并注册应用启用的派生字段与消息 Hook。"""

from core import Bot

def register_hooks(bot: Bot) -> None:
    from .command import register_command_provider
    from .ping import register as register_ping
    register_command_provider(bot)
    register_ping(bot)


async def install_extensions(bot, config):
    from .extensions import install_extensions as install
    return await install(bot, config)


__all__ = ["register_hooks", "install_extensions"]
