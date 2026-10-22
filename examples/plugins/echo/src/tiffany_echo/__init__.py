"""Minimal plugin: reply to /echo while preserving platform-owned clients."""
from fields import TEXT


def install(scope, config):
    prefix = config.get("prefix", "/echo ")
    if not isinstance(prefix, str) or not prefix:
        raise ValueError("echo.prefix must be a nonempty string")

    @scope.hook(name="echo", on="message", needs=(TEXT,))
    async def echo(ctx):
        text = ctx.resolve(TEXT)
        if isinstance(text, str) and text.startswith(prefix):
            await ctx.reply(text[len(prefix):])
