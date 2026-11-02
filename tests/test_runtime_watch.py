import asyncio
import json
import unittest
from unittest import mock

import reels_brain as brain
import runtime
import watchtogether
import webui


class FakeSocket:
    def __init__(self):
        self.messages = []

    async def send_str(self, value):
        self.messages.append(json.loads(value))


def info(uid, name, host=False, moderator=False):
    return {"user": {"id": uid, "name": name}, "host": host, "moderator": moderator}


class QuietDocs(unittest.IsolatedAsyncioTestCase):
    """Keep the tests' rooms and tastes out of the bot's real state."""

    def setUp(self):
        self.touch = mock.patch.object(watchtogether._Doc, "touch")
        self.touch.start()
        self.saved = {doc.key: json.loads(json.dumps(doc.data)) for doc in self.docs()}

    def tearDown(self):
        for doc in self.docs():
            doc.data.clear()
            doc.data.update(self.saved[doc.key])
        self.touch.stop()

    @staticmethod
    def docs():
        return (watchtogether.sessions, watchtogether.settings_doc, watchtogether.playlists_doc,
                watchtogether.mods_doc, watchtogether.people_doc, watchtogether.feeds_doc)


class RoomRoleTests(QuietDocs):
    def test_roles_come_from_hoffle_and_room_moderators(self):
        room = watchtogether.Room("wh_role-unit", "Roles")
        host = room.join(FakeSocket(), info("h", "Host", host=True))
        mod = room.join(FakeSocket(), info("m", "Mod", moderator=True))
        member = room.join(FakeSocket(), info("x", "Member"))
        self.assertEqual([host["role"], mod["role"], member["role"]], ["host", "moderator", "member"])
        watchtogether.mods_doc.data["wh_role-unit"] = ["x"]
        again = room.join(FakeSocket(), info("x", "Member"))
        self.assertEqual(again["role"], "moderator")
        state = watchtogether._room_state(room)
        self.assertEqual([m["id"] for m in state["members"]], ["h", "m", "x"])  # one entry per person


class PlaylistTests(QuietDocs):
    async def test_save_load_delete_follow_owner(self):
        room = watchtogether.Room("wh_playlist-unit", "Playlists")
        owner = room.join(FakeSocket(), info("o", "Owner"))
        other = room.join(FakeSocket(), info("p", "Other"))
        room.queue = [
            {"url": "https://example.com/one", "title": "One", "duration": 12},
            {"url": "not-a-url", "title": "Ignored"},
        ]
        await watchtogether._save_room_playlist(room, " Road Trip ", owner)
        playlist = watchtogether.playlists_doc.data["wh_playlist-unit"]["Road Trip"]
        self.assertEqual([i["url"] for i in playlist["items"]], ["https://example.com/one"])
        self.assertEqual(playlist["owner_id"], "o")
        await watchtogether._delete_room_playlist(room, "Road Trip", other)
        self.assertIn("Road Trip", watchtogether.playlists_doc.data["wh_playlist-unit"])
        added = []

        async def add(_room, url, by, play_next=False):
            added.append((url, by["id"]))
        with mock.patch.object(watchtogether, "_add_query", side_effect=add):
            await watchtogether._load_room_playlist(room, "Road Trip", other)
        self.assertEqual(added, [("https://example.com/one", "p")])
        await watchtogether._delete_room_playlist(room, "Road Trip", owner)
        self.assertNotIn("wh_playlist-unit", watchtogether.playlists_doc.data)


class RuntimeTests(QuietDocs):
    async def test_registry_cancels_owned_tasks(self):
        registry = runtime.TaskRegistry("test")
        started = asyncio.Event()

        async def worker():
            started.set()
            await asyncio.Event().wait()

        registry.create(worker(), "worker")
        await started.wait()
        self.assertEqual(registry.active, 1)
        await registry.cancel_all(timeout=1)
        self.assertEqual(registry.active, 0)
        self.assertTrue(registry.closing)

    async def test_health_and_metrics_are_machine_readable(self):
        bot = mock.Mock()
        bot.is_ready.return_value = True
        response = await webui.WebUI(bot).health(None)
        payload = json.loads(response.text)
        self.assertEqual(payload["status"], "ok")
        self.assertIn("rooms", payload["watch"])
        self.assertIn("musicwatch_process_uptime_seconds", watchtogether.metrics_text())

    async def test_admin_snapshot_and_delete(self):
        response = await webui.WebUI(mock.Mock()).admin_index(None)
        self.assertIn("MusicWatch Admin", response.text)
        self.assertNotIn('href="/watch/"', response.text)
        room_id = "wh_admin-unit"
        room = watchtogether.Room(room_id, "Admin Room")
        socket = mock.AsyncMock()
        room.sockets[socket] = dict(info("a", "A")["user"], role="member", order=1)
        watchtogether.rooms[room_id] = room
        watchtogether.settings_doc.data[room_id] = {"quality": 360}
        watchtogether.playlists_doc.data[room_id] = {"Mix": {}}
        watchtogether.sessions.data["admin-sid"] = {"room": room_id, "user": {"id": "a"}, "created": 0}
        try:
            item = next(v for v in watchtogether.admin_snapshot() if v["id"] == room_id)
            self.assertTrue(item["active"])
            await watchtogether.admin_delete_room(room_id)
            self.assertNotIn(room_id, watchtogether.rooms)
            self.assertNotIn(room_id, watchtogether.settings_doc.data)
            self.assertNotIn(room_id, watchtogether.playlists_doc.data)
            self.assertNotIn("admin-sid", watchtogether.sessions.data)
            socket.close.assert_awaited_once_with(code=4004, message=b'room deleted by administrator')
        finally:
            watchtogether.rooms.pop(room_id, None)


