import json
import types
import unittest
from unittest import mock

import music
import webui


class FakeSpotify:
    def __init__(self):
        self.next_calls = 0

    def playlist(self, _url):
        return {
            "name": "Night Drive",
            "images": [{"url": "https://i.scdn.co/cover.jpg"}],
            "tracks": {
                "total": 3,
                "next": "page-2",
                "items": [{"track": self.track("First", "Artist A", 181000)}],
            },
        }

    def album(self, _url):
        return {
            "name": "An Album",
            "total_tracks": 1,
            "artists": [{"name": "Artist B"}],
            "images": [{"url": "https://i.scdn.co/album.jpg"}],
            "tracks": {
                "next": None,
                "items": [self.track("Album Track", "Artist B", 202000, album=False)],
            },
        }

    def next(self, _results):
        self.next_calls += 1
        return {
            "next": None,
            "items": [
                {"track": self.track("Second", "Artist B", 195000)},
                {"track": self.track("Third", "Artist C", 210000)},
            ],
        }

    @staticmethod
    def track(name, artist, duration_ms, album=True):
        value = {
            "name": name,
            "artists": [{"name": artist}],
            "duration_ms": duration_ms,
        }
        if album:
            value["album"] = {"images": [{"url": f"https://i.scdn.co/{name}.jpg"}]}
        return value


class SpotifyCollectionTests(unittest.TestCase):
    def test_playlist_import_paginates_and_preserves_metadata(self):
        spotify = FakeSpotify()
        with mock.patch.object(music, "SPOTIFY_AVAILABLE", True), \
                mock.patch.object(music, "sp", spotify):
            result = music.MusicCog._blocking_spotify_collection(
                "https://open.spotify.com/playlist/abc123?si=share", limit=200
            )

        self.assertEqual(result["name"], "Night Drive")
        self.assertEqual(result["cover"], "https://i.scdn.co/cover.jpg")
        self.assertEqual(result["total"], 3)
        self.assertEqual(len(result["tracks"]), 3)
        self.assertEqual(result["tracks"][0]["title"], "First — Artist A")
        self.assertEqual(result["tracks"][0]["search_query"], "First Artist A")
        self.assertEqual(spotify.next_calls, 1)

    def test_playlist_artist_credits_are_deduplicated_case_insensitively(self):
        spotify = FakeSpotify()
        duplicate_track = spotify.track("Bir Derdim Var", "unused", 250000)
        duplicate_track["artists"] = [
            {"name": "mor ve ötesi"}, {"name": "mor ve ötesi"},
            {"name": "Mor ve Ötesi"}, {"name": "Mor ve Ötesi"},
            {"name": "Tarkan Gözübüyük"},
        ]
        spotify.playlist = mock.Mock(return_value={
            "name": "Turkish Rock", "images": [],
            "tracks": {"total": 1, "next": None,
                       "items": [{"track": duplicate_track}]},
        })
        with mock.patch.object(music, "SPOTIFY_AVAILABLE", True), \
                mock.patch.object(music, "sp", spotify):
            result = music.MusicCog._blocking_spotify_collection(
                "https://open.spotify.com/playlist/abc123")

        track = result["tracks"][0]
        self.assertEqual(track["artists"], ["mor ve ötesi", "Tarkan Gözübüyük"])
        self.assertEqual(track["title"],
                         "Bir Derdim Var — mor ve ötesi, Tarkan Gözübüyük")
        self.assertEqual(track["search_query"],
                         "Bir Derdim Var mor ve ötesi, Tarkan Gözübüyük")

    def test_album_import_uses_collection_cover_as_fallback(self):
        with mock.patch.object(music, "SPOTIFY_AVAILABLE", True), \
                mock.patch.object(music, "sp", FakeSpotify()):
            result = music.MusicCog._blocking_spotify_collection(
                "spotify:album:album123", limit=200
            )

        self.assertEqual(result["kind"], "album")
        self.assertEqual(result["tracks"][0]["thumbnail"], "https://i.scdn.co/album.jpg")

    def test_rejects_non_collection_links_before_calling_spotify(self):
        with mock.patch.object(music, "SPOTIFY_AVAILABLE", True), \
                mock.patch.object(music, "sp", FakeSpotify()):
            with self.assertRaisesRegex(ValueError, "playlist or album"):
                music.MusicCog._blocking_spotify_collection(
                    "https://open.spotify.com/track/track123"
                )


class FakeCog:
    def __init__(self):
        self.playlists = {"1": {"shared": {"Night Drive": []}, "users": {}}}
        self.meta = {}
        self.saved_playlists = None
        self.saved_meta = None

    @staticmethod
    def _owner_key(user_id):
        return music.MusicCog._owner_key(user_id)

    @staticmethod
    def _bucket_for(guild_data, owner_key, create=False):
        return music.MusicCog._bucket_for(guild_data, owner_key, create)

    def resolve_playlist_owner(self, data, guild_id, user_id, name):
        return music.MusicCog.resolve_playlist_owner(self, data, guild_id, user_id, name)

    def _read_playlists(self):
        return self.playlists

    def _write_playlists(self, data):
        self.saved_playlists = data

    def _read_playlist_meta(self):
        return self.meta

    def _write_playlist_meta(self, data):
        self.saved_meta = data

    async def spotify_collection(self, _url, _requester, limit=200):
        self.requested_limit = limit
        payload = {
            "name": "Night Drive",
            "cover": "https://i.scdn.co/imported.jpg",
            "total": 2,
        }
        songs = [
            types.SimpleNamespace(
                title="First — Artist A", url="spotify:search:First Artist A",
                duration="3:01", source_type="spotify",
                thumbnail="https://i.scdn.co/first.jpg",
            ),
            types.SimpleNamespace(
                title="Second — Artist B", url="spotify:search:Second Artist B",
                duration="3:15", source_type="spotify",
                thumbnail="https://i.scdn.co/second.jpg",
            ),
        ]
        return payload, songs


class FakeBot:
    def __init__(self, cog):
        self.cog = cog

    def get_cog(self, _name):
        return self.cog


class FakeRequest:
    async def json(self):
        return {
            "action": "import_spotify",
            "name": "",
            "url": "https://open.spotify.com/playlist/abc123",
        }


class SpotifyImportAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_import_creates_personal_playlist_with_unique_name_and_cover(self):
        cog = FakeCog()
        ui = webui.WebUI(FakeBot(cog))
        guild = types.SimpleNamespace(id=1, me=object())
        player = types.SimpleNamespace()
        ui._get_guild_and_player = mock.Mock(return_value=(guild, player))
        ui._get_acting_user_id = mock.Mock(return_value=42)
        ui._acting_member = mock.Mock(return_value=object())
        ui._guild_state = mock.Mock(return_value={"id": "1"})

        response = await ui.api_playlist_action(FakeRequest())
        body = json.loads(response.text)

        self.assertEqual(response.status, 200)
        self.assertEqual(body["imported_name"], "Night Drive (2)")
        imported = cog.saved_playlists["1"]["users"]["42"]["Night Drive (2)"]
        self.assertEqual(len(imported), 2)
        self.assertEqual(imported[0]["source_type"], "spotify")
        self.assertEqual(
            cog.saved_meta["1"]["users"]["42"]["Night Drive (2)"],
            "https://i.scdn.co/imported.jpg",
        )
        self.assertEqual(cog.requested_limit, 200)


if __name__ == "__main__":
    unittest.main()
