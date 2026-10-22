import asyncio
import json
import subprocess
import sys
import tomllib
import unittest

from adapters.onebot_fields import register_onebot_fields
from core import Bot, Envelope, Field, OwnerInUseError, ServiceKey
from deployment.configure import encode_config
from fields import MESSAGE_TYPE, SELF_ID, USER_ID
from hooks.extensions import REGISTRY, install_extensions
from hooks.extensions.commands import CommandRouter
from hooks.extensions.model import CommandSpec, Contribution
from hooks.extensions.ratelimit import RateLimiter
from shared.extensions import ExtensionsConfig, RateLimitConfig, ordered_ids
from tests.unit.deployment.test_storage import onebot_config


class ReplyClient:
    def __init__(self):
        self.replies = []

    async def reply(self, ctx, text):
        self.replies.append(text)


def envelope(text, client, *, user=1, scene="group", adapter="test", session="room"):
    return Envelope("test", {"message": text, "user_id": user, "self_id": 99,
                    "message_type": scene}, client=client, kind="message",
                    adapter_id=adapter, session_id=session)


class ConfigTests(unittest.TestCase):
    def test_resource_limits_are_hard_bounds_with_lower_limits_allowed(self):
        for name, option, maximum in (("ratelimit", "max_buckets", 4096),
                                      ("notes", "max_entries", 10000),
                                      ("notes", "max_bytes", 4096),
                                      ("notes", "max_inflight", 64)):
            with self.subTest(option=option):
                with self.assertRaises(ValueError):
                    ExtensionsConfig.parse({name: {option: maximum + 1}})
                self.assertEqual(getattr(getattr(ExtensionsConfig.parse({name: {option: maximum}}), name), option), maximum)
                self.assertEqual(getattr(getattr(ExtensionsConfig.parse({name: {option: 1}}), name), option), 1)

    def test_early_errors_and_topological_order(self):
        for data in ({"enabled": ["missing"]}, {"enabled": ["ping"]},
                     {"enabled": ["commands", "commands"]}, {"enabled": "commands"},
                     {"enabled": [], "commands": {"prefix": ""}},
                     {"enabled": [], "permissions": {"admins": [{"adapter_id": "a", "scene": "group", "user_id": True}]}},
                     {"enabled": [], "ratelimit": {"rate": float("nan")}},
                     {"enabled": [], "notes": {"max_inflight": 0}},
                     {"enabled": [], "notes": {"typo": 1}}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                ExtensionsConfig.parse(data)
        self.assertEqual(ordered_ids(["ping", "commands", "help"]), ("commands", "ping", "help"))
        with self.assertRaisesRegex(ValueError, "cycle"):
            ordered_ids(["a", "b"], {"a": ("b",), "b": ("a",)})

    def test_toml_identity_roundtrip_and_disabled_imports(self):
        data = onebot_config() | {"extensions": {"enabled": ["commands", "permissions"],
                    "permissions": {"admins": [{"adapter_id": "a", "scene": "group", "user_id": 123},
                                               {"adapter_id": "a", "scene": "private", "user_id": "123"}]}}}
        self.assertEqual(tomllib.loads(encode_config(data).decode()), data)
        code = """
import asyncio, sys
from core import Bot
from hooks import install_extensions
from shared.extensions import ExtensionsConfig
async def run():
    bot=Bot()
    await install_extensions(bot,ExtensionsConfig.parse({'enabled':['commands','ping']}))
    assert 'hooks.extensions.notes' not in sys.modules
    assert 'hooks.extensions.permissions' not in sys.modules
    assert 'hooks.extensions.ratelimit' not in sys.modules
    assert 'sqlite3' not in sys.modules
asyncio.run(run())
"""
        result = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_bucket_refill_expiration_and_capacity(self):
        now = [0.0]
        limiter = RateLimiter(RateLimitConfig(rate=1, burst=2, max_buckets=2, idle_ttl=3), clock=lambda: now[0])
        self.assertTrue(limiter.allow("a"))
        self.assertTrue(limiter.allow("a"))
        self.assertFalse(limiter.allow("a"))
        self.assertTrue(limiter.allow("b"))
        self.assertFalse(limiter.allow("c"))
        now[0] = 1
        self.assertTrue(limiter.allow("a"))
        now[0] = 5
        self.assertTrue(limiter.allow("c"))
        self.assertEqual(len(limiter.buckets), 1)
        limiter = RateLimiter(RateLimitConfig(max_buckets=20), clock=lambda: now[0])
        for index in range(20):
            self.assertTrue(limiter.allow(index))
        now[0] += 601
        self.assertTrue(limiter.allow("new"))
        self.assertEqual(len(limiter.buckets), 13)


class ExtensionTests(unittest.IsolatedAsyncioTestCase):
    async def test_without_health_command_plain_and_unknown_messages_do_not_parse_identity(self):
        await install_extensions(self.bot, ExtensionsConfig.parse({"enabled": ["commands", "help"]}))
        await self.bot.start()
        for text in ("", "hello", "ping", "/unknown args"):
            ctx = await self.emit(text)
            self.assertFalse(ctx.has(USER_ID))
            self.assertFalse(ctx.has(SELF_ID))
        self.assertEqual(self.client.replies, [])
        await self.emit("/help")
        self.assertEqual(self.client.replies, ["命令: /help"])

    async def test_literal_routes_preserve_prefix_case_and_parameter_semantics(self):
        from hooks.command import COMMAND
        await install_extensions(self.bot, ExtensionsConfig.parse({"enabled": ["commands", "ping", "help"],
                                                                  "commands": {"prefix": "!!"}}))
        await self.bot.start()
        for text in ("ping", " PiNg ", "!!ping", " !!PING ", "!!ping\targ"):
            ctx = await self.emit(text)
            if text.strip().startswith("!!"):
                parsed = ctx.resolve(COMMAND)
                self.assertEqual(parsed.name, "ping")
                self.assertEqual(parsed.args, ("arg",) if "arg" in text else ())
        self.assertEqual(self.client.replies, ["pong"] * 5)
        ctx = await self.emit("!!missing arguments")
        self.assertFalse(ctx.has(USER_ID))
        self.assertFalse(ctx.has(SELF_ID))
        await self.emit("!!help")
        await self.emit("!!help")
        self.assertEqual(self.client.replies[-2:], ["命令: !!ping !!help"] * 2)

    async def test_commands_only_does_not_require_unused_identity_fields(self):
        from hooks.command import COMMAND
        from fields import TEXT
        bot = Bot()
        self.addAsyncCleanup(bot.stop, "abort")
        bot.provide(TEXT, lambda ctx: ctx.raw["text"])
        await install_extensions(bot, ExtensionsConfig.parse({"enabled": ["commands"]}))
        self.assertEqual(bot.dispatcher.hooks, ())
        seen = []
        @bot.hook(on="message", needs=(COMMAND,))
        async def own(ctx):
            seen.append(ctx.resolve(COMMAND))
        async with bot:
            await bot.emit(Envelope("demo", {"text": "/TEST one"}, kind="message"))
        self.assertEqual((seen[0].name, seen[0].args), ("test", ("one",)))

    async def test_wrapped_ping_is_executed_even_with_the_same_function_name(self):
        from functools import wraps
        from hooks.extensions.ping import reply_ping
        @wraps(reply_ping)
        async def wrapped(ctx, command):
            await ctx.reply("wrapper ran")
            await reply_ping(ctx, command)
        class WrappedSpec:
            requires = ("commands",)
            def validate(self, config): return None
            def install(self, scope, config):
                key = ServiceKey("wrapped-ping")
                scope.service(key, Contribution((CommandSpec("ping", wrapped, health=True),)))
                return key
        await install_extensions(self.bot, ExtensionsConfig.parse({"enabled": ["commands", "ping"]}),
                                 registry=dict(REGISTRY) | {"ping": WrappedSpec()})
        await self.bot.start()
        await self.emit("ping")
        self.assertEqual(self.client.replies, ["wrapper ran", "pong"])

    async def test_reinstall_rejected_without_revoking_existing_scopes(self):
        config = ExtensionsConfig.parse({"enabled": ["commands", "ping"]})
        installed = await install_extensions(self.bot, config)
        snapshots = self.bot.providers.snapshot(), self.bot.services.snapshot(), self.bot.dispatcher.snapshot()
        with self.assertRaisesRegex(ValueError, "already been used"):
            await install_extensions(self.bot, config)
        self.assertEqual(snapshots, (self.bot.providers.snapshot(), self.bot.services.snapshot(), self.bot.dispatcher.snapshot()))
        self.assertTrue(all(scope.active for scope in installed.scopes.values()))
        await self.bot.start()
        await self.emit("ping")
        self.assertEqual(self.client.replies, ["pong"])

    async def asyncSetUp(self):
        self.bot, self.client = Bot(), ReplyClient()
        self.addAsyncCleanup(self.bot.stop, "abort")
        register_onebot_fields(self.bot, "test")

    async def emit(self, text, **options):
        return await self.bot.emit(envelope(text, self.client, **options))

    async def test_one_hook_ping_help_and_lazy_negative_paths(self):
        installed = await install_extensions(self.bot, ExtensionsConfig.parse({"enabled": ["commands", "ping", "help"]}))
        await self.bot.start()
        for text in ("hello", "/missing a b", "", "/"):
            ctx = await self.emit(text)
            self.assertFalse(ctx.has(USER_ID))
            self.assertFalse(ctx.has(SELF_ID))
            self.assertFalse(ctx.has(MESSAGE_TYPE))
        for text in ("ping", " PiNg ", "/PING", "/ping\tignored"):
            await self.emit(text)
        await self.emit("/help")
        await self.emit("ping", user=99)
        self.assertEqual(self.client.replies[:4], ["pong"] * 4)
        self.assertEqual(self.client.replies[4], "命令: /ping /help")
        self.assertEqual(len(self.bot.dispatcher.hooks), 1)
        with self.assertRaises(OwnerInUseError):
            await installed.scopes["ping"].unload()
        await installed.scopes["commands"].unload()
        await installed.scopes["ping"].unload()
        await installed.scopes["help"].unload()

    async def test_permission_native_identity_and_limiter_are_command_local(self):
        config = ExtensionsConfig.parse({"enabled": ["commands", "ping", "help", "permissions", "ratelimit"],
            "permissions": {"admins": [{"adapter_id": "test", "scene": "group", "user_id": 1}]},
            "ratelimit": {"burst": 1}})
        installed = await install_extensions(self.bot, config)
        await self.bot.start()
        await self.emit("/status", user="1")
        await self.emit("/status", scene="private")
        await self.emit("/status", adapter="other")
        self.assertEqual(self.client.replies, [])
        await self.emit("/status")
        self.assertIn("state=running", self.client.replies.pop())
        await self.emit("/help")
        self.assertEqual(self.client.replies, [])
        await self.emit("ping")
        await self.emit("/ping")
        self.assertEqual(self.client.replies, ["pong", "pong"])
        limiter = installed.router.limiter
        count = len(limiter.buckets)
        ctx = await self.emit("/unknown", user=123)
        self.assertFalse(ctx.has(USER_ID))
        self.assertEqual(len(limiter.buckets), count)
        await self.emit("/help", user=None)
        self.assertEqual(len(self.client.replies), 2)

    async def test_installation_failure_revokes_partial_registrations(self):
        class BrokenSpec:
            requires = ("commands",)
            def validate(self, config):
                return None
            def install(self, scope, config):
                scope.provide(Field("partial"), lambda ctx: 1)
                scope.service(ServiceKey("partial"), object())
                raise RuntimeError("install failed")
        before = (self.bot.providers.snapshot(), self.bot.services.snapshot())
        with self.assertRaisesRegex(RuntimeError, "install failed"):
            await install_extensions(self.bot, ExtensionsConfig.parse({"enabled": ["commands", "ping"]}),
                                     registry=dict(REGISTRY) | {"ping": BrokenSpec()})
        self.assertEqual(set(self.bot.providers.snapshot().providers), set(before[0].providers))
        self.assertEqual(set(self.bot.services.snapshot().services), set(before[1].services))
        self.assertEqual(self.bot.dispatcher.hooks, ())

    async def test_duplicate_commands_rollback_and_raw_preservation(self):
        class DuplicateSpec:
            requires = ("commands",)
            def validate(self, config): return None
            def install(self, scope, config):
                key = ServiceKey("duplicate")
                async def handler(ctx, command): pass
                scope.service(key, Contribution((CommandSpec("ping", handler),)))
                return key
        with self.assertRaisesRegex(ValueError, "duplicate command"):
            await install_extensions(self.bot, ExtensionsConfig.parse({"enabled": ["commands", "ping", "help"]}),
                                     registry=dict(REGISTRY) | {"help": DuplicateSpec()})
        self.assertEqual(self.bot.services.registrations(), ())
        bot = Bot()
        self.addAsyncCleanup(bot.stop, "abort")
        register_onebot_fields(bot, "test")
        await install_extensions(bot, ExtensionsConfig.parse({"enabled": ["commands", "ping"]}))
        await bot.start()
        event = envelope("/ping", self.client)
        before = json.dumps(event.raw)
        ctx = await bot.emit(event)
        self.assertIs(ctx.raw, event.raw)
        self.assertEqual(json.dumps(event.raw), before)