class WatchQueueTests(QuietDocs):
    async def test_added_video_carries_on_after_the_queue_ran_out(self):
        room = watchtogether.Room("wh_end-unit", "End")
        room.join(FakeSocket(), info("a", "A"))
        last = watchtogether._new_item("https://example.com/a", "A", status="ready", file="a.mp4", duration=30)
        room.queue = [last]
        room.index = 0
        room.set_position(30, playing=False)
        new = watchtogether._new_item("https://example.com/b", "B", status="ready", file="b.mp4")
        room.queue.append(new)
        await watchtogether._after_ready(room, new)
        self.assertIs(room.current(), new)
        self.assertTrue(room.playing)
        self.assertLess(room.get_position(), 1)

    async def test_room_waiting_on_a_download_starts_it_from_the_top(self):
        room = watchtogether.Room("wh_wait-unit", "Wait")
        room.join(FakeSocket(), info("a", "A"))
        first = watchtogether._new_item("https://example.com/a", "A", status="ready", file="a.mp4")
        slow = watchtogether._new_item("https://example.com/b", "B", status="downloading")
        room.queue = [first, slow]
        watchtogether._move_to(room, 1)
        room.position = 25   # the download took a while
        slow["status"], slow["file"] = "ready", "b.mp4"
        await watchtogether._after_ready(room, slow)
        self.assertLess(room.get_position(), 1)


class ReelsFeedTests(QuietDocs):
    async def test_topup_fills_ahead_from_the_chosen_source(self):
        room = watchtogether.Room("rh_feed-unit", "Feed")
        room.join(FakeSocket(), info("ann", "Ann"))
        room.join(FakeSocket(), info("bob", "Bob"))
        watchtogether.people_doc.data["ann"] = {"tags": {"cats": 5.0, "kitten": 2.0}, "creators": {}}
        entries = [{"id": f"vid{i:08d}x", "title": f"Cat {i} #cats", "view_count": 1000 * i} for i in range(20)]
        spawned = []

        def discard(coroutine, name):
            spawned.append(name)
            coroutine.close()

        urls = []

        async def source(url):
            urls.append(url)
            return entries

        with mock.patch.object(watchtogether, "_source_entries", side_effect=source), \
                mock.patch.object(watchtogether, "_spawn", side_effect=discard), \
                mock.patch.object(brain, "EXPLORE_RATE", 0.0):
            await watchtogether._reels_topup_inner(room)
        self.assertEqual(len(room.queue), watchtogether.REELS_READY_AHEAD)
        self.assertEqual(len(spawned), watchtogether.REELS_READY_AHEAD)
        self.assertTrue(all(u.startswith("https://www.youtube.com/hashtag/") for u in urls))
        self.assertTrue(all(it["why"]["label"].startswith("#") for it in room.queue))
        self.assertEqual(room.queue[0]["why"]["for"], "Ann")
        for item in room.queue:
            self.assertIn(item["vid"], room.feed["seen"])
            self.assertTrue(item["url"].startswith("https://www.youtube.com/shorts/"))

    async def test_nothing_to_go_on_waits_for_the_seed_screen(self):
        room = watchtogether.Room("rh_empty-unit", "Empty")
        room.join(FakeSocket(), info("new", "New"))
        with mock.patch.object(watchtogether, "_source_entries") as source:
            await watchtogether._reels_topup_inner(room)
        source.assert_not_called()
        self.assertTrue(watchtogether._room_state(room)["feed"]["need_seed"])

    async def test_full_watch_teaches_everyone_present(self):
        room = watchtogether.Room("rh_learn-unit", "Learn")
        room.join(FakeSocket(), info("ann", "Ann"))
        room.join(FakeSocket(), info("bob", "Bob"))
        item = watchtogether._new_item("u", "t", status="ready", file="f.mp4", duration=10,
                                       arm="tag:cats", channel_id="UC9")
        item["tags_w"] = {"cats": 1.0}
        room.queue = [item]
        watchtogether._move_to(room, 0)
        room.position = 10
        watchtogether._conclude_reel(room, "ended")
        self.assertGreater(watchtogether.people_doc.data["ann"]["tags"]["cats"], 0)
        self.assertGreater(watchtogether.people_doc.data["bob"]["tags"]["cats"], 0)
        self.assertEqual(room.feed["arms"]["tag:cats"][0], 1.0)
        watchtogether._conclude_reel(room, "ended")   # once per visit
        self.assertEqual(room.feed["arms"]["tag:cats"][0], 1.0)


class CommandTests(QuietDocs):
    async def test_watch_add_and_reels_seed(self):
        spawned = []

        def discard(coroutine, name):
            spawned.append(name)
            coroutine.close()

        user = {"id": "cmd-user", "name": "Cee"}
        with mock.patch.object(watchtogether, "_spawn", side_effect=discard):
            result = await watchtogether.command({"mode": "watch", "channel_id": "cmd-room", "action": "add",
                                                  "q": "https://example.com/v", "user": user})
            self.assertTrue(result["ok"])
            self.assertTrue(any(name.startswith("add:wh_cmd-room") for name in spawned))
            result = await watchtogether.command({"mode": "reels", "channel_id": "cmd-room", "action": "seed",
                                                  "q": "cats, street food", "user": user})
        self.assertEqual(result["topics"], ["cats", "streetfood"])
        self.assertGreater(watchtogether.people_doc.data["cmd-user"]["tags"]["cats"], 0)
        status = await watchtogether.command({"mode": "watch", "channel_id": "cmd-room", "action": "status"})
        self.assertFalse(status["active"])
        watchtogether.rooms.pop("wh_cmd-room", None)
        watchtogether.rooms.pop("rh_cmd-room", None)


if __name__ == "__main__":
    unittest.main()
