"""Splitting the playing or a queued song into stems from the web player (no DJ booth)."""

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest import mock

import huddle_voice
import stems
import webui


def handler_class():
    return next(c for c in vars(webui).values() if isinstance(c, type) and hasattr(c, "api_split_stems"))


class Request:
    def __init__(self, guild_id, body):
        self.match_info = {"guild_id": guild_id}
        self._body = body

    async def json(self):
        return self._body


def call(ui, request):
    response = asyncio.run(handler_class().api_split_stems(ui, request))
    return response.status, json.loads(response.text)


class SplitStemsApiTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(stems, "enabled", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.requests = []
        rs = mock.patch.object(webui, "request_stems", side_effect=lambda entries: (self.requests.extend(entries) or (1, 0)))
        rs.start()
        self.addCleanup(rs.stop)

    def test_discord_current_and_queued_song(self):
        current = SimpleNamespace(title="Now", url="https://www.youtube.com/watch?v=aaaaaaaaaaa", duration="3:00",
                                  source_type="youtube", thumbnail=None)
        queued = SimpleNamespace(title="Next", url="https://www.youtube.com/watch?v=bbbbbbbbbbb", duration="2:00",
                                 source_type="youtube", thumbnail=None)
        player = SimpleNamespace(current=current, queue=[queued])
        ui = SimpleNamespace(_get_guild_and_player=lambda request: (SimpleNamespace(id=1), player))
        status, body = call(ui, Request("1", {"target": "current"}))
        self.assertEqual(status, 200)
        self.assertIn("Splitting Now", body["message"])
        status, _ = call(ui, Request("1", {"target": "queue", "index": 0}))
        self.assertEqual(status, 200)
        self.assertEqual([e["title"] for e in self.requests], ["Now", "Next"])
        self.assertEqual(call(ui, Request("1", {"target": "queue", "index": 5}))[0], 404)

    def test_huddle_room_track_without_a_page_is_a_search(self):
        manager = SimpleNamespace(players={"room1": {"track": {"title": "Song", "artist": "Band", "query": "band song"},
                                                     "queue": []}})
        with mock.patch.object(huddle_voice, "MANAGER", manager):
            status, body = call(SimpleNamespace(), Request("huddle:room1", {}))
        self.assertEqual(status, 200)
        self.assertEqual(self.requests[0]["url"], "spotify:search:band song")

    def test_off_without_the_separator(self):
        with mock.patch.object(stems, "enabled", return_value=False):
            self.assertEqual(call(SimpleNamespace(), Request("1", {}))[0], 409)


if __name__ == "__main__":
    unittest.main()
