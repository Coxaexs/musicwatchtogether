import asyncio
import os
import tempfile
import time
import unittest
from unittest import mock

import numpy as np

import stems


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.sep = stems.Separator()
        self.patch = mock.patch.object(stems, "SEPARATOR", self.sep)
        self.patch.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = mock.patch.object(stems, "CACHE_DIR", self.tmp.name)
        self.cache.start()

    def tearDown(self):
        self.cache.stop()
        self.patch.stop()
        self.tmp.cleanup()

    def test_idle(self):
        s = stems.status()
        self.assertIsNone(s["current"])
        self.assertEqual((s["finished"], s["failed"], s["library"]), (0, 0, 0))
        self.assertIsNone(s["average_seconds"])

    def test_library_counts_songs_on_disk(self):
        for name in ("a.npy", "b.npy", "a.inst.flac", "index.json"):
            open(os.path.join(self.tmp.name, name), "w").close()
        self.assertEqual(stems.status()["library"], 2)

    def test_progress_of_a_running_song(self):
        self.sep.device = "cpu"
        self.sep.current = {"title": "Some Song", "started": time.monotonic() - 50, "seconds": 200.0}
        cur = stems.status()["current"]
        self.assertEqual(cur["title"], "Some Song")
        self.assertEqual(cur["device"], "cpu")
        self.assertTrue(0 < cur["percent"] < 100)
        self.assertIsNone(cur["eta"])                        # no finished song to learn from yet
        self.sep.ratios = [1.0]                              # 1 s of work per second of audio
        cur = stems.status()["current"]
        self.assertAlmostEqual(cur["percent"], 25, delta=2)
        self.assertAlmostEqual(cur["eta"], 150, delta=3)
        self.assertEqual(stems.status()["average_seconds"], 200.0)

    def test_percent_never_reaches_100_before_it_is_done(self):
        self.sep.current = {"title": "Slow", "started": time.monotonic() - 9999, "seconds": 10.0}
        self.assertEqual(stems.status()["current"]["percent"], 99)

    def test_a_split_updates_the_counters_and_title(self):
        sep = self.sep

        class Proc:
            returncode = None

            class stdin:
                @staticmethod
                def write(data):
                    pass

                @staticmethod
                async def drain():
                    pass

            class stdout:
                @staticmethod
                async def readline():
                    await asyncio.sleep(0.3)             # the song takes a moment, so the status can be read mid-way
                    return b'{"ok": true, "seconds": 2.0}\n'

        async def fake_start():
            sep.proc = Proc()

        sep._start = fake_start

        async def run():
            audio = np.zeros((stems.SR * 4, 2), dtype=np.int16)
            seen = {}
            original = stems.track_hash

            def fake_hash(a):
                return "abc123"
            with mock.patch.object(stems, "track_hash", fake_hash), mock.patch.object(stems, "enabled", lambda: True), \
                    mock.patch.object(stems, "_load", lambda p: np.zeros((3, 10, 2), np.int16)), \
                    mock.patch.object(stems, "_prune", lambda: None):
                task = asyncio.ensure_future(sep.separate(audio, title="My Title"))
                await asyncio.sleep(0.05)
                seen["title"] = (stems.status()["current"] or {}).get("title")
                await task
            return seen

        seen = asyncio.run(run())
        self.assertEqual(seen["title"], "My Title")
        s = stems.status()
        self.assertEqual((s["finished"], s["failed"]), (1, 0))
        self.assertIsNone(s["current"])


if __name__ == "__main__":
    unittest.main()
