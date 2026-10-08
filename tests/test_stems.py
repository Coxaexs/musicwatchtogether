import asyncio
import os
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dj  # noqa: E402
import stems  # noqa: E402

FAKE_WORKER = (
    "import json, sys\n"
    "import numpy as np\n"
    "print(json.dumps({'ready': True, 'device': 'cpu', 'model': 'fake'}), flush=True)\n"
    "for line in sys.stdin:\n"
    "    job = json.loads(line)\n"
    "    pcm = np.fromfile(job['input'], dtype='<i2').reshape(-1, 2)\n"
    "    np.save(job['output'], np.stack([pcm // 4, pcm // 4, pcm // 2]))\n"
    "    print(json.dumps({'id': job['id'], 'ok': True, 'seconds': 0}), flush=True)\n")


class StemsLibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = stems.CACHE_DIR
        stems.CACHE_DIR = self.tmp.name
        stems._library_cache = (None, [])

    def tearDown(self):
        stems.CACHE_DIR = self.saved
        stems._library_cache = (None, [])
        self.tmp.cleanup()

    def write(self, digest, seconds, used, karaoke=False):
        np.save(stems.stems_path(digest), np.zeros((3, int(seconds * stems.SR), 2), dtype=np.int16))
        os.utime(stems.stems_path(digest), (used, used))
        if karaoke:
            open(stems.instrumental_path(digest), "wb").close()

    def test_lists_each_song_once_newest_first(self):
        self.write("aaa", 3, used=1000)
        self.write("bbb", 2, used=2000, karaoke=True)
        stems.remember(["yt:abcdefghijk", "song a artist"], "aaa", "Song A")
        stems.remember(["spotify:search:song b"], "bbb", "Song B")
        stems.remember(["yt:zzzzzzzzzzz"], "gone", "Pruned song")   # no stems file
        stems.pin("aaa", "top")
        songs = stems.library()
        self.assertEqual([s["title"] for s in songs], ["Song B", "Song A"])
        b, a = songs
        self.assertEqual((a["youtube"], a["seconds"], a["karaoke"], a["pinned"]), ("abcdefghijk", 3, False, True))
        self.assertEqual((b["youtube"], b["seconds"], b["karaoke"], b["pinned"]), (None, 2, True, False))
        self.assertEqual(sorted(a["keys"]), ["song a artist", "yt:abcdefghijk"])

    def test_reading_does_not_touch_files(self):
        self.write("aaa", 1, used=1000)
        stems.remember(["yt:abcdefghijk"], "aaa", "Song A")
        stems.library()
        self.assertEqual(os.path.getmtime(stems.stems_path("aaa")), 1000)

    def test_empty_cache(self):
        self.assertEqual(stems.library(), [])


class StemsCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        fake = os.path.join(self.tmp.name, "fake_worker.py")
        with open(fake, "w") as f:
            f.write(FAKE_WORKER)
        self.saved = (stems.PYTHON, stems.WORKER, stems.CACHE_DIR, stems.CACHE_BYTES,
                      os.environ.get("DJ_STEMS"))
        stems.PYTHON, stems.WORKER = sys.executable, fake
        stems.CACHE_DIR = os.path.join(self.tmp.name, "cache")
        os.environ["DJ_STEMS"] = "1"
        stems.SEPARATOR = stems.Separator()
        self._temp = stems.gpu_temperature
        stems.gpu_temperature = lambda: None          # don't depend on the real GPU
        self.loop = asyncio.new_event_loop()
        self._mix_cache = dj.mixer.cache
        dj.mixer.cache = dj.mixer.AnalysisCache(os.path.join(self.tmp.name, "mix.json"))

    def tearDown(self):
        async def stop():
            await stems.SEPARATOR._kill()
            if stems.SEPARATOR.idle_task:
                stems.SEPARATOR.idle_task.cancel()
        self.loop.run_until_complete(stop())
        self.loop.close()
        stems.gpu_temperature = self._temp
        dj.mixer.cache = self._mix_cache
        stems.PYTHON, stems.WORKER, stems.CACHE_DIR, stems.CACHE_BYTES, env = self.saved
        if env is None:
            os.environ.pop("DJ_STEMS", None)
        else:
            os.environ["DJ_STEMS"] = env
        self.tmp.cleanup()

    def song(self, name, seconds=4, freq=220):
        t = np.arange(int(seconds * stems.SR)) / stems.SR
        wave = (np.sin(2 * np.pi * freq * t) * 12000).astype(np.int16)
        path = os.path.join(self.tmp.name, name + ".wav")
        import wave as wavmod
        with wavmod.open(path, "wb") as out:
            out.setnchannels(2)
            out.setsampwidth(2)
            out.setframerate(stems.SR)
            out.writeframes(np.stack([wave, wave], axis=1).tobytes())
        return path

    def test_prepare_indexes_by_key_and_writes_the_instrumental(self):
        path = self.song("a")
        digest = self.loop.run_until_complete(stems.prepare(path, ["yt:aaaaaaaaaaa", None], "A", instrumental=True))
        self.assertEqual(stems.lookup(["nope", "yt:aaaaaaaaaaa"]), digest)
        inst = stems.instrumental_for(["yt:aaaaaaaaaaa"])
        self.assertTrue(inst and os.path.exists(inst))
        # vocals are half the fake mix, so the instrumental is the other half
        audio = stems.decode(path)
        back = stems.decode(inst)
        self.assertEqual(back.shape, audio.shape)
        np.testing.assert_allclose(back, audio - audio // 2, atol=2)
        # cached: the second time needs neither ffmpeg nor the worker
        stems.WORKER = "/nonexistent"
        again = self.loop.run_until_complete(
            stems.prepare("/nonexistent.wav", ["yt:aaaaaaaaaaa"], instrumental=True))
        self.assertEqual(again, digest)

    def test_a_prepared_song_is_rebuilt_without_its_source(self):
        path = self.song("a")
        self.loop.run_until_complete(stems.prepare(path, ["yt:aaaaaaaaaaa", "song a"], "A",
                                                   instrumental=True))
        audio = stems.decode(path)
        os.remove(path)                               # the source is gone
        rebuilt, cached = stems.local_audio(["song a"])
        self.assertEqual(rebuilt.shape, audio.shape)
        np.testing.assert_allclose(rebuilt, audio, atol=2)
        self.assertEqual(cached.shape, (3,) + audio.shape)
        self.assertIsNone(stems.local_audio(["unknown"]))

    def test_the_dj_loads_a_prepared_song_from_the_cache(self):
        path = self.song("a", seconds=8)
        self.loop.run_until_complete(stems.prepare(path, ["song a"], "A", instrumental=True))
        session = dj.DJSession("t", "test", None)
        self.loop.run_until_complete(session.load(
            "A", {"title": "a", "audio_url": "https://example.invalid/expired", "keys": ["song a"]}))
        deck = session.engine.decks["A"]
        self.assertTrue(deck.loaded)
        self.assertEqual(deck.stems_status, "ready")
        self.assertNotIn("A", session.stem_tasks)     # nothing to split again

    def test_pins_survive_pruning_and_follow_the_playlist(self):
        a = self.loop.run_until_complete(stems.prepare(self.song("a", freq=200), ["a"], pin_reason="playlist:x"))
        b = self.loop.run_until_complete(stems.prepare(self.song("b", freq=300), ["b"]))
        c = self.loop.run_until_complete(stems.prepare(self.song("c", freq=400), ["c"]))
        os.utime(stems.stems_path(a), (1, 1))          # a is the oldest
        stems.CACHE_BYTES = os.path.getsize(stems.stems_path(b)) + 10
        stems._prune()
        self.assertTrue(os.path.exists(stems.stems_path(a)))   # pinned
        self.assertEqual(sum(os.path.exists(stems.stems_path(d)) for d in (b, c)), 1)
        stems.set_pins("playlist:x", [])
        self.assertNotIn(a, stems.pinned())

    def test_forget_drops_files_for_a_recompute(self):
        digest = self.loop.run_until_complete(stems.prepare(self.song("a"), ["a"], instrumental=True))
        stems.forget(["a"])
        self.assertIsNone(stems.lookup(["a"]))
        self.assertFalse(os.path.exists(stems.instrumental_path(digest)))

    def test_background_work_waits_for_a_listener(self):
        audio_a = stems.decode(self.song("a", freq=200))
        audio_b = stems.decode(self.song("b", freq=300))
        order = []

        async def run():
            async def bg():
                await asyncio.sleep(0.05)
                await stems.SEPARATOR.separate(audio_a, background=True)
                order.append("background")

            async def fg():
                await stems.SEPARATOR.lock.acquire()      # something is splitting
                task = asyncio.create_task(bg())
                await asyncio.sleep(0.2)
                waiter = asyncio.create_task(stems.SEPARATOR.separate(audio_b))
                await asyncio.sleep(0.1)
                stems.SEPARATOR.lock.release()
                await waiter
                order.append("listener")
                await task
            await fg()
        self.loop.run_until_complete(run())
        self.assertEqual(order, ["listener", "background"])


class BackgroundPauseTests(unittest.TestCase):
    def test_background_waits_for_a_live_set_and_a_hot_gpu(self):
        saved = (stems.pause_background, stems.gpu_temperature)
        try:
            stems.pause_background, stems.gpu_temperature = (lambda: False), (lambda: 60)
            self.assertFalse(stems.background_should_wait())
            stems.gpu_temperature = lambda: 90
            self.assertTrue(stems.background_should_wait())
            stems.pause_background, stems.gpu_temperature = (lambda: True), (lambda: None)
            self.assertTrue(stems.background_should_wait())
        finally:
            stems.pause_background, stems.gpu_temperature = saved

    def test_a_live_booth_pauses_background_work(self):
        session = dj.DJSession("bg", "test", None)
        session.live = True
        dj.sessions["bg"] = session
        try:
            self.assertTrue(stems.pause_background())
        finally:
            dj.sessions.pop("bg", None)
        self.assertFalse(stems.pause_background())


class KaraokeDeckTests(unittest.TestCase):
    def test_karaoke_keeps_vocals_out_even_through_transitions(self):
        deck = dj.Deck("A")
        n = stems.SR * 2
        audio = (np.random.default_rng(1).normal(0, 3000, (n, 2))).astype(np.int16)
        deck.audio = audio
        deck.stems = np.stack([np.zeros_like(audio), np.zeros_like(audio), audio])  # all vocals
        deck.stems_status = "ready"
        deck.karaoke = True
        deck.playing = True
        deck.rate_now = 1.0
        deck.read(dj.BLOCK, 0.0)                       # ramps down
        out = deck.read(dj.BLOCK, 0.0)
        self.assertLess(np.abs(out).max(), 1e-3)
        deck.stem_gain["vocals"] = 1.0                 # a transition asks for them back
        out = deck.read(dj.BLOCK, 0.0)
        self.assertLess(np.abs(out).max(), 1e-3)
        self.assertTrue(deck.state()["karaoke"])


if __name__ == "__main__":
    unittest.main()
