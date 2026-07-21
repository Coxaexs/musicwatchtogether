import asyncio
import json
import unittest

from aiohttp import ClientSession, CookieJar, web
from aiohttp.test_utils import TestServer

import watchtogether


class WatchWebsocketIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.room_id = "w-integration-room"
        self.token = "integration-token"
        watchtogether.tokens[self.token] = {
            "room": self.room_id,
            "name": "Integration",
            "created_at": watchtogether.time.time(),
        }
        app = web.Application()
        app.router.add_post("/watch/api/session", watchtogether.api_session)
        app.router.add_get("/watch/ws", watchtogether.ws_handler)
        self.server = TestServer(app)
        try:
            await self.server.start_server()
        except PermissionError:
            watchtogether.tokens.pop(self.token, None)
            self.skipTest("this sandbox does not permit local listener sockets")
        self.sessions = []
        self.sockets = []

    async def asyncTearDown(self):
        for socket in getattr(self, "sockets", []):
            await socket.close()
        for session in getattr(self, "sessions", []):
            await session.close()
        room = watchtogether.rooms.pop(self.room_id, None)
        if room:
            tasks = [task for task in (room.sync_task, room.reels_task) if task]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        watchtogether.tokens.pop(self.token, None)
        if getattr(self, "server", None):
            await self.server.close()

    async def join(self, name):
        session = ClientSession(cookie_jar=CookieJar(unsafe=True))
        self.sessions.append(session)
        response = await session.post(
            self.server.make_url("/watch/api/session"),
            json={"room": self.room_id, "token": self.token},
        )
        self.assertEqual(response.status, 200)
        await response.read()
        socket = await session.ws_connect(
            self.server.make_url(f"/watch/ws?room={self.room_id}")
        )
        self.sockets.append(socket)
        await socket.send_json({"t": "join", "name": name})
        welcome = await self.receive_until(socket, lambda value: value.get("t") == "welcome")
        state = await self.receive_until(socket, lambda value: value.get("t") == "state")
        return socket, welcome["id"], state

    async def receive_until(self, socket, predicate):
        async def receive():
            while True:
                message = await socket.receive()
                if message.type == web.WSMsgType.TEXT:
                    value = json.loads(message.data)
                    if predicate(value):
                        return value
                elif message.type in (web.WSMsgType.CLOSE, web.WSMsgType.CLOSED,
                                       web.WSMsgType.ERROR):
                    self.fail("websocket closed before expected message")

        return await asyncio.wait_for(receive(), timeout=3)

    async def test_roles_gate_playback_and_host_can_promote(self):
        host_socket, host_id, host_state = await self.join("Host")
        viewer_socket, viewer_id, viewer_state = await self.join("Viewer")
        self.assertEqual(host_state["participants"][0]["role"], "host")
        viewer = next(item for item in viewer_state["participants"] if item["id"] == viewer_id)
        self.assertEqual(viewer["role"], "viewer")

        await viewer_socket.send_json({"t": "play", "pos": 12})
        await asyncio.sleep(0.1)
        room = watchtogether.rooms[self.room_id]
        self.assertFalse(room.playing)
        self.assertTrue(
            any("require a moderator" in item["text"] for item in room.chat),
            list(room.chat),
        )
        denial = await self.receive_until(
            viewer_socket,
            lambda value: value.get("t") == "chat" and "require a moderator" in value.get("text", ""),
        )
        self.assertIn("playback controls", denial["text"])
        self.assertFalse(room.playing)

        await host_socket.send_json({"t": "role_set", "id": viewer_id, "role": "moderator"})
        promoted = await self.receive_until(
            viewer_socket,
            lambda value: value.get("t") == "state" and any(
                item["id"] == viewer_id and item["role"] == "moderator"
                for item in value.get("participants", [])
            ),
        )
        self.assertTrue(any(item["id"] == host_id for item in promoted["participants"]))

        await viewer_socket.send_json({"t": "play", "pos": 12})
        sync = await self.receive_until(
            host_socket,
            lambda value: value.get("t") == "sync" and value.get("action") == "play",
        )
        self.assertEqual(sync["by"], "Viewer")


if __name__ == "__main__":
    unittest.main()
