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
        self.loop = asyncio.new_event_loop()

    def tearDown(self):
        async def stop():
            await stems.SEPARATOR._kill()
            if stems.SEPARATOR.idle_task:
                stems.SEPARATOR.idle_task.cancel()
        self.loop.run_until_complete(stop())
        self.loop.close()
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
