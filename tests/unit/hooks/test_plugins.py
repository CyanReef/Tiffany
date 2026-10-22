import importlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from core import APP_PATHS, Bot, Envelope
from hooks.plugins import install_plugins
from shared.paths import RuntimePaths
from shared.plugins import PluginError, PluginSettings, discover_plugins
from tests.support.plugins import plugin_project


class PluginLoadingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.paths = RuntimePaths.resolve(self.temp.name)
        self.paths.initialize()
        self.bot = Bot()
        self.bot.service(APP_PATHS, self.paths)

    def source(self, plugin_id, code, **kwargs):
        root = plugin_project(self.paths.plugins / plugin_id, plugin_id, code=code, **kwargs)
        sys.path.insert(0, str(root))
        self.addCleanup(sys.path.remove, str(root))
        self.addCleanup(sys.modules.pop, f"plugin_{plugin_id}", None)
        importlib.invalidate_caches()
        return root

    async def test_config_dispatch_uses_only_registered_hook_and_closes_resources(self):
        self.source("sample", '''from core import APP_PATHS, ServiceKey
KEY = ServiceKey('sample')
class Resource:
    async def setup(self, runtime): self.runtime = runtime
    async def teardown(self): self.closed = True
def install(scope, config):
    resource = Resource()
    scope.service(KEY, resource)
    @scope.hook(on='message', uses=(KEY, APP_PATHS))
    async def echo(ctx):
        path = ctx.service(APP_PATHS).storage / 'result.txt'
        path.write_text(config['prefix'] + ctx.raw['text'])
''')
        (self.paths.home / "config/sample.toml").write_text('prefix = "reply:"')
        installed = await install_plugins(self.bot, self.paths, discover_plugins(self.paths))
        resource = self.bot.services.get(importlib.import_module("plugin_sample").KEY)
        with patch("shared.plugins.discover_plugins", side_effect=AssertionError("message path scanned plugins")):
            async with self.bot:
                await self.bot.emit(Envelope("test", {"text": "hello"}, kind="message"))
        self.assertEqual((self.paths.storage / "result.txt").read_text(), "reply:hello")
        self.assertTrue(resource.closed)
        self.assertEqual(installed.scopes[0].name, "plugins.sample")

    async def test_two_plugins_share_exact_service_key(self):
        self.source("provider", "from core import ServiceKey\nKEY=ServiceKey('shared')\ndef install(scope, config):\n scope.service(KEY, [])\n")
        self.source("consumer", "from plugin_provider import KEY\ndef install(scope, config):\n @scope.hook(uses=(KEY,))\n async def use(ctx): ctx.service(KEY).append(ctx.raw['value'])\n", requires=("provider",))
        await install_plugins(self.bot, self.paths, discover_plugins(self.paths))
        values = self.bot.services.get(importlib.import_module("plugin_provider").KEY)
        async with self.bot:
            await self.bot.emit(Envelope("test", {"value": 3}))
        self.assertEqual(values, [3])

    async def test_failed_registration_rolls_back_previous_and_partial_scope(self):
        self.source("first", "def install(scope, config):\n @scope.hook()\n async def first(ctx): pass\n")
        self.source("second", "def install(scope, config):\n @scope.hook()\n async def second(ctx): pass\n raise RuntimeError('broken')\n", requires=("first",))
        with self.assertRaisesRegex(PluginError, "second.*registration"):
            await install_plugins(self.bot, self.paths, discover_plugins(self.paths))
        for name in ("first", "second"):
            self.assertFalse(self.bot.dispatcher.handles_for_owner(f"plugins.{name}"))
            self.assertFalse(self.bot.scope(f"plugins.{name}").active)

    async def test_disabled_plugin_never_imports(self):
        self.source("sample", "raise AssertionError('disabled import')")
        self.paths.plugin_settings.write_bytes(PluginSettings(disabled=("sample",)).encode())
        await install_plugins(self.bot, self.paths, discover_plugins(self.paths))
        self.assertNotIn("plugin_sample", sys.modules)

    async def test_async_install_and_invalid_configuration_fail_with_plugin_id(self):
        self.source("sample", "async def install(scope, config): pass\n")
        with self.assertRaisesRegex(PluginError, "sample.*import"):
            await install_plugins(self.bot, self.paths, discover_plugins(self.paths))
        (self.paths.home / "config/sample.toml").write_text("invalid = [")
        with self.assertRaisesRegex(PluginError, "sample.*configuration"):
            await install_plugins(self.bot, self.paths, discover_plugins(self.paths))

    async def test_initialization_failure_is_identified_and_adapters_do_not_start(self):
        self.source("sample", "from core import ServiceKey\nclass Broken:\n async def setup(self, runtime): raise RuntimeError('broken setup')\ndef install(scope, config): scope.service(ServiceKey('broken'), Broken())\n")
        installed = await install_plugins(self.bot, self.paths, discover_plugins(self.paths))
        with self.assertRaisesRegex(RuntimeError, "broken setup"):
            await self.bot.start()
        error = installed.startup_failure(self.bot.runtime)
        self.assertEqual(error.plugin, "sample")
        self.assertEqual(error.phase, "setup")
        self.assertTrue((await self.bot.stop("abort")).successful)
