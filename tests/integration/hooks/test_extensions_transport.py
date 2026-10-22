import asyncio
import json
from pathlib import Path
import tempfile
import threading
import unittest

from websockets.asyncio.client import connect
from adapters.OneBotWebSocketAdapter import OneBotWebSocketAdapter
from adapters.QQOfficialWebSocketAdapter import QQOfficialWebSocketAdapter
from clients.QQOfficialClient import QQOfficialClient
from core import APP_PATHS, Bot, SchedulerPolicy
from hooks import install_extensions
from hooks.extensions.notes import STORE
from shared.extensions import ExtensionsConfig
from shared.paths import RuntimePaths
from tests.support.qqofficial import QQTestServer, eventually, message


CONFIG = ExtensionsConfig.parse({"enabled": ["commands", "ping", "help", "permissions", "ratelimit", "notes"]})


class ExtensionTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_busy_notes_keep_onebot_control_responses_and_drain_alive(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = Bot(scheduler=SchedulerPolicy(max_events=3, max_concurrency=1))
            bot.service(APP_PATHS, RuntimePaths(Path(directory)))
            await install_extensions(bot, CONFIG)
            adapter = bot.install(OneBotWebSocketAdapter("127.0.0.1", 0, "onebot", call_timeout=2))
            gate, entered = threading.Event(), threading.Event()
            try:
                await bot.start()
                store = bot.services.get(STORE)
                original = store._set
                def slow(*args):
                    entered.set()
                    gate.wait(4)
                    return original(*args)
                store._set = slow
                port = adapter._server.sockets[0].getsockname()[1]
                async with connect(f"ws://127.0.0.1:{port}") as ws:
                    for index in range(8):
                        await ws.send(json.dumps({"post_type": "message", "message_type": "group", "group_id": 7,
                            "self_id": 99, "user_id": 1, "message_id": index, "message": "/note set saved"}))
                    await eventually(lambda: entered.is_set() and bot.runtime.scheduler.queued == 2)
                    await asyncio.wait_for(await ws.ping(), 1)
                    call = asyncio.create_task(adapter.active_client.call("get_status"))
                    request = json.loads(await asyncio.wait_for(ws.recv(), 1))
                    await ws.send(json.dumps({"echo": request["echo"], "status": "ok", "retcode": 0, "data": {}}))
                    self.assertTrue((await asyncio.wait_for(call, 1)).ok)
                    drain = asyncio.create_task(bot.stop("drain"))
                    await asyncio.sleep(.01)
                    self.assertFalse(drain.done())
                    gate.set()
                    for _ in range(3):
                        request = json.loads(await asyncio.wait_for(ws.recv(), 2))
                        self.assertEqual(request["params"]["message"], "保存成功")
                        await ws.send(json.dumps({"echo": request["echo"], "status": "ok", "retcode": 0, "data": {}}))
                    self.assertTrue((await asyncio.wait_for(drain, 3)).successful)
            finally:
                gate.set()
                await bot.stop("abort")

    async def bot(self, directory):
        bot = Bot()
        bot.service(APP_PATHS, RuntimePaths(Path(directory)))
        await install_extensions(bot, CONFIG)
        self.addAsyncCleanup(bot.stop, "abort")
        return bot

    async def test_onebot_notes_reply_after_commit_and_recover_on_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            for commands, expected in ((["/note set remembered", "/note get", "/ping"], ["保存成功", "remembered", "pong"]),
                                       (["/note get"], ["remembered"])):
                bot = await self.bot(directory)
                adapter = bot.install(OneBotWebSocketAdapter("127.0.0.1", 0, "onebot", call_timeout=2))
                await bot.start()
                port = adapter._server.sockets[0].getsockname()[1]
                async with connect(f"ws://127.0.0.1:{port}") as ws:
                    for index, (command, reply) in enumerate(zip(commands, expected)):
                        await ws.send(json.dumps({"post_type": "message", "message_type": "group", "group_id": 7,
                            "self_id": 99, "user_id": 1, "message_id": index, "message": command}))
                        request = json.loads(await asyncio.wait_for(ws.recv(), 2))
                        self.assertEqual(request["params"]["message"], reply)
                        await ws.send(json.dumps({"echo": request["echo"], "status": "ok", "retcode": 0, "data": {}}))
                    await eventually(lambda: bot.runtime.scheduler.idle)
                report = await bot.stop()
                self.assertTrue(report.successful)

    async def test_qq_group_private_isolation_and_shared_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            peer = await QQTestServer().start()
            self.addAsyncCleanup(peer.close)
            bot = await self.bot(directory)
            client = QQOfficialClient("app", "test-secret", api_base=peer.api_base, token_url=peer.token_url)
            adapter = bot.install(QQOfficialWebSocketAdapter("app", "test-secret", client=client))
            await bot.start()
            ws = await asyncio.wait_for(peer.connections.get(), 2)
            for seq, scene, text in ((2, "group", "/note set group-note"), (3, "group", "/note get"),
                                      (4, "private", "/note get"), (5, "private", "ping")):
                await ws.send_json(message(scene, seq=seq, content=text, target="same-openid"))
                await eventually(lambda: len(peer.api_calls) == seq - 1 and bot.runtime.scheduler.idle)
            self.assertEqual([row[1]["content"] for row in peer.api_calls], ["保存成功", "group-note", "暂无笔记", "pong"])
            self.assertEqual(peer.api_calls[1][0], "/v2/groups/same-openid/messages")
            self.assertEqual(peer.api_calls[2][0], "/v2/users/same-openid/messages")
            self.assertTrue((await bot.stop()).successful)
