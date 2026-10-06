import importlib.util
from dataclasses import asdict
import os
from pathlib import Path
import subprocess
import sys
import tomllib
import unittest
from unittest.mock import patch

from adapters import create_adapter
from settings import AdapterConfig, QQOfficialConfig, WebSocketConfig, load_config, parse_config


class QQOfficialConfigurationTests(unittest.TestCase):
    def test_scheduler_defaults_and_explicit_policy_round_trip(self):
        from core import SchedulerPolicy
        from deployment.configure import encode_config
        config = load_config('examples/Tiffany.onebot.toml')
        self.assertEqual(config.scheduler, SchedulerPolicy())
        self.assertIsNone(config.adapter.websocket.workers)
        data = asdict(config)
        data['scheduler'] = {'max_events': 1024, 'buffer_budget_bytes': 8388608, 'max_concurrency': 128}
        data['adapter']['websocket']['workers'] = 32
        parsed = parse_config(tomllib.loads(encode_config(data).decode()))
        self.assertEqual(parsed.scheduler, SchedulerPolicy(**data['scheduler']))
        self.assertEqual(create_adapter(parsed.adapter).workers, 32)
        self.assertNotIn('queue_size', asdict(parsed.adapter.websocket))
        for key in data['scheduler']:
            with self.subTest(key=key), self.assertRaises(ValueError):
                parse_config(data | {'scheduler': {key: 0}})

    @unittest.skipUnless(importlib.util.find_spec('aiohttp'), 'requires qqofficial extra')
    def test_qq_http_budget_is_independent_of_workers(self):
        with patch.dict(os.environ, {'TIFFANY_QQBOT_SECRET': 'test-secret'}):
            adapter = create_adapter(AdapterConfig('qqofficial_websocket', 'qq_official',
                qqofficial=QQOfficialConfig('app', workers=64, max_inflight=3)))
        self.assertEqual(adapter.workers, 64)
        self.assertEqual(adapter.client.max_inflight, 3)

    def test_existing_onebot_config_and_positional_constructor_still_work(self):
        config = load_config("examples/legacy/Tiffany.toml")
        self.assertEqual(config.adapter.type, "onebot_websocket")
        self.assertIsNotNone(create_adapter(AdapterConfig("onebot_websocket", "napcat", WebSocketConfig("127.0.0.1", 0))))

    def test_missing_secret_reports_variable_name_without_io(self):
        with patch.dict(os.environ, {}, clear=True):
            config = load_config("examples/Tiffany.qqofficial.toml")
            with self.assertRaisesRegex(ValueError, "TIFFANY_QQBOT_SECRET"):
                config.validate_credentials()
            with self.assertRaisesRegex(ValueError, "TIFFANY_QQBOT_SECRET"):
                create_adapter(AdapterConfig("qqofficial_websocket", "qq_official", qqofficial=QQOfficialConfig("app")))

    @unittest.skipUnless(importlib.util.find_spec("aiohttp"), "requires qqofficial extra")
    def test_example_load_and_factory_have_no_io_or_registration(self):
        with patch.dict(os.environ, {"TIFFANY_QQBOT_SECRET": "test-secret"}):
            config = load_config("examples/Tiffany.qqofficial.toml")
            adapter = create_adapter(config.adapter)
        self.assertIsNone(adapter.runtime)
        self.assertIsNone(adapter.client._session)
        self.assertIsNone(adapter._runner)
        self.assertIsNone(config.adapter.websocket)
        self.assertEqual(adapter.client.max_inflight, config.adapter.qqofficial.max_inflight)
        self.assertNotIn("test-secret", repr(config))

    def test_invalid_protocol_options_are_rejected(self):
        for options in ({"workers": 0}, {"workers": True}, {"max_inflight": 0}, {"call_timeout": float("inf")}, {"sandbox": "false"}, {"app_id": 123}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                QQOfficialConfig(**({"app_id": "app"} | options))
        with self.assertRaisesRegex(ValueError, "requires"):
            create_adapter(AdapterConfig("qqofficial_websocket", "qq_official"))

    def test_base_imports_and_onebot_factory_do_not_import_aiohttp(self):
        code = """
import builtins
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == 'aiohttp' or name.startswith('aiohttp.'):
        raise AssertionError('base runtime imported optional QQ dependency')
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from adapters import create_adapter
from settings import load_config
create_adapter(load_config('examples/Tiffany.onebot.toml').adapter)
"""
        result = subprocess.run([sys.executable, "-B", "-c", code], cwd=Path(__file__).resolve().parents[3], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
