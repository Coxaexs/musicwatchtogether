import asyncio
import os
import sys
import tempfile
import unittest
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dj  # noqa: E402


def click_track(path, bpm, seconds=60):
    """A kick on every beat with a hat in between, so tempo analysis locks on."""
    sr = dj.SR
    n = int(seconds * sr)
    t = np.arange(n) / sr
    audio = np.zeros(n)
    beat = 60.0 / bpm
    kick_len = int(0.12 * sr)
    kt = np.arange(kick_len) / sr
    kick = np.sin(2 * np.pi * (50 + 80 * np.exp(-kt / 0.02)) * kt) * np.exp(-kt / 0.05)
    for start in np.arange(0.5, seconds - 0.2, beat):
        i = int(start * sr)
        audio[i:i + kick_len] += kick[: n - i]
    audio += 0.05 * np.sin(2 * np.pi * 220 * t)
    pcm = (np.stack([audio, audio], axis=1) * 0.6 * 32767).astype("<i2")
    with wave.open(path, "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(sr)
        out.writeframes(pcm.tobytes())


class ListCrate:
    def __init__(self, files):
        self.files = list(files)

    def items(self):
        return [{"title": os.path.basename(f)} for f in self.files]

    async def take(self, index=0):
        if not 0 <= index < len(self.files):
            return None
        path = self.files.pop(index)
        return {"title": os.path.basename(path), "file": path, "keys": []}

    async def add(self, query, requester=None):
        return query


class DJEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.a = os.path.join(cls.tmp.name, "a.wav")
        cls.b = os.path.join(cls.tmp.name, "b.wav")
        click_track(cls.a, 120)
        click_track(cls.b, 126)
        cls._cache = dj.mixer.cache
        dj.mixer.cache = dj.mixer.AnalysisCache(os.path.join(cls.tmp.name, "cache.json"))

    @classmethod
    def tearDownClass(cls):
        dj.mixer.cache = cls._cache
        cls.tmp.cleanup()

    def session(self, auto=False):
        session = dj.DJSession("t", "test", ListCrate([self.a, self.b]), auto=auto)
        session.live = True
        return session

    def run_async(self, coro):
        return asyncio.run(coro)

    def test_blocks_are_one_opus_frame_even_when_empty(self):
        session = self.session()
        self.assertEqual(len(session.engine.render()), dj.FRAME_BYTES)

    def test_load_analyses_and_cues_on_a_beat(self):
        session = self.session()
        self.run_async(session.load("A", {"title": "a", "file": self.a}))
        deck = session.engine.decks["A"]
        self.assertTrue(deck.loaded)
        self.assertAlmostEqual(deck.bpm, 120, delta=2)
        self.assertEqual(len(deck.wave["low"]) > 0, True)
        beat = deck.beat_at(deck.cue)
        self.assertAlmostEqual(beat, round(beat), delta=0.05)

    def test_sync_matches_tempo(self):
        session = self.session()
        self.run_async(session.load("A", {"title": "a", "file": self.a}))
        self.run_async(session.load("B", {"title": "b", "file": self.b}))
        engine = session.engine
        engine.decks["A"].playing = True
        engine.master_name = "A"
        self.run_async(session.handle({"op": "sync", "deck": "B"}))
        self.run_async(session.handle({"op": "play", "deck": "B"}))
        for _ in range(50):
            engine.render()
        a, b = engine.decks["A"], engine.decks["B"]
        self.assertAlmostEqual(a.live_bpm(), b.live_bpm(), delta=0.05)
        phase = (a.beat_at(a.seconds) - b.beat_at(b.seconds) + 0.5) % 1 - 0.5
        self.assertLess(abs(phase), 0.05)

    def test_loop_keeps_the_playhead_inside(self):
        session = self.session()
        self.run_async(session.load("A", {"title": "a", "file": self.a}))
        deck = session.engine.decks["A"]
        deck.playing = True
        self.run_async(session.handle({"op": "loop", "deck": "A", "beats": 1}))
        for _ in range(200):  # 4 s, eight trips round a half-second loop
            session.engine.render()
        self.assertTrue(deck.loop_in <= deck.seconds <= deck.loop_out + 0.001)

    def test_hot_cue_sets_then_jumps(self):
        session = self.session()
        self.run_async(session.load("A", {"title": "a", "file": self.a}))
        deck = session.engine.decks["A"]
        deck.seek(10.0)
        self.run_async(session.handle({"op": "hotcue", "deck": "A", "index": 2}))
        stored = deck.hotcues[2]
        deck.seek(30.0)
        self.run_async(session.handle({"op": "hotcue", "deck": "A", "index": 2}))
        self.assertAlmostEqual(deck.seconds, stored, delta=0.001)
        self.assertTrue(deck.playing)

    def test_every_effect_and_sample_renders(self):
        session = self.session()
        self.run_async(session.load("A", {"title": "a", "file": self.a}))
        session.engine.decks["A"].playing = True
        for kind in dj.FX_TYPES:
            self.run_async(session.handle({"op": "fx", "type": kind, "on": True, "level": 1}))
            for _ in range(10):
                self.assertEqual(len(session.engine.render()), dj.FRAME_BYTES)
        for kind in dj.COLOR_FX:
            self.run_async(session.handle({"op": "set", "deck": "A", "param": "color_fx", "value": kind}))
            self.run_async(session.handle({"op": "set", "deck": "A", "param": "color", "value": 0.8}))
            session.engine.render()
        for name in dj.SAMPLES:
            self.run_async(session.handle({"op": "sample", "name": name}))
        self.assertEqual(len(session.engine.render()), dj.FRAME_BYTES)

    def test_manual_mix_hands_over_to_the_other_deck(self):
        session = self.session()
        self.run_async(session.load("A", {"title": "a", "file": self.a}))
        self.run_async(session.load("B", {"title": "b", "file": self.b}))
        engine = session.engine
        engine.decks["A"].playing = True
        engine.xfader = -1
        self.run_async(session.handle({"op": "mix", "style": "blend", "beats": 8}))
        for _ in range(400):  # 8 s: one bar of wait plus eight beats of blend
            engine.render()
        self.assertIsNone(engine.transition)
        self.assertFalse(engine.decks["A"].playing)
        self.assertTrue(engine.decks["B"].playing)
        self.assertEqual(engine.xfader, 1.0)
        self.assertEqual(engine.decks["B"].eq["low"], 0.0)

    def test_auto_dj_starts_from_the_queue(self):
        session = self.session(auto=True)

        async def run():
            await session._tick()
            await asyncio.gather(*session.load_tasks.values())
        self.run_async(run())
        self.assertTrue(any(d.playing for d in session.engine.decks.values()))
        self.assertEqual(len(session.crate.files), 1)

    def test_loading_a_deck_on_air_is_refused(self):
        session = self.session()
        self.run_async(session.load("A", {"title": "a", "file": self.a}))
        session.engine.decks["A"].playing = True
        session.engine.xfader = -1
        with self.assertRaises(ValueError):
            self.run_async(session.handle({"op": "load", "deck": "A", "query": "x"}))


if __name__ == "__main__":
    unittest.main()
