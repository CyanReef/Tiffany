"""Exercise public APIs with neighboring layers and optional packages absent."""
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[3]
OPTIONAL = {"aiohttp", "websockets", "prometheus_client", "qqbot_agent_sdk", "qrcode"}


class LayeringTests(unittest.TestCase):
    def run_isolated(self, blocked, body):
        guard = f"""
import importlib.abc
import sys

blocked = {blocked!r}

class BlockLayer(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.partition('.')[0] in blocked:
            raise ImportError('layer must remain independent: ' + fullname)

sys.meta_path.insert(0, BlockLayer())
"""
        result = subprocess.run(
            [sys.executable, "-B", "-c", textwrap.dedent(guard) + textwrap.dedent(body)],
            cwd=ROOT, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_core_runs_with_path_service_without_deployment_or_protocols(self):
        self.run_isolated(OPTIONAL | {"deployment", "adapters", "clients", "hooks"}, """
            import asyncio
            import tempfile
            from core import APP_PATHS, Bot, Envelope, RuntimePaths
            from shared.paths import RuntimePaths as SharedPaths

            assert RuntimePaths is SharedPaths

            async def exercise(home):
                paths = RuntimePaths.resolve(home)
                paths.initialize()
                bot = Bot()
                bot.service(APP_PATHS, paths)

                @bot.hook(uses=(APP_PATHS,))
                async def save(ctx):
                    target = ctx.service(APP_PATHS).storage / 'business.txt'
                    target.write_text(ctx.raw['text'], encoding='utf-8')

                async with bot:
                    await bot.emit(Envelope('test', {'text': 'persisted'}))
                assert (paths.storage / 'business.txt').read_text(encoding='utf-8') == 'persisted'
                assert (await bot.runtime.wait_closed()).successful

            with tempfile.TemporaryDirectory() as home:
                asyncio.run(exercise(home))
            assert not any(name.partition('.')[0] in blocked for name in sys.modules)
        """)

    def test_launcher_imports_before_core_or_third_party_dependencies_exist(self):
        self.run_isolated(OPTIONAL | {"core", "adapters", "clients", "hooks"}, """
            from deployment.launcher import arguments
            from deployment.paths import RuntimePaths
            from shared.paths import RuntimePaths as SharedPaths

            assert RuntimePaths is SharedPaths
            assert arguments(['check-config', '--home', 'test home']).home == 'test home'
            assert not any(name.partition('.')[0] in blocked for name in sys.modules)
        """)
