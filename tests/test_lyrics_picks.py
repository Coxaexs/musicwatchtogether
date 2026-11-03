import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from aiohttp.test_utils import make_mocked_request

import webui
from music import MusicCog

LRCLIB = [
    {"id": 11, "trackName": "Zehir", "artistName": "manifest", "albumName": "Zehir", "duration": 300,
     "syncedLyrics": "[00:10.00]Far too long\n[00:12.00]second", "plainLyrics": "Far too long\nsecond"},
    {"id": 12, "trackName": "Zehir", "artistName": "manifest", "albumName": "Zehir", "duration": 184,
     "syncedLyrics": "[00:05.00]Right one\n[00:07.00]♪\n[00:09.00]line two\n[00:11.00]line three\n[00:13.00]line four",
     "plainLyrics": "Right one\nline two"},
    {"id": 13, "trackName": "Zehir", "artistName": "manifest", "albumName": "Copy", "duration": 184,
     "syncedLyrics": "[00:05.00]Right one\n[00:09.00]line two", "plainLyrics": ""},
    {"id": 14, "trackName": "Zehir (live)", "artistName": "manifest", "albumName": None, "duration": 190,
     "syncedLyrics": None, "plainLyrics": "Words only\nno timings"},
    {"id": 15, "trackName": "Empty", "artistName": "x", "duration": 100, "syncedLyrics": "", "plainLyrics": ""},
]


class LyricsPickTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        patcher = mock.patch.object(webui, "LYRICS_PICKS_DIR", self.dir.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cog = MusicCog.__new__(MusicCog)
        self.cog._lrclib_search = mock.AsyncMock(return_value=LRCLIB)
        self.cog._lrclib_get = mock.AsyncMock(return_value=LRCLIB[1])
        self.cog._fetch_synced_lyrics = mock.AsyncMock(return_value={"track": "Wrong", "artist": "x", "lines": [[1.0, "wrong"]]})
        self.cog._fetch_lyrics = mock.AsyncMock(return_value=None)
        self.ui = webui.WebUI(SimpleNamespace(get_cog=lambda name: self.cog))
        admin = mock.patch.object(webui.WebUI, "_authorized_admin", return_value=True)
        self.admin = admin.start()
        self.addCleanup(admin.stop)

    async def get(self, handler, query):
        return json.loads((await handler(make_mocked_request("GET", "/x?" + query))).text)

    async def pick(self, body):
        request = make_mocked_request("POST", "/api/lyrics/pick")
        request.json = mock.AsyncMock(return_value=body)
        return json.loads((await self.ui.api_lyrics_pick(request)).text)

    async def test_candidates_list_distinct_lyrics_with_a_few_lines_each(self):
        data = await self.get(self.ui.api_lyrics_candidates,
                              "title=manifest+feat.+Pango+-+Zehir+%7C+Official+Lyric+Video&duration=184")
        self.assertEqual(data["query"], "manifest Zehir")
        self.cog._lrclib_search.assert_awaited_once_with("manifest Zehir")
        # same length first, the re-upload of the same lyrics dropped, words-only last, empty left out
        self.assertEqual([item["id"] for item in data["candidates"]], [12, 11, 14])
        self.assertEqual(data["candidates"][0]["snippet"], ["Right one", "line two", "line three"])
        self.assertEqual(data["candidates"][2]["synced"], False)
        self.assertEqual(data["candidates"][0]["url"], "https://lrclib.net/tracks/12")

    async def test_someone_can_search_with_their_own_words(self):
        data = await self.get(self.ui.api_lyrics_candidates, "title=Zehir&q=manifest+zehir+akustik")
        self.assertEqual(data["query"], "manifest zehir akustik")

    async def test_a_pick_wins_for_that_server_only_and_can_be_undone(self):
        picked = await self.pick({"title": "manifest - Zehir", "artist": "manifest", "scope": "server-a",
                                  "lrclib_id": 12, "by": "mewis"})
        self.assertEqual(picked["lines"][0], [5.0, "Right one"])
        self.assertEqual((picked["picked"], picked["source_url"]), (True, "https://lrclib.net/tracks/12"))

        here = await self.get(self.ui.api_lyrics_search, "q=manifest+-+Zehir&artist=manifest&pick_scope=server-a")
        self.assertEqual(here["lines"][0], [5.0, "Right one"])
        elsewhere = await self.get(self.ui.api_lyrics_search, "q=manifest+-+Zehir&artist=manifest&pick_scope=server-b")
        self.assertEqual(elsewhere["lines"], [[1.0, "wrong"]])

        self.assertEqual(await self.pick({"title": "manifest - Zehir", "artist": "manifest", "scope": "server-a",
                                          "lrclib_id": None}), {"cleared": True})
        again = await self.get(self.ui.api_lyrics_search, "q=manifest+-+Zehir&artist=manifest&pick_scope=server-a")
        self.assertEqual(again["lines"], [[1.0, "wrong"]])

    async def test_only_hoffle_may_pick_or_read_picks(self):
        await self.pick({"title": "Zehir", "artist": "manifest", "scope": "s", "lrclib_id": 12})
        self.admin.return_value = False
        request = make_mocked_request("POST", "/api/lyrics/pick")
        request.json = mock.AsyncMock(return_value={"title": "Zehir", "lrclib_id": 12})
        self.assertEqual((await self.ui.api_lyrics_pick(request)).status, 403)
        self.assertEqual((await self.ui.api_lyrics_candidates(make_mocked_request("GET", "/x?title=Zehir"))).status, 403)
        # A dashboard invite link gets the normal lookup, not Hoffle's pick.
        data = await self.get(self.ui.api_lyrics_search, "q=Zehir&artist=manifest&pick_scope=s")
        self.assertEqual(data["lines"], [[1.0, "wrong"]])

    async def test_scope_cannot_climb_out_of_the_picks_folder(self):
        path = self.ui._lyrics_pick_path(self.cog, "../../etc", "Zehir", "manifest")
        self.assertTrue(os.path.abspath(path).startswith(os.path.abspath(self.dir.name) + os.sep))


class MusixmatchInThePickerTests(unittest.IsolatedAsyncioTestCase):
    """When lrclib's versions are all wrong, the picker can ask Musixmatch."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        for name, value in (("LYRICS_PICKS_DIR", self.dir.name),):
            patcher = mock.patch.object(webui, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        admin = mock.patch.object(webui.WebUI, "_authorized_admin", return_value=True)
        admin.start()
        self.addCleanup(admin.stop)
        self.cog = MusicCog.__new__(MusicCog)
        self.cog._lrclib_search = mock.AsyncMock(return_value=LRCLIB[3:4])
        self.ui = webui.WebUI(SimpleNamespace(get_cog=lambda name: self.cog))
        self.answer = {"track": "Kanıyorduk", "artist": "Redd", "lines": [[19.9, "Resmini buldum"], [25.0, "♪"], [30.0, "ikinci"]],
                       "lyrics": "", "length": 266.0, "source": "musixmatch"}
        self.mxm = mock.Mock(api_key="key")
        self.mxm.cached.return_value = (None, False)
        self.mxm.lookup = mock.AsyncMock(return_value=self.answer)
        self.mxm.left.return_value = 296
        self.ui._musixmatch = self.mxm

    async def post(self, handler, body):
        request = make_mocked_request("POST", "/x")
        request.json = mock.AsyncMock(return_value=body)
        return json.loads((await handler(request)).text)

    async def test_the_list_says_what_would_be_asked_without_asking(self):
        data = json.loads((await self.ui.api_lyrics_candidates(make_mocked_request(
            "GET", "/x?title=paga+play+-+Redd+-+Kan%C4%B1yorduk&duration=266"))).text)
        self.assertEqual(data["musixmatch"], {"track": "Kanıyorduk", "artist": "Redd", "answered": False,
                                              "candidate": None, "left": 296})
        self.mxm.lookup.assert_not_awaited()

    async def test_asking_shows_its_answer_and_using_it_saves_the_pick(self):
        data = await self.post(self.ui.api_lyrics_ask_musixmatch,
                               {"title": "paga play - Redd - Kanıyorduk", "duration": 266})
        self.mxm.lookup.assert_awaited_once_with("Kanıyorduk", "Redd", 266)
        offer = data["musixmatch"]
        self.assertEqual(offer["candidate"]["snippet"], ["Resmini buldum", "ikinci"])
        self.assertEqual(offer["candidate"]["url"], "https://www.musixmatch.com/lyrics/Redd/Kan%C4%B1yorduk")

        self.mxm.cached.return_value = (self.answer, True)
        picked = await self.post(self.ui.api_lyrics_pick, {
            "title": "paga play - Redd - Kanıyorduk", "scope": "s1", "duration": 266,
            "musixmatch": {"track": "Kanıyorduk", "artist": "Redd"}})
        self.assertEqual((picked["source"], picked["picked"], picked["lines"][0]), ("musixmatch", True, [19.9, "Resmini buldum"]))
        self.cog._fetch_synced_lyrics = mock.AsyncMock()
        later = json.loads((await self.ui.api_lyrics_search(make_mocked_request(
            "GET", "/x?q=paga+play+-+Redd+-+Kan%C4%B1yorduk&pick_scope=s1"))).text)
        self.assertEqual(later["source"], "musixmatch")
        self.cog._fetch_synced_lyrics.assert_not_awaited()

    async def test_typed_artist_and_song_are_what_gets_asked(self):
        await self.post(self.ui.api_lyrics_ask_musixmatch,
                        {"title": "something else", "duration": 200, "q": "Redd - Kanıyorduk"})
        self.mxm.lookup.assert_awaited_once_with("Kanıyorduk", "Redd", 200)

    async def test_a_musixmatch_pick_needs_its_answer_first(self):
        data = await self.post(self.ui.api_lyrics_pick, {
            "title": "x", "scope": "s1", "musixmatch": {"track": "Kanıyorduk", "artist": "Redd"}})
        self.assertEqual(data, {"error": "Ask Musixmatch first."})


class ChannelPrefixTests(unittest.TestCase):
    def setUp(self):
        self.cog = MusicCog.__new__(MusicCog)

    def test_channel_artist_song_titles_ask_about_the_artist(self):
        terms = self.cog._musixmatch_terms
        self.assertEqual(terms("paga play - Redd - Kanıyorduk", None), ("Kanıyorduk", "Redd"))
        self.assertEqual(terms("paga play - Redd - Kanıyorduk - Live", None), ("Kanıyorduk", "Redd"))
        # the artist named first: the third part is not a song
        self.assertEqual(terms("Duman - Senden Daha Güzel - Kafama Göre", "Duman"), ("Senden Daha Güzel", "Duman"))
        # a version word last: the artist came first, the song is the middle part
        self.assertEqual(terms("Redd - Kanıyorduk - Remix", None), ("Kanıyorduk", "Redd"))

    def test_titles_hoffle_rooms_played(self):
        terms = self.cog._musixmatch_terms
        cases = [
            (("Slimane - Mon Amour (LIVE) | France 🇫🇷 | Grand Final | Eurovision 2024", "Eurovision Song Contest"),
             ("Mon Amour", "Slimane")),
            (("hayat | canlı akustik @ Sirkeci Garı - mor ve ötesi", "mor ve ötesi - official channel"),
             ("hayat", "mor ve ötesi")),
            (("John Lennon - Imagine (Lyrics)🎶", "Vhic Playlist"), ("Imagine", "John Lennon")),
            (("@TheMarias - No One Noticed (Lyrics)", "Dan Music"), ("No One Noticed", "TheMarias")),
            (("Sakin - Laleler Beyaz [Official Video Klip] HQ", "osde86"), ("Laleler Beyaz", "Sakin")),
            (("Model ve Fatma Turgut - Levlâ Vazgeçti (Bitmedi Hikayemiz) 2026 Canlı performans", "Buğra Konserde"),
             ("Levlâ Vazgeçti", "Model ve Fatma Turgut")),
            (("Mor Ve Ötesi - Uyan | Remastered (1080p) Stereo", "Klip Arşivi"), ("Uyan", "Mor Ve Ötesi")),
            (("Just the Two of Us - Cover", "Lucy Ellis"), ("Just the Two of Us", "Lucy Ellis")),
            (("Martin Solveig - Intoxicated - Radio Edit (Lyrics)", "Careless"), ("Intoxicated", "Martin Solveig")),
            (("Redd - Kanıyorduk", "paga play"), ("Kanıyorduk", "Redd")),
        ]
        for (title, artist), expected in cases:
            with self.subTest(title=title):
                self.assertEqual(terms(title, artist), expected)

    def test_lrclib_also_tries_song_then_artist(self):
        rungs = self.cog._lyrics_search_rungs("Senden Önce Senden Sonra - Teoman (Official Video)", "MuzikPlay")
        self.assertIn(("Teoman Senden Önce Senden Sonra", "Senden Önce Senden Sonra", "Teoman", False), rungs)

    def test_lrclib_also_tries_the_artist_after_the_channel(self):
        rungs = self.cog._lyrics_search_rungs("paga play - Redd - Kanıyorduk", None)
        self.assertIn(("Redd - Kanıyorduk", "Kanıyorduk", "Redd", False), rungs)
        self.assertEqual(rungs[-1], ("Redd - Kanıyorduk", "Redd - Kanıyorduk", None, True))


class LrclibDownTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_musixmatch_request_while_lrclib_is_not_answering(self):
        import time as clock
        cog = MusicCog.__new__(MusicCog)

        async def lrclib_down(*_args, **_kwargs):
            cog._lrclib_failed_at = clock.time()
            return None

        cog._fetch_synced_lyrics = mock.AsyncMock(side_effect=lrclib_down)
        cog._fetch_lyrics = mock.AsyncMock(return_value=None)
        ui = webui.WebUI(SimpleNamespace(get_cog=lambda name: cog))
        mxm = mock.Mock()
        mxm.cached.return_value = (None, False)
        mxm.lookup = mock.AsyncMock()
        ui._musixmatch = mxm
        with mock.patch.object(webui.WebUI, "_authorized_admin", return_value=True), \
                mock.patch.object(webui, "LYRICS_PICKS_DIR", tempfile.mkdtemp()):
            response = await ui.api_lyrics_search(make_mocked_request(
                "GET", "/api/lyrics/search?q=paga+play+-+Redd+-+Kan%C4%B1yorduk&duration=266&musixmatch=1"))
        data = json.loads(response.text)
        self.assertTrue(data["lrclib_down"])
        mxm.lookup.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
