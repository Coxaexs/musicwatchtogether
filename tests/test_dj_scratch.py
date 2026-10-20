"""Scratches move the song on air like a hand on a record (no canned sound)."""

import asyncio
import math
import os
import random
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dj  # noqa: E402
from tests.test_dj import ListCrate, click_track  # noqa: E402


class BoxReadTests(unittest.TestCase):
    def test_constant_and_slow_reads_are_plain_interpolation(self):
        audio = np.full((1000, 2), 1000, np.int16)
        edges = np.arange(100, 301, 2.0)                       # twice normal speed
        np.testing.assert_allclose(dj._box_read(audio, edges), 1000, atol=1e-3)
        ramp = np.repeat(np.arange(1000, dtype=np.int16)[:, None], 2, axis=1)
        slow = np.arange(100, 110.5, 0.5)                      # half speed: under a sample apart
        np.testing.assert_allclose(dj._box_read(ramp, slow)[:, 0], slow[1:], atol=1e-3)

    def test_a_fast_sweep_does_not_alias(self):
        t = np.arange(48000) / 48000
        tone = (np.sin(2 * np.pi * 15000 * t) * 10000).astype(np.int16)
        audio = np.stack([tone, tone], axis=1)
        positions = np.arange(1000, 1000 + 2 * 2000, 2.0)      # read at 2x: 15 kHz would fold to 18 kHz
        point = audio[positions.astype(int)].astype(float)
        box = dj._box_read(audio, np.concatenate([[positions[0] - 2], positions]))
        self.assertLess(np.sqrt((box ** 2).mean()), 0.6 * np.sqrt((point ** 2).mean()))


class ScratchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.a = os.path.join(cls.tmp.name, "a.wav")
        cls.b = os.path.join(cls.tmp.name, "b.wav")
        click_track(cls.a, 120)
        click_track(cls.b, 121)
        cls._env = os.environ.get("DJ_STEMS")
        os.environ["DJ_STEMS"] = "0"
        cls._settings = dj.SETTINGS_FILE
        dj.SETTINGS_FILE = os.path.join(cls.tmp.name, "s.json")
        cls._cache = dj.mixer.cache
        dj.mixer.cache = dj.mixer.AnalysisCache(os.path.join(cls.tmp.name, "c.json"))

    @classmethod
    def tearDownClass(cls):
        dj.mixer.cache = cls._cache
        dj.SETTINGS_FILE = cls._settings
        if cls._env is None:
            os.environ.pop("DJ_STEMS", None)
        else:
            os.environ["DJ_STEMS"] = cls._env
        cls.tmp.cleanup()

    def session(self):
        s = dj.DJSession("t", "test", ListCrate([]))
        s.live = True
        asyncio.run(s.load("A", {"title": "a", "file": self.a}))
        asyncio.run(s.load("B", {"title": "b", "file": self.b}))
        return s

    def test_strokes_vary_and_come_back(self):
        s = self.session()
        deck = s.engine.decks["A"]
        g1 = dj.scratch_gesture(deck, beats=2, rng=random.Random(1))
        g2 = dj.scratch_gesture(deck, beats=2, rng=random.Random(2))
        ts = np.linspace(0, g1.seconds, 2000)
        r1, r2 = np.array([g1.rate(t) for t in ts]), np.array([g2.rate(t) for t in ts])
        self.assertGreater(np.abs(r1 - r2).max(), 0.1)        # not the same machine stroke every time
        self.assertGreater(r1.max(), 1.2)                      # fast enough to be a scratch
        # Integrated over whole strokes the record ends where it started.
        dt = 1e-4
        moved = sum(g1.rate(t) * dt for t in np.arange(0, g1.seconds, dt))
        self.assertLess(abs(moved), 0.08)

    def test_no_vocal_means_the_whole_mix(self):
        s = self.session()
        deck = s.engine.decks["A"]
        audio = deck.audio.astype(np.float32)
        deck.stems = np.stack([audio * 0.5, audio * 0.5, audio * 0.0]).astype(np.int16)   # no vocal at all
        deck.stems_status = "ready"
        self.assertIsNone(dj.scratch_gesture(deck, vocals_only=True).stems)
        deck.stems = np.stack([audio * 0.2, audio * 0.2, audio * 0.6]).astype(np.int16)
        self.assertEqual(dj.scratch_gesture(deck, vocals_only=True).stems["vocals"], 1.0)

    def test_pad_scratches_the_record_mid_mix(self):
        s = self.session()
        engine = s.engine
        engine.decks["A"].playing = True
        engine.master_name = "A"
        engine.xfader = -1.0
        asyncio.run(s.handle({"op": "mix", "style": "fade", "beats": 32, "now": True}))
        for _ in range(10):
            engine.render()
        self.assertTrue(engine.transition and engine.transition.started)
        played = []
        engine.play_sample = lambda name: played.append(name)
        engine.pad("scratch")
        self.assertEqual(played, [])
        self.assertTrue(any(g.kind == "scratch" for g in engine.gestures))
        for _ in range(200):
            out = engine.render()
        self.assertEqual(len(out), dj.FRAME_BYTES)

    def test_pad_with_nothing_on_air_plays_the_sample(self):
        s = self.session()
        played = []
        s.engine.play_sample = lambda name: played.append(name)
        s.engine.pad("scratch")
        self.assertEqual(played, ["scratch"])


if __name__ == "__main__":
    unittest.main()
