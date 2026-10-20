"""Playlists stored once under the global pseudo-guild show up in every room and server."""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import music  # noqa: E402
import storage  # noqa: E402
import webui  # noqa: E402


class GlobalPlaylistTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        store = storage.SQLiteDocumentStore(os.path.join(self.tmp.name, "state.sqlite3"))
        for target in (music, webui):
            patcher = mock.patch.object(target, "state_store", store)
            patcher.start()
            self.addCleanup(patcher.stop)
        cls = next(c for c in vars(music).values() if isinstance(c, type) and hasattr(c, "resolve_playlist_owner"))
        self.cog = cls.__new__(cls)
        self.cog.PLAYLISTS_FILE = os.path.join(self.tmp.name, "playlists.json")
        self.cog.PLAYLIST_META_FILE = os.path.join(self.tmp.name, "meta.json")
        song = {"title": "a", "url": "https://www.youtube.com/watch?v=aaaaaaaaaaa"}
        self.cog._write_playlists({
            "*": {"shared": {"Everywhere FLAC": [song], "Clash": [song]}, "users": {}},
            "huddle:room": {"shared": {"Clash": [song, song]}, "users": {"7": {"Mine": [song]}}},
        })

    def test_every_room_and_server_sees_the_global_lists(self):
        for guild in ("huddle:room", "huddle:other", "123456"):
            lists = self.cog.get_user_playlists(guild, "7")
            self.assertIn("Everywhere FLAC", lists, guild)
        data = self.cog._read_playlists()
        self.assertEqual(self.cog.resolve_playlist_owner(data, "huddle:other", None, "Everywhere FLAC"), "global")
        self.assertEqual(self.cog._guild_for("huddle:other", "global"), "*")
        lists = self.cog._bucket_for(data[self.cog._guild_for("huddle:other", "global")], "global")
        self.assertIn("Everywhere FLAC", lists)

    def test_a_rooms_own_list_wins(self):
        data = self.cog._read_playlists()
        self.assertEqual(self.cog.resolve_playlist_owner(data, "huddle:room", None, "Clash"), "shared")
        self.assertEqual(len(self.cog.get_user_playlists("huddle:room", None)["Clash"]), 2)
        self.assertEqual(len(self.cog.get_user_playlists("huddle:other", None)["Clash"]), 1)

    def test_one_mix_record_for_all_rooms(self):
        webui._save_playlist_mix("huddle:room", "global", "Everywhere FLAC", {"enabled": True, "transitions": {"x": 1}})
        self.assertEqual(webui._playlist_mix("huddle:other", "global", "Everywhere FLAC")["transitions"], {"x": 1})
        self.assertFalse(webui._playlist_mix("huddle:other", "shared", "Everywhere FLAC")["enabled"])


if __name__ == "__main__":
    unittest.main()
