"""Who may change a Hoffle room's playlists: an admin, or whoever made the list."""

import json
import os
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import huddle  # noqa: E402
import music  # noqa: E402
import storage  # noqa: E402
import webui  # noqa: E402

ROOM = "huddle:room"
KEY = "test-bot-key"
FIRST = {"title": "Old song", "url": "https://www.youtube.com/watch?v=aaaaaaaaaaa"}


class FakeBot:
    def __init__(self, cog):
        self.cog = cog
        self.guilds = []

    def get_cog(self, _name):
        return self.cog


class FakeRequest:
    """One call to the playlist API, with Hoffle's key or a /web link token."""

    def __init__(self, body=None, guild=ROOM, token=None, query=None):
        self.body = body or {}
        self.match_info = {"guild_id": guild}
        self.path = f"/api/guilds/{guild}/playlists"
        self.query = query or {}
        self.cookies = {}
        self.headers = {"X-MB-Session": token} if token else {"Authorization": f"Bearer {KEY}"}

    async def json(self):
        return self.body


class PlaylistRightsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        store = storage.SQLiteDocumentStore(os.path.join(self.tmp.name, "state.sqlite3"))
        for patcher in (
            mock.patch.object(music, "state_store", store),
            mock.patch.object(webui, "state_store", store),
            mock.patch.object(config, "HOFFLE_BOT_KEY", KEY),
            mock.patch.object(webui, "_save_tokens", lambda: None),
            mock.patch.object(webui, "temp_tokens", {}),
            mock.patch.object(huddle, "room_state", mock.AsyncMock(return_value={})),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        cls = next(c for c in vars(music).values() if isinstance(c, type) and hasattr(c, "resolve_playlist_owner"))
        self.cog = cls.__new__(cls)
        self.cog.PLAYLISTS_FILE = os.path.join(self.tmp.name, "playlists.json")
        self.cog.PLAYLIST_META_FILE = os.path.join(self.tmp.name, "meta.json")
        self.cog._write_playlists({
            "*": {"shared": {"Everywhere FLAC": [dict(FIRST)]}, "users": {}},
            ROOM: {"shared": {"Old list": [dict(FIRST)]}, "users": {}},
            "1": {"shared": {"Discord list": [dict(FIRST)]}, "users": {}},
        })
        self.next_song = "https://www.youtube.com/watch?v=bbbbbbbbbbb"

        async def process_youtube(_query, _requester):
            return types.SimpleNamespace(title="New song", url=self.next_song, duration="3:00",
                                         source_type="youtube", thumbnail=None)

        self.cog.process_youtube = process_youtube
        self.ui = webui.WebUI(FakeBot(self.cog))

    async def act(self, by=None, admin=False, token=None, guild=ROOM, **body):
        if by:
            body.update(by=by, admin=admin)
        response = await self.ui.api_playlist_action(FakeRequest(body, guild=guild, token=token))
        return response.status, json.loads(response.text)

    def songs(self, name, guild=ROOM):
        return self.cog._read_playlists()[guild]["shared"].get(name)

    async def test_anyone_can_make_a_playlist_and_add_to_it(self):
        status, _ = await self.act(by="ana", action="create", name="Mine")
        self.assertEqual(status, 200)
        status, body = await self.act(by="ana", action="add_song", name="Mine", query=self.next_song)
        self.assertEqual(status, 200)
        self.assertEqual([song["title"] for song in self.songs("Mine")], ["New song"])
        self.assertTrue(next(item for item in body["playlists"] if item["name"] == "Mine")["can_edit"])

    async def test_nobody_else_can_add_to_it(self):
        await self.act(by="ana", action="create", name="Mine")
        status, body = await self.act(by="bob", action="add_song", name="Mine", query=self.next_song)
        self.assertEqual(status, 403)
        self.assertIn("Mine", body["error"])
        self.assertEqual(self.songs("Mine"), [])

    async def test_a_list_with_no_creator_is_admin_only(self):
        status, _ = await self.act(by="ana", action="add_song", name="Old list", query=self.next_song)
        self.assertEqual(status, 403)
        status, _ = await self.act(by="kiwi", admin=True, action="add_song", name="Old list", query=self.next_song)
        self.assertEqual(status, 200)
        self.assertEqual(len(self.songs("Old list")), 2)

    async def test_lists_in_every_room_are_admin_only(self):
        status, _ = await self.act(by="ana", action="add_song", name="Everywhere FLAC", query=self.next_song)
        self.assertEqual(status, 403)
        status, _ = await self.act(by="flo", admin=True, action="add_song", name="Everywhere FLAC", query=self.next_song)
        self.assertEqual(status, 200)
        self.assertEqual(len(self.songs("Everywhere FLAC", "*")), 2)

    async def test_the_same_rule_covers_removing_and_deleting(self):
        await self.act(by="ana", action="create", name="Mine")
        await self.act(by="ana", action="add_song", name="Mine", query=self.next_song)
        status, _ = await self.act(by="bob", action="remove_song", name="Mine", index=0)
        self.assertEqual(status, 403)
        status, _ = await self.act(by="bob", action="delete", name="Mine")
        self.assertEqual(status, 403)
        self.assertEqual(len(self.songs("Mine")), 1)
        status, _ = await self.act(by="ana", action="remove_song", name="Mine", index=0)
        self.assertEqual(status, 200)

    async def test_a_deleted_name_goes_to_whoever_makes_it_next(self):
        await self.act(by="ana", action="create", name="Mine")
        await self.act(by="ana", action="delete", name="Mine")
        await self.act(by="bob", action="create", name="Mine")
        status, _ = await self.act(by="ana", action="add_song", name="Mine", query=self.next_song)
        self.assertEqual(status, 403)
        status, _ = await self.act(by="bob", action="add_song", name="Mine", query=self.next_song)
        self.assertEqual(status, 200)

    async def test_a_web_link_acts_as_the_person_it_was_made_for(self):
        await self.act(by="ana", action="create", name="Mine")
        anas = webui.generate_token(ROOM, None, hoffle_user="ana")
        bobs = webui.generate_token(ROOM, None, hoffle_user="bob")
        status, _ = await self.act(token=bobs, action="delete", name="Mine")
        self.assertEqual(status, 403)
        status, _ = await self.act(token=anas, action="add_song", name="Mine", query=self.next_song)
        self.assertEqual(status, 200)

    async def test_a_web_link_cannot_claim_to_be_someone_else(self):
        await self.act(by="ana", action="create", name="Mine")
        bobs = webui.generate_token(ROOM, None, hoffle_user="bob")
        status, _ = await self.act(token=bobs, by="ana", admin=True, action="delete", name="Mine")
        self.assertEqual(status, 403)

    async def test_a_link_made_before_this_changes_nothing(self):
        old = webui.generate_token(ROOM, None)
        status, _ = await self.act(token=old, action="delete", name="Old list")
        self.assertEqual(status, 403)

    async def test_huddle_link_keeps_who_it_is_for(self):
        class LinkRequest(FakeRequest):
            path = "/api/admin/huddle-link"

        response = await self.ui.api_admin_huddle_link(LinkRequest({"channel_id": "room", "by": "ana", "admin": True}))
        info = webui.temp_tokens[json.loads(response.text)["token"]]
        self.assertEqual((info["hoffle_user"], info["hoffle_admin"]), ("ana", True))

    async def test_the_list_says_what_this_person_may_change(self):
        await self.act(by="ana", action="create", name="Mine")
        self.ui._refresh_stems_library = mock.AsyncMock()

        async def editable(**query):
            response = await self.ui.api_playlists(FakeRequest(query=query))
            return {item["name"] for item in json.loads(response.text)["playlists"] if item.get("can_edit")}

        self.assertEqual(await editable(by="ana"), {"Mine"})
        self.assertEqual(await editable(by="bob"), set())
        self.assertEqual(await editable(by="flo", admin="1"), {"Mine", "Old list", "Everywhere FLAC"})

    async def test_discord_playlists_stay_as_they_were(self):
        guild = types.SimpleNamespace(id=1, me=object(), voice_client=None)
        self.ui._get_guild_and_player = mock.Mock(return_value=(guild, types.SimpleNamespace()))
        self.ui._guild_state = mock.Mock(return_value={})
        self.ui._get_acting_user_id = mock.Mock(return_value=None)
        status, _ = await self.act(guild="1", action="add_song", name="Discord list", query=self.next_song)
        self.assertEqual(status, 200)
        self.assertEqual(len(self.songs("Discord list", "1")), 2)

    async def test_a_song_already_in_the_list_is_not_added_twice(self):
        self.next_song = FIRST["url"]
        status, body = await self.act(by="kiwi", admin=True, action="add_song", name="Old list", query=self.next_song)
        self.assertEqual(status, 409)
        self.assertIn("already in Old list", body["error"])
        self.assertEqual(len(self.songs("Old list")), 1)

    async def test_hoffle_cannot_start_a_stem_split_by_adding_to_stemmed_songs(self):
        with mock.patch.object(webui, "request_stems") as split:
            status, _ = await self.act(by="kiwi", admin=True, action="add_song",
                                       name=webui.STEMS_PLAYLIST, query=self.next_song)
        self.assertEqual(status, 400)
        split.assert_not_called()

    async def test_saves_stay_off_the_event_loop(self):
        threads = []
        write = self.cog._write_playlists

        def spy(data):
            threads.append(threading.current_thread())
            write(data)

        self.cog._write_playlists = spy
        await self.act(by="ana", action="create", name="Mine")
        await self.act(by="ana", action="add_song", name="Mine", query=self.next_song)
        self.assertEqual(len(threads), 2)
        self.assertNotIn(threading.main_thread(), threads)


if __name__ == "__main__":
    unittest.main()
