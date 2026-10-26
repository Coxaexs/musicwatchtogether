"""The DJ's mood stations (stations.py): which songs, Real DJ's picks, the session's mood, autoplay's songs."""
import asyncio
import json
import os
import random
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dj  # noqa: E402
import realdj  # noqa: E402
import stations  # noqa: E402
import tests.test_realdj_routines as routines  # noqa: E402

SONGS = {
    "riot0000000": {"in": ["heavy", "indie"], "title": "Three Days Grace - Riot"},
    "reptilia000": {"in": ["indie", "pop"], "title": "The Strokes - Reptilia"},
    "duvet000000": {"in": ["dreamy", "sad"], "title": "Bôa - Duvet"},
    "dumanhelal0": {"in": ["turkish", "indie"], "title": "Duman - Helal Olsun"},
}


class Stations(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = os.path.join(self.tmp.name, "stations.json")
        with open(path, "w") as f:
            json.dump({"songs": SONGS}, f)
        self.patch = mock.patch.object(stations, "FILE", path)
        self.patch.start()
        stations._state.update(songs={}, mtime=None, checked=0.0)

    def tearDown(self):
        self.patch.stop()
        stations._state.update(songs={}, mtime=None, checked=0.0)
        self.tmp.cleanup()

    def test_which_songs_and_how_well(self):
        self.assertEqual(stations.rank(["yt:riot0000000"], "heavy"), 0)
        self.assertEqual(stations.rank(["yt:riot0000000"], "indie"), 1)
        self.assertIsNone(stations.rank(["yt:duvet000000"], "heavy"))
        self.assertEqual(stations.songs("indie")[0][2], 0)             # best fits first
        self.assertLess(stations.score(["yt:riot0000000"], "heavy"), stations.score(["yt:reptilia000"], "indie") + 1)
        self.assertGreater(stations.score(["yt:duvet000000"], "heavy"), stations.score(["yt:unknown0000"], "heavy"))
        self.assertEqual(stations.score(["yt:duvet000000"], None), 0.0)
        counts = {s["id"]: s["songs"] for s in stations.catalog()}
        self.assertEqual((counts["indie"], counts["heavy"], counts["party"]), (3, 1, 0))

    def test_real_dj_picks_the_moods_songs_first(self):
        items = [{"title": "Duvet", "keys": ["yt:duvet000000"]}, {"title": "Riot", "keys": ["yt:riot0000000"]},
                 {"title": "Unknown", "keys": ["yt:unknown0000"]}]
        picks = {realdj.pick_next(items, None, 10, [], random.Random(seed), station="heavy") for seed in range(10)}
        self.assertEqual(picks, {1})
        picks = {realdj.pick_next(items, None, 10, [], random.Random(seed), station="dreamy") for seed in range(10)}
        self.assertEqual(picks, {0})

    def test_the_set_takes_the_moods_vibe_and_fetches_its_songs(self):
        calls = []

        async def refill(session):
            calls.append(session.station)
            return 3
        session = dj.DJSession("huddle:room", "huddle", routines.Crate([]))
        session.crate.used = set()
        session.autoplay = False                         # a station brings its songs even with autoplay off
        with mock.patch.object(dj, "refill", refill):
            asyncio.run(session.handle({"op": "auto", "station": "heavy"}))
            self.assertEqual((session.station, session.vibe), ("heavy", "hype"))
            self.assertTrue(session.station_fresh)
            self.assertIn("Heavy rock", session.notice)
            asyncio.run(session.refill_crate())
            asyncio.run(session.handle({"op": "auto", "station": "sad"}))
            self.assertEqual(session.vibe, "chill")
            asyncio.run(session.refill_crate())          # a new mood: no waiting for the last top-up's cooldown
            asyncio.run(session.handle({"op": "auto", "station": "polka"}))
            self.assertIsNone(session.station)
        self.assertEqual(calls, ["heavy", "sad"])
        state = session.state()["auto"]
        self.assertIsNone(state["station"])
        self.assertEqual(len(state["stations"]), 8)
        session.station = "dreamy"
        again = dj.DJSession("huddle:room", "huddle", routines.Crate([]))
        again.crate.used = set()
        asyncio.run(again.restore(session.snapshot()))
        self.assertEqual(again.station, "dreamy")

    def test_autoplay_brings_the_station_songs_with_stems_first(self):
        import webui
        stemmed = [{"title": "Three Days Grace - Riot", "url": "https://www.youtube.com/watch?v=riot0000000"},
                   {"title": "Other", "url": "https://www.youtube.com/watch?v=other000000"}]
        picks = webui.station_entries("indie", stemmed, taken=["the strokes - reptilia"], count=5, rng=random.Random(1))
        self.assertEqual(picks[0]["title"], "Three Days Grace - Riot")       # it has stems: first
        self.assertEqual([p["title"] for p in picks[1:]], ["Duman - Helal Olsun"])   # Reptilia was played
        self.assertEqual(picks[1]["url"], "https://www.youtube.com/watch?v=dumanhelal0")


if __name__ == "__main__":
    unittest.main()
