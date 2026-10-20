"""The room's skip button while the DJ booth is on air: mix into the next song now."""

import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dj  # noqa: E402
from tests.test_dj import ListCrate, click_track  # noqa: E402


class SkipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.a = os.path.join(cls.tmp.name, "a.wav")
        cls.b = os.path.join(cls.tmp.name, "b.wav")
        click_track(cls.a, 120)
        click_track(cls.b, 122)
        cls._stems_env = os.environ.get("DJ_STEMS")
        os.environ["DJ_STEMS"] = "0"
        cls._settings_file = dj.SETTINGS_FILE
        dj.SETTINGS_FILE = os.path.join(cls.tmp.name, "dj_settings.json")
        cls._cache = dj.mixer.cache
        dj.mixer.cache = dj.mixer.AnalysisCache(os.path.join(cls.tmp.name, "cache.json"))

    @classmethod
    def tearDownClass(cls):
        dj.mixer.cache = cls._cache
        dj.SETTINGS_FILE = cls._settings_file
        if cls._stems_env is None:
            os.environ.pop("DJ_STEMS", None)
        else:
            os.environ["DJ_STEMS"] = cls._stems_env
        cls.tmp.cleanup()

    def session(self, files):
        s = dj.DJSession("t", "test", ListCrate(files))
        s.live = True
        return s

    @staticmethod
    def play_a(session):
        engine = session.engine
        with engine.lock:
            engine.decks["A"].playing = True
            engine.master_name = "A"
            engine.xfader = -1.0

    def run_until_done(self, engine, blocks=3000):
        for _ in range(blocks):
            engine.render()
            if engine.transition is None:
                return True
        return False

    def test_skip_mixes_into_the_loaded_deck_quickly(self):
        async def run():
            s = self.session([])
            await s.load("A", {"title": "a", "file": self.a})
            await s.load("B", {"title": "b", "file": self.b})
            self.play_a(s)
            for _ in range(50):
                s.engine.render()
            await s.handle({"op": "skip"})
            tr = s.engine.transition
            self.assertIsNotNone(tr)
            self.assertIs(tr.inn, s.engine.decks["B"])
            self.assertLessEqual(tr.beats, dj.SKIP_BEATS)
            self.assertTrue(self.run_until_done(s.engine))
            self.assertTrue(s.engine.decks["B"].playing)
            self.assertFalse(s.engine.decks["A"].playing)
        asyncio.run(run())

    def test_skip_during_a_mix_finishes_it(self):
        async def run():
            s = self.session([])
            await s.load("A", {"title": "a", "file": self.a})
            await s.load("B", {"title": "b", "file": self.b})
            self.play_a(s)
            await s.handle({"op": "mix", "style": "fade", "beats": 32, "now": True})
            for _ in range(20):
                s.engine.render()
            self.assertTrue(s.engine.transition.started)
            await s.handle({"op": "skip"})
            self.assertIsNone(s.engine.transition)
            self.assertTrue(s.engine.decks["B"].playing)
        asyncio.run(run())

    def test_skip_loads_the_next_song_first(self):
        async def run():
            s = self.session([self.b])
            await s.load("A", {"title": "a", "file": self.a})
            self.play_a(s)
            await s.handle({"op": "skip"})
            self.assertTrue(s.engine.decks["B"].loaded)
            self.assertIsNotNone(s.engine.transition)
            self.assertTrue(self.run_until_done(s.engine))
            self.assertTrue(s.engine.decks["B"].playing)
        asyncio.run(run())

    def test_skip_with_nothing_queued_says_so(self):
        async def run():
            s = self.session([])
            await s.load("A", {"title": "a", "file": self.a})
            self.play_a(s)
            with self.assertRaises(ValueError):
                await s.handle({"op": "skip"})
            self.assertTrue(s.engine.decks["A"].playing)
        asyncio.run(run())

    def test_skip_while_paused_resumes(self):
        async def run():
            s = self.session([])
            await s.load("A", {"title": "a", "file": self.a})
            await s.load("B", {"title": "b", "file": self.b})
            self.play_a(s)
            await s.handle({"op": "pause"})
            self.assertTrue(s.paused)
            await s.handle({"op": "skip"})
            self.assertFalse(s.paused)
            self.assertIsNotNone(s.engine.transition)
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
