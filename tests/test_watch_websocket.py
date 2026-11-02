import asyncio
import json
import unittest
from unittest import mock

from aiohttp import ClientSession, WSMsgType, web
from aiohttp.test_utils import TestServer

import watchtogether


class Client:
    def __init__(self, ws):
        self.ws = ws
        self.seen = []

    async def recv(self, predicate, timeout=3):
        for message in self.seen:
            if predicate(message):
                self.seen.remove(message)
                return message
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            left = deadline - asyncio.get_running_loop().time()
            if left <= 0:
                raise AssertionError("no matching message")
            msg = await self.ws.receive(timeout=left)
            if msg.type != WSMsgType.TEXT:
                raise AssertionError(f"socket closed: {msg.type} {msg.data}")
            data = json.loads(msg.data)
            if predicate(data):
                return data
            self.seen.append(data)

    async def send(self, **message):
        await self.ws.send_json(message)


class WatchWebsocketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.touch = mock.patch.object(watchtogether._Doc, "touch")
        self.touch.start()
        self.saved = {d.key: json.loads(json.dumps(d.data)) for d in self.docs()}
        app = web.Application()
        app.router.add_post("/watch/api/session", watchtogether.api_session)
        app.router.add_get("/watch/ws", watchtogether.ws_handler)
        self.server = TestServer(app)
        await self.server.start_server()
        self.http = ClientSession()
        self.sockets = []

    async def asyncTearDown(self):
        for ws in self.sockets:
            await ws.close()
        await self.http.close()
        await self.server.close()
        for room_id in [r for r in watchtogether.rooms if r.endswith("ws-unit")]:
            room = watchtogether.rooms.pop(room_id)
            for task in (room.sync_task, room.reels_task):
                if task:
                    task.cancel()
        for doc in self.docs():
            doc.data.clear()
            doc.data.update(self.saved[doc.key])
        self.touch.stop()

    @staticmethod
    def docs():
        return (watchtogether.sessions, watchtogether.settings_doc, watchtogether.mods_doc,
                watchtogether.people_doc, watchtogether.feeds_doc)

    async def join(self, uid, name, mode="watch", host=False, moderator=False):
        issued = watchtogether.issue_ticket({
            "mode": mode, "channel_id": "ws-unit", "room_name": "Voice",
            "user": {"id": uid, "name": name}, "host": host, "moderator": moderator,
        })
        async with self.http.post(self.server.make_url("/watch/api/session"),
                                  json={"room": issued["room"], "ticket": issued["ticket"]}) as r:
            self.assertEqual(r.status, 200)
            sid = (await r.json())["session"]
        ws = await self.http.ws_connect(self.server.make_url(f"/watch/ws?room={issued['room']}"))
        self.sockets.append(ws)
        await ws.send_json({"t": "hello", "session": sid})
        client = Client(ws)
        welcome = await client.recv(lambda m: m["t"] == "welcome")
        client.you = welcome["you"]
        client.room = issued["room"]
        return client

    async def test_without_a_session_the_socket_is_closed(self):
        ws = await self.http.ws_connect(self.server.make_url("/watch/ws?room=wh_ws-unit"))
        self.sockets.append(ws)
        await ws.send_json({"t": "hello", "session": "nope"})
        msg = await ws.receive(timeout=3)
        self.assertEqual(msg.type, WSMsgType.CLOSE)
        self.assertEqual(msg.data, 4001)

    async def test_roles_follow_hoffle_and_the_host_can_lock_playback(self):
        host = await self.join("h", "Hana", host=True)
        mod = await self.join("m", "Mo", moderator=True)
        member = await self.join("x", "Xi")
        self.assertEqual([host.you["role"], mod.you["role"], member.you["role"]],
                         ["host", "moderator", "member"])
        room = watchtogether.rooms[host.room]
        room.queue.append(watchtogether._new_item("https://example.com/v", "V", status="ready",
                                                  file="v.mp4", duration=100))
        room.index = 0
        await member.send(t="play", pos=3)
        sync = await host.recv(lambda m: m["t"] == "sync" and m.get("action") == "play")
        self.assertEqual(sync["by"]["id"], "x")
        self.assertTrue(sync["playing"])

        await mod.send(t="settings", control_policy="managers")
        await member.recv(lambda m: m["t"] == "state" and m["settings"]["control_policy"] == "managers")
        await member.send(t="pause", pos=5)
        denied = await member.recv(lambda m: m["t"] == "notice" and m.get("kind") == "error")
        self.assertIn("locked", denied["text"])
        self.assertTrue(room.playing)
        await member.send(t="settings", control_policy="everyone")
        await member.recv(lambda m: m["t"] == "notice" and m.get("kind") == "error")
        self.assertEqual(watchtogether.get_settings(room.id)["control_policy"], "managers")

        await host.send(t="role", id="x", role="moderator")
        state = await member.recv(lambda m: m["t"] == "state" and any(
            p["id"] == "x" and p["role"] == "moderator" for p in m["members"]))
        self.assertTrue(state)

    async def test_vote_to_skip(self):
        a = await self.join("a", "Ana", host=True)
        b = await self.join("b", "Ben")
        c = await self.join("c", "Cy")
        room = watchtogether.rooms[a.room]
        for name in ("one", "two"):
            room.queue.append(watchtogether._new_item(f"https://example.com/{name}", name,
                                                      status="ready", file=f"{name}.mp4"))
        room.index = 0
        watchtogether.update_settings(room.id, skip_policy="vote", vote_threshold=0.5)
        await b.send(t="skip")
        vote = await c.recv(lambda m: m["t"] == "notice" and "voted" in m["text"])
        self.assertIn("(1/2)", vote["text"])
        self.assertEqual(room.index, 0)
        await c.send(t="skip")
        await a.recv(lambda m: m["t"] == "state" and m["index"] == 1)

    async def test_reels_like_less_and_swipe_teach_the_right_people(self):
        with mock.patch.object(watchtogether, "_reels_topup", new=mock.AsyncMock()), \
                mock.patch.object(watchtogether, "_ensure_reels_loop"), \
                mock.patch.object(watchtogether.os.path, "isfile", return_value=True):
            ann = await self.join("ann", "Ann", mode="reels")
            bob = await self.join("bob", "Bob", mode="reels")
            room = watchtogether.rooms[ann.room]
            for i in range(3):
                item = watchtogether._new_item(f"https://youtube.com/shorts/{i}", f"r{i}", status="ready",
                                               file=f"r{i}.mp4", duration=30, arm=f"tag:t{i}",
                                               channel_id=f"UC{i}")
                item["tags_w"] = {f"t{i}": 1.0}
                room.queue.append(item)
            watchtogether._move_to(room, 0)

            await bob.send(t="like")
            heart = await ann.recv(lambda m: m["t"] == "heart")
            self.assertEqual((heart["likes"], heart["by"]["id"]), (1, "bob"))
            self.assertGreater(watchtogether.people_doc.data["bob"]["tags"]["t0"], 0)
            self.assertNotIn("t0", (watchtogether.people_doc.data.get("ann") or {}).get("tags", {}))

            await ann.send(t="swipe", dir=1)       # two seconds in: a fast skip, by Ann
            await bob.recv(lambda m: m["t"] == "swiped")
            self.assertEqual(room.index, 1)
            self.assertLess(watchtogether.people_doc.data["ann"]["tags"]["t0"], 0)
            self.assertGreater(room.feed["arms"]["tag:t0"][0], 0)   # Bob's like still counts

            await bob.send(t="less")
            await ann.recv(lambda m: m["t"] == "state" and m["index"] == 2)
            self.assertLess(watchtogether.people_doc.data["bob"]["tags"]["t1"], 0)
            self.assertEqual(room.feed["arms"]["tag:t1"][1], 2.0)

            await ann.send(t="swipe", dir=-1)
            await bob.recv(lambda m: m["t"] == "state" and m["index"] == 1)


if __name__ == "__main__":
    unittest.main()
