"""Server composition: config, services, readiness and orderly signals."""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import errno

from adapters import create_adapter
from core import APP_PATHS, Bot, OpenMetricsExporter, RuntimeState, ServiceKey
from deployment.console import configure_utf8_output
from deployment.credentials import CredentialStore
from deployment.locking import InstanceLock
from deployment.logging import setup_logging
from deployment.paths import RuntimePaths
from hooks import register_hooks
from settings import AppConfig, load_config

logger = logging.getLogger(__name__)
MONITORING = ServiceKey[OpenMetricsExporter]("application.monitoring")


def failure_exit_code(error: BaseException) -> int:
    if (isinstance(error, ImportError)
            or type(error).__name__ == "MissingOptionalDependencyError"
            or isinstance(error, OSError) and error.errno in (errno.EACCES, errno.EADDRINUSE)):
        return 2
    if type(error).__name__ == "QQOfficialGatewayError":
        return 3
    if type(error).__name__ == "QQOfficialAPIError":
        status = getattr(error, "status", 0)
        if status < 500 and status != 429:
            return 3
    if type(error).__name__ == "ShutdownIncompleteError":
        return 5
    return 4


class TiffanyApplication:
    @staticmethod
    def run(argv=None) -> int:
        configure_utf8_output()
        parser = argparse.ArgumentParser(description="Tiffany application (use start.sh for supervision)")
        parser.add_argument("--home")
        args = parser.parse_args(argv)
        paths = RuntimePaths.resolve(args.home)
        try:
            paths.initialize()
            with InstanceLock(paths.run / "application.lock"):
                config = load_config(paths.config)
                store = CredentialStore(paths)
                secrets = (*store.secrets(), *config.validate_credentials(store.resolve))
                setup_logging(paths.logs / "tiffany.log", level=config.logging.level,
                              max_bytes=config.logging.max_bytes, backups=config.logging.backups, secrets=secrets)
                return asyncio.run(TiffanyApplication._run(paths, config))
        except KeyboardInterrupt:
            return 0
        except (ValueError, OSError, ImportError) as error:
            logger.error("配置或环境错误：%s", error)
            return 2
        except Exception as error:
            logger.exception("应用未处理异常")
            return failure_exit_code(error)

    @staticmethod
    async def _run(paths: RuntimePaths, config: AppConfig) -> int:
        bot = TiffanyApplication._create_bot(config.scheduler)
        bot.service(APP_PATHS, paths)
        adapter = bot.install(create_adapter(config.adapter, credential_resolver=CredentialStore(paths).resolve))
        monitor = config.monitoring
        bot.service(MONITORING, OpenMetricsExporter(
            enabled=monitor.enabled, host=monitor.host, port=monitor.port,
            allow_remote=monitor.allow_remote,
            live=lambda: bot.runtime.state in (RuntimeState.STARTING, RuntimeState.RUNNING),
            ready=lambda: (bot.runtime.state == RuntimeState.RUNNING and adapter.ready
                           and bot.runtime.scheduler.accepting),
        ))
        loop = asyncio.get_running_loop()
        stop_count = 0
        stop_tasks: set[asyncio.Task] = set()
        runner = asyncio.create_task(bot.run_async(), name="application:run")

        def stop(signum):
            nonlocal stop_count
            stop_count += 1
            mode = "drain" if stop_count == 1 else "abort"
            logger.info("收到信号 %s，请求 %s", signum, mode)
            if bot.runtime.state in (RuntimeState.NEW, RuntimeState.SETTING_UP,
                                     RuntimeState.SETUP, RuntimeState.STARTING):
                # Startup cancellation owns its rollback; avoid racing a separate close.
                if stop_count == 1:
                    runner.cancel()
                return
            task = asyncio.create_task(bot.stop(mode), name=f"application:stop:{mode}")
            stop_tasks.add(task)
            task.add_done_callback(stop_tasks.discard)

        installed = {}
        for sig in (signal.SIGINT, signal.SIGTERM):
            installed[sig] = signal.getsignal(sig)
            try:
                loop.add_signal_handler(sig, stop, sig)
            except NotImplementedError:
                signal.signal(sig, lambda signum, frame: loop.call_soon_threadsafe(stop, signum))
        if hasattr(signal, "SIGBREAK"):
            sig = signal.SIGBREAK
            installed[sig] = signal.getsignal(sig)
            signal.signal(sig, lambda signum, frame: loop.call_soon_threadsafe(stop, signum))
        logger.info("启动 %s，接入=%s，运行目录=%s", config.bot.name, config.adapter.type, paths.home)
        code = 0
        try:
            await runner
        except asyncio.CancelledError:
            if not stop_count:
                raise
        except Exception as error:
            logger.error("运行失败 (%s)：%s", type(error).__name__, error)
            code = failure_exit_code(error)
            if (code == 4 and bot.runtime.startup_failed
                    and isinstance(error, (ValueError, TypeError, LookupError))):
                code = 2
            if code == 3:
                logger.error("QQ 永久鉴权或权限错误；保留凭据，请检查权限或执行 bash start.sh login。")
        finally:
            report = await bot.stop("abort" if stop_count > 1 else "drain")
            if stop_tasks:
                await asyncio.gather(*stop_tasks, return_exceptions=True)
            for sig, previous in installed.items():
                try:
                    loop.remove_signal_handler(sig)
                except NotImplementedError:
                    pass
                signal.signal(sig, previous)
            logger.info("关闭结果：successful=%s forced=%s abandoned=%s",
                        report.successful, report.forced, report.abandoned_tasks)
            if report.abandoned_tasks:
                # asyncio.run() otherwise waits forever for cancellation-resistant
                # tasks during its own shutdown. The server child must exit so the
                # supervisor can recover. Flush logs before ending the process.
                logger.error("取消后仍有任务未结束，以清理失败退出")
                logging.shutdown()
                os._exit(3 if code == 3 else 5)
            if not report.successful and code == 0:
                code = 5
        return code

    @staticmethod
    def _create_bot(scheduler=None) -> Bot:
        bot = Bot(scheduler=scheduler)
        register_hooks(bot)
        return bot
