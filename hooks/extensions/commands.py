"""Startup-compiled command lookup, with a single resource-owning Hook."""
from types import MappingProxyType
import sys

from core import ServiceKey
from fields import MESSAGE_TYPE, SELF_ID, TEXT, USER_ID
from hooks.command import COMMAND, Command, _parser


ROUTER = ServiceKey("business.command_router")


def install(scope, config):
    scope.provide(COMMAND, _parser(config.prefix), requires=(TEXT,))


class CommandRouter:
    def __init__(self, prefix, contributions):
        table = {}
        self.permission = self.limiter = None
        for contribution in contributions:
            for spec in contribution.commands:
                if spec.name in table:
                    raise ValueError(f"duplicate command: {spec.name}")
                table[spec.name] = spec
            if contribution.permission is not None:
                self.permission = contribution.permission
            if contribution.limiter is not None:
                self.limiter = contribution.limiter
        if any(spec.admin for spec in table.values()) and self.permission is None:
            raise ValueError("admin commands require the permissions extension")
        self.table = MappingProxyType(table)
        self.prefix = prefix
        candidate = table.get("ping")
        self.ping = candidate if candidate is not None and candidate.health else None
        self._health_command = Command("ping", ())
        for contribution in contributions:
            if contribution.help is not None:
                contribution.help.bind(prefix, tuple(table))
        self.handle = self._make_handler()

    def _make_handler(self):
        # Bind the startup snapshot once. No reflection, registry scans or
        # service lookups are needed to choose a command on the hot path.
        prefix, table = self.prefix, self.table
        ping = self.ping
        ping_module = sys.modules.get("hooks.extensions.ping")
        inline_ping = ping is not None and ping_module is not None and ping.handler is ping_module.reply_ping
        ping_handler = ping.handler if ping is not None else None
        health_command = self._health_command
        allows = self.permission.allows if self.permission is not None else None
        allow = self.limiter.allow if self.limiter is not None else None

        # Literal zero-argument routes are derived only from declarations,
        # never from incoming text. This table is bounded by command count.
        literals = {prefix + name: (spec, Command(name, ())) for name, spec in table.items()}
        literal_get = literals.get
        table_get = table.get
        initial = prefix[0]

        def match(text):
            literal = literal_get(text)
            if literal is not None:
                spec, command = literal
            else:
                if not text.startswith(prefix):
                    return
                head = text[len(prefix):].split(None, 1)
                if not head:
                    return
                name = head[0].casefold()
                spec = table_get(name)
                if spec is None:
                    return
                command = Command(name, tuple(head[1].split()) if len(head) == 2 else ())
            return spec, command

        async def invoke(ctx, spec, command):
            ctx.put(COMMAND, command)
            if spec.health:
                await spec.handler(ctx, command)
                ctx.stop()
                return
            user = ctx.resolve(USER_ID)
            if user is not None and user == ctx.resolve(SELF_ID):
                ctx.stop()
                return
            who = None
            if spec.admin or allow is not None:
                scene = ctx.resolve(MESSAGE_TYPE)
                if type(user) in (int, str) and user != "" and isinstance(scene, str) and scene:
                    who = (ctx.envelope.adapter_id, scene, type(user), user)
            if spec.admin and (who is None or not allows(who)):
                ctx.stop()
                return
            if allow is not None and (who is None or not allow(who)):
                ctx.stop()
                return
            await spec.handler(ctx, command)
            ctx.stop()

        if inline_ping:
            # Select the built-in handler once at startup, by identity. The
            # canonical health path has neither routing nor policy branches.
            async def handle(ctx):
                text = ctx.resolve(TEXT).strip()
                if text == "ping":
                    user = ctx.resolve(USER_ID)
                    own = ctx.resolve(SELF_ID)
                    if user is None or user != own:
                        await ctx.reply("pong")
                    ctx.stop()
                    return
                if text[:1] in ("p", "P") and text.casefold() == "ping":
                    await ping_handler(ctx, health_command)
                    ctx.stop()
                    return
                if text[:1] != initial:
                    return
                matched = match(text)
                if matched is not None:
                    await invoke(ctx, *matched)
        elif ping_handler is not None:
            async def handle(ctx):
                text = ctx.resolve(TEXT).strip()
                if text[:1] in ("p", "P") and text.casefold() == "ping":
                    await ping_handler(ctx, health_command)
                    ctx.stop()
                    return
                if text[:1] != initial:
                    return
                matched = match(text)
                if matched is not None:
                    await invoke(ctx, *matched)
        else:
            async def handle(ctx):
                text = ctx.resolve(TEXT).strip()
                if text[:1] != initial:
                    return
                matched = match(text)
                if matched is not None:
                    await invoke(ctx, *matched)
        return handle


def publish(scope, prefix, keys):
    contributions = tuple(scope.bot.services.get(key) for key in keys)
    router = CommandRouter(prefix, contributions)
    needs = [TEXT, USER_ID, SELF_ID]
    uses = list(keys)
    if router.permission is not None or router.limiter is not None:
        needs.append(MESSAGE_TYPE)
    for spec in router.table.values():
        needs.extend(spec.needs)
        uses.extend(spec.uses)
    uses = tuple(dict.fromkeys(uses))
    scope.service(ROUTER, router, dependencies=uses)
    if router.table:
        scope.register_hook(router.handle, name="commands", on="message",
                            needs=tuple(dict.fromkeys(needs)), uses=(ROUTER, *uses))
    return router
