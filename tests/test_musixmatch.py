import asyncio
import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from aiohttp.test_utils import make_mocked_request

import musixmatch
from music import MusicCog
from musixmatch import MusixmatchLyrics, parse_response, song_key

# Trimmed from a real answer (Şebnem Ferah - Mayın Tarlası).
SAMPLE = {
    "success": True, "code": 200,
    "lyrics": [
        {"text": "Mayın tarlasında dolaşıp durmuşum, aşk sanıp da",
         "time": {"total": 36.07, "minutes": 0, "seconds": 36, "hundredths": 7}},
        {"text": "Herkes arkamdan bağırmış, kimseyi duymamışım",
         "time": {"total": 44.59, "minutes": 0, "seconds": 44, "hundredths": 59}},
        {"text": "", "time": {"total": 72.63, "minutes": 1, "seconds": 12, "hundredths": 63}},
        {"text": "", "time": {"total": 74.0, "minutes": 1, "seconds": 14, "hundredths": 0}},
        {"text": "Mayın tarlasında yürüyüp durmuşum, aşk sanıp da",
         "time": {"total": 77.52, "minutes": 1, "seconds": 17, "hundredths": 52}},
    ],
    "lrc_lyrics": "[00:36.07]Mayın tarlasında dolaşıp durmuşum, aşk sanıp da",
    "plain_lyrics": "Mayın tarlasında dolaşıp durmuşum, aşk sanıp da",
    "track_info": {"track_name": "Mayın Tarlası", "track_length": 448,
                   "artist_name": "Şebnem Ferah"},
}
RATE = {"X-RateLimit-Requests-Limit": "300", "X-RateLimit-Requests-Remaining": "297",
        "X-RateLimit-Requests-Reset": "2590000"}


class FakeHttp:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    async def __call__(self, url, params, headers):
        self.calls.append(dict(params))
        await asyncio.sleep(0.01)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


def ok(payload=SAMPLE, headers=RATE, status=200):
    return status, dict(headers), json.dumps(payload)


class ParseTests(unittest.TestCase):
    def test_timed_json_keeps_times_and_marks_instrumental_gaps_once(self):
        lines, plain, info = parse_response(json.dumps(SAMPLE))
        self.assertEqual(lines[0], [36.07, "Mayın tarlasında dolaşıp durmuşum, aşk sanıp da"])
        self.assertEqual([text for _, text in lines].count("♪"), 1)
        self.assertEqual(lines[-1][0], 77.52)
        self.assertEqual(plain, "")
        self.assertEqual(info, {"track": "Mayın Tarlası", "artist": "Şebnem Ferah", "length": 448.0})

    def test_lrc_text_and_plain_text(self):
        lines, _, _ = parse_response("[00:01.50]one\n[01:02.25]two\n[01:05.00]")
        self.assertEqual(lines, [[1.5, "one"], [62.25, "two"], [65.0, "♪"]])
        lines, plain, _ = parse_response({"plain_lyrics": "a\nb\nc\n******* This Lyrics is NOT for Commercial use *******"})
        self.assertEqual((lines, plain), ([], "a\nb\nc"))

    def test_not_found_and_short_error_text_are_empty(self):
        self.assertEqual(parse_response({"success": False, "code": 404})[:2], ([], ""))
        self.assertEqual(parse_response("Lyrics not found")[:2], ([], ""))


class LookupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def client(self, http, **kwargs):
        return MusixmatchLyrics(self.dir.name, "key", "host.example", http_get=http, **kwargs)

    async def test_found_lyrics_cost_one_request_and_then_come_from_disk(self):
        http = FakeHttp(ok())
        first = await self.client(http).lookup("Mayın Tarlası", "Şebnem Ferah", 446)
        # a fresh process (restart) reads it back from disk
        again = await self.client(http).lookup("mayin tarlasi", "SEBNEM FERAH", 450)
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(http.calls[0], {"t": "Mayın Tarlası", "a": "Şebnem Ferah", "d": "7:26", "type": "json"})
        self.assertEqual(first["lines"], again["lines"])
        self.assertEqual(first["source"], "musixmatch")

    async def test_a_different_cut_asks_once_more_then_remembers_its_length(self):
        http = FakeHttp(ok())
        mxm = self.client(http)
        await mxm.lookup("Mayın Tarlası", "Şebnem Ferah", 448)
        studio = await mxm.lookup("Mayın Tarlası", "Şebnem Ferah", 300)
        self.assertEqual(len(http.calls), 2)
        # Musixmatch only had the 7:28 cut: the timing may drift, say so.
        self.assertEqual(studio["version_length"], 448.0)
        await mxm.lookup("Mayın Tarlası", "Şebnem Ferah", 302)
        self.assertEqual(len(http.calls), 2)
        with open(mxm._path(song_key("Mayın Tarlası", "Şebnem Ferah")), encoding="utf-8") as handle:
            entry = json.load(handle)
        self.assertEqual(len(entry["cuts"]), 1)
        self.assertEqual(entry["cuts"][0]["asked"], [300.0, 448.0])

    async def test_not_found_is_remembered_for_a_month(self):
        http = FakeHttp(ok({"success": False, "code": 404, "message": "not found"}))
        mxm = self.client(http)
        self.assertIsNone(await mxm.lookup("Nope", "Nobody", 200))
        self.assertIsNone(await mxm.lookup("Nope", "Nobody", 200))
        self.assertEqual(len(http.calls), 1)
        with mock.patch.object(musixmatch.time, "time", return_value=time.time() + 31 * 86400):
            await mxm.lookup("Nope", "Nobody", 200)
        self.assertEqual(len(http.calls), 2)

    async def test_two_people_asking_at_once_share_one_request(self):
        http = FakeHttp(ok())
        mxm = self.client(http)
        results = await asyncio.gather(*(mxm.lookup("Mayın Tarlası", "Şebnem Ferah", 448) for _ in range(5)))
        self.assertEqual(len(http.calls), 1)
        self.assertTrue(all(result and result["lines"] for result in results))

    async def test_rapidapi_remaining_count_stops_requests_at_zero(self):
        http = FakeHttp(ok(headers={**RATE, "X-RateLimit-Requests-Remaining": "0"}))
        mxm = self.client(http)
        await mxm.lookup("Song A", "Artist", 200)
        self.assertIsNone(await mxm.lookup("Song B", "Artist", 200))
        self.assertEqual(len(http.calls), 1)
        # once the plan renews, requests go out again
        with mock.patch.object(musixmatch.time, "time", return_value=time.time() + 2600000):
            await mxm.lookup("Song C", "Artist", 200)
        self.assertEqual(len(http.calls), 2)

    async def test_own_count_and_daily_cap_apply_before_rapidapi_has_said(self):
        http = FakeHttp(ok(headers={}))
        mxm = self.client(http, monthly_limit=3, daily_limit=2)
        for name in ("A", "B", "C"):
            await mxm.lookup(name, "Artist", 200)
        self.assertEqual(len(http.calls), 2)  # the daily cap
        with mock.patch.object(musixmatch.time, "time", return_value=time.time() + 86400 + 60):
            await mxm.lookup("D", "Artist", 200)
            await mxm.lookup("E", "Artist", 200)
        self.assertEqual(len(http.calls), 3)  # the monthly count

    async def test_errors_are_not_cached_as_not_found(self):
        http = FakeHttp(ok({"success": False, "code": 500}), ok())
        mxm = self.client(http)
        self.assertIsNone(await mxm.lookup("Song", "Artist", 200))
        self.assertIsNone(await mxm.lookup("Song", "Artist", 200))   # waits before retrying
        self.assertEqual(len(http.calls), 1)
        with mock.patch.object(musixmatch.time, "time", return_value=time.time() + 16 * 60):
            self.assertTrue((await mxm.lookup("Song", "Artist", 200))["lines"])
        self.assertEqual(len(http.calls), 2)

    async def test_quota_refusal_blocks_until_the_plan_renews(self):
        http = FakeHttp((429, {**RATE, "X-RateLimit-Requests-Remaining": "0"}, "{}"))
        mxm = self.client(http)
        await mxm.lookup("Song", "Artist", 200)
        await mxm.lookup("Other", "Artist", 200)
        self.assertEqual(len(http.calls), 1)
        self.assertGreater(mxm.usage()["blocked_until"], time.time() + 86400)

    async def test_no_key_sends_nothing(self):
        http = FakeHttp(ok())
        mxm = MusixmatchLyrics(self.dir.name, "", "host.example", http_get=http)
        self.assertIsNone(await mxm.lookup("Song", "Artist", 200))
        self.assertEqual(http.calls, [])


