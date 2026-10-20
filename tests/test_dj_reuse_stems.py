"""A song split in the booth is reused next time instead of being split again."""

import asyncio
import os
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dj  # noqa: E402
import stems  # noqa: E402
from tests.test_dj import ListCrate, click_track  # noqa: E402


class ReuseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.a = os.path.join(self.tmp.name, "a.wav")
        click_track(self.a, 120, seconds=20)
        saved = (stems.CACHE_DIR, dj.SETTINGS_FILE, dj.mixer.cache)
        stems.CACHE_DIR = os.path.join(self.tmp.name, "cache")
        os.makedirs(stems.CACHE_DIR)
        dj.SETTINGS_FILE = os.path.join(self.tmp.name, "s.json")
        dj.mixer.cache = dj.mixer.AnalysisCache(os.path.join(self.tmp.name, "c.json"))
        self.addCleanup(lambda: setattr(stems, "CACHE_DIR", saved[0]))
        self.addCleanup(lambda: setattr(dj, "SETTINGS_FILE", saved[1]))
        self.addCleanup(lambda: setattr(dj.mixer, "cache", saved[2]))
        for patch in (mock.patch.object(stems, "enabled", return_value=True),):
            patch.start()
            self.addCleanup(patch.stop)
        self.splits = 0

        async def fake_separate(audio, wanted=lambda: True, background=False, title=None):
            self.splits += 1
            result = np.stack([audio // 4] * 3).astype(np.int16)
            np.save(stems.stems_path(stems.track_hash(audio)), result)
            return result
        p = mock.patch.object(stems.SEPARATOR, "separate", side_effect=fake_separate)
        p.start()
        self.addCleanup(p.stop)

    def test_second_load_comes_from_the_cache_even_with_a_file(self):
        item = {"title": "a", "file": self.a, "keys": ["yt:aaaaaaaaaaa"]}

        async def run():
            s = dj.DJSession("t", "test", ListCrate([]))
            s.live = True
            s.stems_on = True
            await s.load("A", dict(item))
            await asyncio.gather(*s.stem_tasks.values())
            self.assertEqual(self.splits, 1)
            digest = stems.lookup(item["keys"])
            self.assertIsNotNone(digest, "the keys were not indexed")
            self.assertTrue(os.path.exists(stems.instrumental_path(digest)), "no karaoke track was kept")
            await s.load("B", dict(item))
            await asyncio.gather(*s.stem_tasks.values())
            self.assertEqual(self.splits, 1, "the song was split a second time")
            b = s.engine.decks["B"]
            self.assertEqual(b.stems_status, "ready")
            self.assertEqual(b.stems.shape[1], len(b.audio))
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
