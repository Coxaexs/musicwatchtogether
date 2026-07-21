import asyncio
import json
import unittest
from unittest import mock

import runtime
import watchtogether
import webui


class FakeSocket:
    def __init__(self):
        self.messages = []

    async def send_str(self, value):
        self.messages.append(json.loads(value))


class RoomRoleTests(unittest.TestCase):
    def test_first_member_hosts_and_moderator_is_promoted_on_disconnect(self):
        room = watchtogether.Room("w-role-unit", "Roles")
        host_socket = FakeSocket()
        moderator_socket = FakeSocket()
        viewer_socket = FakeSocket()

        host = room.join(host_socket, "private-host", "Host")
        moderator = room.join(moderator_socket, "private-moderator", "Moderator")
        viewer = room.join(viewer_socket, "private-viewer", "Viewer")
        moderator["role"] = "moderator"
        room.roles[moderator["session"]] = "moderator"

        self.assertEqual(host["role"], "host")
        self.assertEqual(viewer["role"], "viewer")
        self.assertNotIn("private-host", watchtogether._room_state(room)["participants"][0]["id"])

        room.sockets.pop(host_socket)
        promoted, old_role = room.ensure_active_host()
        self.assertEqual(old_role, "moderator")
        self.assertIs(promoted, moderator)
        self.assertEqual(moderator["role"], "host")
        self.assertEqual(room.roles["private-host"], "moderator")


class PlaylistTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        watchtogether.room_playlists.pop("w-playlist-unit", None)

    async def test_participant_can_save_reusable_room_queue(self):
        room = watchtogether.Room("w-playlist-unit", "Playlists")
        socket = FakeSocket()
        member = room.join(socket, "private-member", "Alex")
        room.queue = [
            {"url": "https://example.com/one", "title": "One", "duration": 12},
            {"url": "not-a-url", "title": "Ignored"},
        ]

        with mock.patch.object(watchtogether, "_save_json") as save:
            await watchtogether._save_room_playlist(room, " Road Trip ", member)

        saved = watchtogether.room_playlists[room.id]["Road Trip"]
        self.assertEqual([item["url"] for item in saved["items"]], ["https://example.com/one"])
        self.assertEqual(saved["updated_by"], "Alex")
        save.assert_called_once()

    async def test_loading_playlist_adds_each_url_in_order(self):
        room = watchtogether.Room("w-playlist-unit", "Playlists")
        member = room.join(FakeSocket(), "private-member", "Alex")
        watchtogether.room_playlists[room.id] = {
            "Mix": {"items": [{"url": "https://example.com/1"},
                                {"url": "https://example.com/2"}]}
        }

        with mock.patch.object(watchtogether, "_notice", new=mock.AsyncMock()), \
                mock.patch.object(watchtogether, "_add_query", new=mock.AsyncMock()) as add:
            await watchtogether._load_room_playlist(room, "Mix", member)

        self.assertEqual(
            [call.args[1] for call in add.await_args_list],
            ["https://example.com/1", "https://example.com/2"],
        )


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
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
        self.assertTrue(payload["ready"])
        self.assertIn("rooms", payload["watch"])
        self.assertIn("musicwatch_process_uptime_seconds", watchtogether.metrics_text())


if __name__ == "__main__":
    unittest.main()
