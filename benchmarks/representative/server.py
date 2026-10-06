"""Shared I/O delay service with high-resolution blocking waits off the loop.

Windows Proactor timers can overshoot by about 10 ms at idle. Python 3.11+
time.sleep uses a high-resolution waitable timer; completed worker waits wake
the event loop immediately. The pool is prestarted so clients share the same
ready service capacity. Measured headers verify every real delay.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import importlib.metadata
import json
import sys
import threading
from time import get_clock_info, perf_counter_ns, sleep


def wait_delay():
    start = perf_counter_ns()
    while (remaining := .005 - (perf_counter_ns() - start) / 1e9) > 0:
        sleep(remaining)
    return perf_counter_ns() - start


async def serve():
    from aiohttp import web
    pool = ThreadPoolExecutor(max_workers=64, thread_name_prefix="shared-delay")
    barrier = threading.Barrier(65, timeout=15)
    initial = [pool.submit(barrier.wait) for _ in range(64)]
    barrier.wait()
    for future in initial:
        future.result()
    loop = asyncio.get_running_loop()
    loop.set_default_executor(pool)

    async def delay(request):
        duration = await asyncio.to_thread(wait_delay)
        return web.Response(text="hello", headers={"X-Service-Delay-Ns": str(duration)})

    app = web.Application()
    app.router.add_get("/delay", delay)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    try:
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        print(json.dumps({"url": f"http://127.0.0.1:{port}/delay", "runtime": sys.version,
                          "monotonic_clock": vars(get_clock_info("monotonic")),
                          "aiohttp": importlib.metadata.version("aiohttp"),
                          "wait": "time.sleep high-resolution waitable timer off event loop",
                          "prestarted_threads": 64}), flush=True)
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        pool.shutdown(wait=True)


if __name__ == "__main__":
    asyncio.run(serve())
