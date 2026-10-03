"""Official SDK onboarding only; all credential-bearing output stays hidden."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
import sys
from urllib.parse import urlparse


@dataclass(frozen=True, slots=True)
class Binding:
    app_id: str
    app_secret: str = field(repr=False)
    user_openid: str = ""

    def __post_init__(self):
        for value in (self.app_id, self.app_secret):
            if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
                raise ValueError("绑定结果缺少有效 AppID 或密钥")
        if not isinstance(self.user_openid, str) or any(ord(c) < 32 for c in self.user_openid):
            raise ValueError("绑定结果 OpenID 无效")


def show_qr(url: str, stream=None):
    stream = stream or sys.stdout
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "q.qq.com":
        raise ValueError("SDK 返回的扫码链接无效")
    import qrcode
    qr = qrcode.QRCode(border=2)
    qr.add_data(url)
    qr.make(fit=True)
    qr.print_ascii(out=stream, invert=True)
    print(f"请使用手机 QQ 扫码。备用链接：{url}", file=stream, flush=True)


async def scan_binding(*, start=None, expired_error=None, display=None, timeout=300.0, refreshes=3) -> Binding:
    if start is None:
        from qqbot_agent_sdk.onboard import start_onboard, OnboardExpiredError
        start = start_onboard
        expired_error = OnboardExpiredError
    expired_error = expired_error or TimeoutError
    display = display or show_qr
    sdk_logger = logging.getLogger("qqbot_agent_sdk")
    previous = sdk_logger.disabled, sdk_logger.propagate
    # SDK logs binding task IDs and scanner IDs. Do not persist them or error bodies.
    sdk_logger.disabled = True
    sdk_logger.propagate = False
    sink = logging.NullHandler()
    sdk_logger.addHandler(sink)
    try:
        for attempt in range(refreshes + 1):
            try:
                async with asyncio.timeout(timeout):
                    result = await start(on_qr_ready=display, poll_timeout=timeout, source="tiffany")
                binding = Binding(result.app_id, result.client_secret, result.user_openid)
                if not binding.user_openid.strip():
                    raise ValueError("绑定结果缺少扫码者 OpenID")
                return binding
            except (expired_error, TimeoutError):
                if attempt == refreshes:
                    raise ValueError("二维码已过期或等待超时，请重试或手动配置。") from None
                print("二维码过期，正在重新生成。", flush=True)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # Never echo SDK exception messages: a malformed response may contain credentials.
                raise ValueError(f"扫码绑定失败（{type(error).__name__}），可重试或手动配置。") from None
        raise AssertionError("unreachable")
    finally:
        sdk_logger.removeHandler(sink)
        sdk_logger.disabled, sdk_logger.propagate = previous