class TermsTests(unittest.TestCase):
    def setUp(self):
        self.cog = MusicCog.__new__(MusicCog)

    def test_titles_hoffle_has_missed_before(self):
        cases = [
            (("manifest feat. Pango - Zehir | Official Lyric Video", None), ("Zehir", "manifest")),
            (("Kurtuluş, Kurtuluş, Sezen Aksu, Sezen Aksu - Büklüm Büklüm [8-TEWgCfg0M].mp3", None),
             ("Büklüm Büklüm", "Kurtuluş, Sezen Aksu")),
            (("Teoman - İki Yabancı ft. Şebnem Ferah", "Teoman"), ("İki Yabancı", "Teoman")),
            (("Redd - Şimdi Seni Bulsam [Radio Edit] (Official Video)", "Redd Official"),
             ("Şimdi Seni Bulsam", "Redd")),
            (("Bir Derdim Var — mor ve ötesi", "mor ve ötesi"), ("Bir Derdim Var", "mor ve ötesi")),
            (("Hello", "AdeleVEVO"), ("Hello", "Adele")),
            (("Gelir Miyim", "Deniz Tekin - Topic"), ("Gelir Miyim", "Deniz Tekin")),
        ]
        for (query, artist), expected in cases:
            with self.subTest(query=query):
                self.assertEqual(self.cog._musixmatch_terms(query, artist), expected)


class EndpointGateTests(unittest.IsolatedAsyncioTestCase):
    """Only Hoffle's master-password /lyricsnow request may spend requests."""

    def make_ui(self, synced=None):
        import webui
        cog = MusicCog.__new__(MusicCog)
        cog._fetch_synced_lyrics = mock.AsyncMock(return_value=synced)
        cog._fetch_lyrics = mock.AsyncMock(return_value=None)
        ui = webui.WebUI(SimpleNamespace(get_cog=lambda name: cog))
        mxm = mock.Mock()
        mxm.cached.return_value = (None, False)
        mxm.lookup = mock.AsyncMock(return_value={
            "track": "Zehir", "artist": "manifest", "lines": [[1.0, "x"]], "lyrics": "", "source": "musixmatch"})
        ui._musixmatch = mxm
        return ui, mxm

    async def ask(self, ui, query, admin=True):
        request = make_mocked_request("GET", "/api/lyrics/search?" + query)
        with mock.patch.object(type(ui), "_authorized_admin", return_value=admin):
            return json.loads((await ui.api_lyrics_search(request)).text)

    async def test_lyricsnow_from_hoffle_asks_musixmatch_after_lrclib(self):
        ui, mxm = self.make_ui()
        data = await self.ask(ui, "q=manifest+-+Zehir&duration=180&musixmatch=1")
        mxm.lookup.assert_awaited_once_with("Zehir", "manifest", 180)
        self.assertEqual(data["source"], "musixmatch")
        self.assertEqual(data["lines"], [[1.0, "x"]])

    async def test_without_the_flag_or_from_an_invite_link_it_is_never_asked(self):
        for query, admin in (("q=Zehir", True), ("q=Zehir&musixmatch=1", False)):
            ui, mxm = self.make_ui()
            await self.ask(ui, query, admin)
            mxm.lookup.assert_not_awaited()
            mxm.cached.assert_not_called()

    async def test_lrclib_synced_lyrics_spend_nothing(self):
        ui, mxm = self.make_ui(synced={"track": "Zehir", "artist": "manifest", "lines": [[2.0, "y"]]})
        data = await self.ask(ui, "q=manifest+-+Zehir&musixmatch=1")
        mxm.lookup.assert_not_awaited()
        self.assertNotIn("source", data)

    def test_the_discord_lyrics_path_has_no_musixmatch_left(self):
        self.assertFalse(hasattr(MusicCog, "_fetch_musixmatch_lyrics"))
        import inspect
        self.assertNotIn("musixmatch", inspect.getsource(MusicCog._fetch_synced_lyrics).lower())


if __name__ == "__main__":
    unittest.main()
