"""Local QQ HTTP and WebSocket peers; never connect to the real platform."""

import asyncio
import json

from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer


def message(scene="group", *, seq=2, target="openid", message_id=None, content=" /ping "):
    author_key = "member_openid" if scene == "group" else "user_openid"
    data = {"id": message_id or f"message-{seq}", "content": content,
            "author": {author_key: target}, "attachments": [{"url": "https://unused.invalid/image"}]}
    if scene == "group":
        data["group_openid"] = target
    return {"op": 0, "s": seq, "t": "GROUP_AT_MESSAGE_CREATE" if scene == "group" else "C2C_MESSAGE_CREATE",
            "id": f"event-{seq}", "d": data}


class QQTestServer:
    def __init__(self):
        self.token_calls = 0
        self.token_response = None
        self.token_gate = None
        self.api_calls = []
        self.auth_payloads = []
        self.heartbeats = []
        self.heartbeat_interval = 40
        self.ack_heartbeat = True
        self.on_auth = None
        self.on_api = None
        self.connections = asyncio.Queue()
        self.reply_received = asyncio.Event()
        self.heartbeat_received = asyncio.Event()
        self.sockets = []
        app = web.Application()
        app.router.add_post("/app/getAppAccessToken", self._token)
        app.router.add_get("/gateway", self._gateway)
        app.router.add_get("/ws", self._websocket)
        app.router.add_route("*", "/{path:.*}", self._api)
        self.server = TestServer(app, shutdown_timeout=0.1)

    async def start(self):
        await self.server.start_server()
        self.api_base = str(self.server.make_url("/")).rstrip("/")
        self.token_url = self.api_base + "/app/getAppAccessToken"
        self.gateway_url = self.api_base.replace("http://", "ws://") + "/ws"
        return self

    async def close(self):
        for ws in self.sockets:
            await ws.close()
        await self.server.close()

    async def _token(self, request):
        await request.json()
        self.token_calls += 1
        if self.token_gate is not None:
            await self.token_gate.wait()
        return web.json_response(self.token_response or {"access_token": f"token-{self.token_calls}", "expires_in": "7200"})

    async def _gateway(self, request):
        return web.json_response({"url": self.gateway_url})

    async def _api(self, request):
        body = await request.json() if request.can_read_body else None
        self.api_calls.append((request.path, body, request.headers.get("Authorization")))
        if self.on_api is not None:
            return await self.on_api(request, body)
        self.reply_received.set()
        return web.json_response({"id": "sent-message", "timestamp": 1})

    @staticmethod
    async def ready(ws, *, seq=1):
        await ws.send_json({"op": 0, "s": seq, "t": "READY", "d": {
            "session_id": "gateway-session", "user": {"id": "bot-user"},
        }})

    async def _websocket(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.sockets.append(ws)
        await ws.send_json({"op": 10, "d": {"heartbeat_interval": self.heartbeat_interval}})
        try:
            auth = await ws.receive_json()
            self.auth_payloads.append(auth)
            index = len(self.auth_payloads)
            if self.on_auth is not None:
                await self.on_auth(ws, auth, index)
            elif auth["op"] == 6:
                await ws.send_json({"op": 0, "s": auth["d"]["seq"], "t": "RESUMED", "d": ""})
            else:
                await self.ready(ws)
            await self.connections.put(ws)
            async for frame in ws:
                if frame.type != WSMsgType.TEXT:
                    continue
                payload = json.loads(frame.data)
                if payload.get("op") == 1:
                    self.heartbeats.append(payload)
                    self.heartbeat_received.set()
                    if self.ack_heartbeat:
                        await ws.send_json({"op": 11})
        except (ConnectionError, RuntimeError, TypeError, ValueError):
            if not ws.closed:
                raise
        return ws


async def eventually(predicate, timeout=2):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)
