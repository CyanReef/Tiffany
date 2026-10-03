import asyncio
import io
import logging
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from deployment.onboarding import Binding, scan_binding, show_qr


class OnboardingTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_validates_result_without_outputting_secret(self):
        async def start(**kwargs):
            self.assertEqual(kwargs["poll_timeout"], 300)
            self.assertEqual(kwargs["source"], "tiffany")
            kwargs["on_qr_ready"]("https://q.qq.com/scan")
            return SimpleNamespace(app_id="app", client_secret="sensitive", user_openid="openid")
        output = io.StringIO()
        with patch("sys.stdout", output):
            binding = await scan_binding(start=start, display=lambda url: print(url))
        self.assertEqual(binding.app_secret, "sensitive")
        self.assertNotIn("sensitive", output.getvalue() + repr(binding))

    async def test_expiry_refreshes_at_most_three_times(self):
        class Expired(Exception):
            pass
        start = AsyncMock(side_effect=Expired)
        with patch("sys.stdout", io.StringIO()):
            with self.assertRaises(ValueError):
                await scan_binding(start=start, expired_error=Expired)
        self.assertEqual(start.await_count, 4)

    async def test_malformed_fields_and_api_errors_hide_response_secrets(self):
        cases = [SimpleNamespace(app_id="", client_secret="sensitive", user_openid="openid"),
                 SimpleNamespace(app_id="app", client_secret="sensitive", user_openid=""),
                 SimpleNamespace(app_id="app", client_secret=None, user_openid="openid"),
                 ValueError("sensitive response body")]
        for case in cases:
            with self.subTest(case=type(case).__name__):
                start = AsyncMock(side_effect=case) if isinstance(case, Exception) else AsyncMock(return_value=case)
                with self.assertRaises(ValueError) as result:
                    await scan_binding(start=start)
                self.assertNotIn("sensitive", str(result.exception))

    async def test_cancellation_propagates_without_returning_credentials(self):
        with self.assertRaises(asyncio.CancelledError):
            await scan_binding(start=AsyncMock(side_effect=asyncio.CancelledError))

    async def test_timeout_is_bounded_and_sdk_child_logs_are_suppressed(self):
        async def start(**kwargs):
            logging.getLogger("qqbot_agent_sdk.onboard").error("sensitive SDK body")
            await asyncio.Event().wait()
        with patch("sys.stderr", io.StringIO()) as output:
            with self.assertRaises(ValueError):
                await scan_binding(start=start, timeout=0.01, refreshes=0)
        self.assertNotIn("sensitive", output.getvalue())

    def test_manual_binding_does_not_require_scanner_id(self):
        self.assertEqual(Binding("app", "secret").user_openid, "")

    def test_real_terminal_qr_includes_https_link_only(self):
        output = io.StringIO()
        show_qr("https://q.qq.com/qqbot/openclaw/connect.html?task_id=example", output)
        self.assertIn("https://q.qq.com/", output.getvalue())
