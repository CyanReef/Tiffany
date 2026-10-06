"""Native message paths for the expanded offline comparison.

No replacement dispatcher: each engine retains its framework parser, dependency
injection and native handler scheduling. Network platform login is not started.
"""
from __future__ import annotations

import asyncio

from benchmarks.compare import TiffanyEngine


class TiffanyEngine16(TiffanyEngine):
    async def setup(self, case, consumer):
        await super().setup(case, consumer)
        if case.get("sessions", 1) == 16:
            self.bot.runtime.scheduler.configure_adapter("benchmark", active_limit=16)
            if self.bot.runtime.scheduler.global_limit != 16:
                raise AssertionError("unexpected global concurrency limit")


class GraiaEngine:
    app = None

    async def setup(self, case, consumer):
        from graia.ariadne.app import Ariadne
        from graia.ariadne.connection.config import config, HttpClientConfig
        from graia.ariadne.connection.util import build_event
        from graia.ariadne.event.message import FriendMessage
        from graia.ariadne.event.lifecycle import AccountLaunch
        from graia.ariadne.message.chain import MessageChain
        from loguru import logger

        logger.remove()
        logger.add(__import__("sys").stderr, level="ERROR")
        if type(self).app is None:
            Ariadne.config(install_log=False)
            type(self).app = Ariadne(config(12345, "offline", HttpClientConfig()))
            # Real native cache/service interfaces, without connecting to Mirai.
            Ariadne._patch_launch_manager()
        self.app = type(self).app
        self.build_event = build_event
        self.listeners = []
        for index in range(case["matching"] + case["unmatched"]):
            if index < case["matching"]:
                async def handler(message):
                    await consumer.accept(message.display)
                handler.__annotations__["message"] = MessageChain
                event_type = FriendMessage
            else:
                async def handler():
                    consumer.unmatched_calls += 1
                event_type = AccountLaunch
            self.app.broadcast.receiver(event_type)(handler)
            self.listeners.append(self.app.broadcast.getListener(handler))

        # _event_hook includes Ariadne's context and native message/friend cache.
        # It schedules Broadcast without awaiting it. Observe the returned task
        # to measure through handler completion; the actual dispatcher is intact.
        self.pending = {}
        self.post_event = self.app.broadcast.postEvent
        def observed_post(event, *args, **kwargs):
            task = self.post_event(event, *args, **kwargs)
            self.pending[id(event)] = task
            return task
        self.app.broadcast.postEvent = observed_post

    async def emit(self, index, session):
        event = self.build_event({
            "type": "FriendMessage",
            "messageChain": [{"type": "Source", "id": index, "time": 1700000000},
                             {"type": "Plain", "text": "hello"}],
            "sender": {"id": 20000 + session, "nickname": "benchmark", "remark": ""},
        })
        await self.app._event_hook(event)
        await self.pending.pop(id(event))

    async def close(self):
        from graia.amnesia.builtins.memcache import Memcache
        self.app.broadcast.postEvent = self.post_event
        for listener in self.listeners:
            self.app.broadcast.removeListener(listener)
        if self.pending:
            raise AssertionError("Graia left unfinished observed events")
        await self.app.launch_manager.get_interface(Memcache).clear()


class EntariEngine:
    app = None

    async def setup(self, case, consumer):
        from arclet.entari import Entari, Session
        from arclet.entari.event.base import FriendAddedEvent
        from arclet.entari.session import EntariProtocol
        from arclet import letoderea as le
        from satori import Event, Login, User
        from satori.client.account import Account
        from satori.client.config import WebsocketsInfo

        if type(self).app is None:
            type(self).app = Entari(log_level="ERROR", gen_schema=False)
        self.app = type(self).app
        self.Event = Event
        self.account = Account(Login(platform="benchmark", user=User(id="12345")),
                               WebsocketsInfo(), [], EntariProtocol)
        self.subscribers = []
        for index in range(case["matching"] + case["unmatched"]):
            if index < case["matching"]:
                async def handler(session):
                    await consumer.accept(session.content)
                handler.__annotations__["session"] = Session
                sub = self.app.on_message()(handler)
            else:
                async def handler():
                    consumer.unmatched_calls += 1
                sub = le.on(FriendAddedEvent)(handler)
            self.subscribers.append(sub)

    async def emit(self, index, session):
        event = self.Event.parse({
            "id": index, "type": "message-created", "timestamp": 1700000000000,
            "login": {"sn": 0, "platform": "benchmark", "user": {"id": "12345"},
                      "status": 1, "adapter": "benchmark"},
            "channel": {"id": f"private:{20000 + session}", "type": 1},
            "user": {"id": str(20000 + session), "name": "benchmark"},
            "message": {"id": str(index), "content": "hello"},
        })
        await self.app.handle_event(self.account, event)

    async def close(self):
        for subscriber in self.subscribers:
            subscriber.dispose()
        await asyncio.sleep(0)
