"""Sampler sounds, and real scratches and rewinds on the decks."""
import asyncio
import os
import random
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dj  # noqa: E402
import realdj  # noqa: E402
import samples  # noqa: E402
import tests.test_realdj_routines as routines  # noqa: E402

P, SHAPE, words_for = routines.P, routines.SHAPE, routines.words_for

SR = dj.SR


def band_energy(x, lo, hi):
    spectrum = np.abs(np.fft.rfft(x[:, 0]))
    freqs = np.fft.rfftfreq(len(x), 1 / SR)
    return float((spectrum[(freqs >= lo) & (freqs < hi)] ** 2).sum())


class SampleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bank = samples.build()

    def test_every_pad_has_a_clean_sound(self):
        self.assertEqual(set(self.bank), set(dj.SAMPLES))
        for name, x in self.bank.items():
            self.assertEqual((x.dtype, x.shape[1]), (np.float32, 2), name)
            self.assertTrue(np.isfinite(x).all(), name)
            self.assertTrue(0.5 < np.abs(x).max() <= 1.0, name)
            self.assertTrue(0.2 < len(x) / SR < 6, name)
            self.assertLess(np.abs(x[-10:]).max(), 0.05, name)       # no click at the end

    def test_the_808_is_low_and_the_horn_honks(self):
        boom = self.bank["boom"][: SR // 2]
        self.assertGreater(band_energy(boom, 30, 90), 4 * band_energy(boom, 200, 2000))
        horn = self.bank["horn"]
        self.assertGreater(band_energy(horn, 800, 3000), 3 * band_energy(horn, 5000, 12000))
        self.assertGreater(band_energy(horn, 280, 500), band_energy(horn, 30, 200))   # the pipes, not rumble

    def test_the_riser_rises(self):
        riser = self.bank["riser"]
        rms = lambda x: float(np.sqrt((x ** 2).mean()))
        self.assertGreater(rms(riser[-SR // 2:]), 4 * rms(riser[: SR // 2]))

    def test_the_bank_builds_off_the_audio_thread(self):
        dj.sample_bank(wait=True)
        self.assertEqual(set(dj.sample_bank()), set(dj.SAMPLES))


class GestureTests(unittest.TestCase):
    """Uses the singing synthetic songs (with stems) from the routine tests."""

    @classmethod
    def setUpClass(cls):
        routines.RoutineTests.setUpClass()
        cls.files = routines.RoutineTests.files

    @classmethod
    def tearDownClass(cls):
        routines.RoutineTests.tearDownClass()

    two_decks = routines.RoutineTests.two_decks

    def playing(self, beat):
        session, a, _b = self.two_decks()
        engine = session.engine
        a.playing = True
        a.seek(a.time_of_beat(beat))
        engine.master_name = "A"
        engine.xfader = -1.0
        return session, engine, a

    def test_scratch_goes_back_and_forth_on_the_vocal_and_lands_on_time(self):
        session, engine, a = self.playing(2 * P + 2)
        word = 0.5 + words_for(SHAPE)[0][0] * 60 / 124
        start = a.seconds
        self.assertTrue(engine.start_gesture(dj.scratch_gesture(a, beats=2, at=word, vocals_only=True)))
        positions, vocal_only = [], True
        for _ in range(int(2 * a.beat_seconds / (dj.BLOCK / SR)) - 1):
            engine.render()
            positions.append(a.seconds)
            vocal_only &= a.stem_gain["drums"] == 0.0 and a.stem_gain["vocals"] == 1.0
        steps = np.diff(positions)
        self.assertTrue((steps < 0).any() and (steps > 0).any())       # pulled back and pushed forward
        self.assertTrue(all(abs(p - word) < 0.4 for p in positions))   # around the word
        self.assertTrue(vocal_only)
        for _ in range(20):
            engine.render()
        self.assertFalse(a.gesture)
        self.assertEqual(set(a.stem_gain.values()), {1.0})
        expected = start + (len(positions) + 20) * dj.BLOCK / SR
        self.assertAlmostEqual(a.seconds, expected, delta=0.06)        # slip: back on time

    def test_rewind_spins_back_and_plays_the_chorus_again(self):
        session, engine, a = self.playing(3 * P + 8)
        target = a.time_of_beat(2 * P)
        before = a.seconds
        engine.start_gesture(dj.rewind_gesture(a, target))
        lowest = before
        for _ in range(60):                        # 1.2 s: spinning back
            engine.render()
            lowest = min(lowest, a.seconds)
        self.assertLess(lowest, before - 1.0)
        for _ in range(25):                        # past the end of the rewind
            engine.render()
        self.assertFalse(a.gesture)
        self.assertTrue(a.playing)
        self.assertGreater(a.seconds, target)
        self.assertLess(a.seconds, target + 0.5)

    def test_pads_move_the_record_when_a_song_is_on(self):
        dj.sample_bank(wait=True)
        session, engine, a = self.playing(2 * P)
        engine.pad("scratch")
        self.assertEqual([g.kind for g in engine.gestures], ["scratch"])
        self.assertEqual(engine.voices, [])
        a.playing = False
        engine.gestures = []
        a.gesture = False
        engine.pad("scratch")                      # nothing on air: the sampled one
        self.assertEqual(len(engine.voices), 1)
        engine.pad("horn")
        self.assertEqual(len(engine.voices), 2)

    def test_real_dj_pulls_up_the_first_chorus_once(self):
        session, engine, a = self.playing(2 * P + 20)
        real = realdj.RealDJ(session, rng=random.Random(1), vibe="hype")
        s = realdj.structure(a)
        plan = realdj.Plan("blend", 32, 7 * P, 0.0)
        phrase = 2                                # in the first chorus, the next phrase is big too
        to = real._pull_up_point(a, s, phrase, (phrase + 1) * P, plan)
        self.assertAlmostEqual(to, a.time_of_beat(s["big"][0] * P), places=3)
        real._fl_rewind(a, (phrase + 1) * P, to)
        self.assertIsNone(real._pull_up_point(a, s, phrase, (phrase + 1) * P, plan))   # once per song
        # Not in the second chorus, nor when the next mix is too close.
        self.assertIsNone(realdj.RealDJ(session, vibe="hype")._pull_up_point(a, s, 5, 6 * P, plan))
        self.assertIsNone(realdj.RealDJ(session, vibe="hype")._pull_up_point(
            a, s, phrase, (phrase + 1) * P, realdj.Plan("blend", 32, 4 * P, 0.0)))
        self.assertIsNone(realdj.RealDJ(session, vibe="chill")._pull_up_point(a, s, phrase, (phrase + 1) * P, plan))
        spun = False
        for _ in range(int(14 * a.beat_seconds / (dj.BLOCK / SR))):
            engine.render()
            spun |= a.gesture
        self.assertTrue(spun)
        self.assertTrue(a.playing)
        self.assertLess(a.beat_at(a.seconds), (phrase + 1) * P)   # replaying the chorus

    def test_three_songs_lays_a_third_vocal_over_the_beat_then_plays_it_next(self):
        files = self.files
        session = dj.DJSession("t", "test", routines.Crate(files[2:]))
        for name, path in (("A", files[0]), ("B", files[1])):
            asyncio.run(session.load(name, {"title": os.path.basename(path), "file": path, "keys": [path]}))
        engine = session.engine
        a, c = engine.decks["A"], engine.decks[dj.LAYER]
        a.playing = True
        a.seek(a.time_of_beat(P + 4))
        engine.master_name = "A"
        engine.xfader = -1.0
        session.three = True
        real = realdj.RealDJ(session, rng=random.Random(1), vibe="club")
        self.assertTrue(real.three)
        real.plan = realdj.Plan("blend", 32, 7 * P, 0.0)
        real._pick_third = lambda m: 0             # the crate song (its stems are "cached")

        async def prepare():
            await real._third_song(a)              # picks it and loads deck C
            for task in list(session.load_tasks.values()):
                await task
            await real._third_song(a)              # plans its phrase
        asyncio.run(prepare())
        self.assertEqual(real.layer["stage"], "ready")
        k = real.layer["phrase"]
        self.assertTrue(realdj.structure(a)["phrases"][k]["big"])
        while a.beat_at(a.seconds) < k * P + 12:
            engine.render()
        self.assertTrue(c.playing and c.sync)
        self.assertEqual((c.stem_gain["drums"], c.stem_gain["other"], c.stem_gain["vocals"]), (0.0, 0.0, 1.0))
        self.assertEqual(a.stem_gain["vocals"], 0.0)                 # the playing song's vocal steps out
        offset = (c.beat_at(c.seconds) - a.beat_at(a.seconds)) % 1.0
        self.assertLess(min(offset, 1 - offset), 0.08)                # on the beat
        self.assertEqual(real.layer["stage"], "on air")
        while a.beat_at(a.seconds) < (k + 1) * P + 1:
            engine.render()
        self.assertFalse(c.loaded)
        self.assertEqual(a.stem_gain["vocals"], 1.0)
        self.assertEqual(real.layer["stage"], "done")
        teased = real.teased["title"]

        async def next_song():
            await real._load_next(engine.decks["B"], a)
            for task in list(session.load_tasks.values()):
                await task
        engine.decks["B"].playing = False
        asyncio.run(next_song())
        self.assertEqual(engine.decks["B"].info.get("title"), teased)  # the tease pays off
        self.assertIsNone(real.teased)

    def test_three_songs_teases_the_song_waiting_on_the_other_deck_first(self):
        files = self.files
        session = dj.DJSession("t", "test", routines.Crate(files[2:]))
        for name, path in (("A", files[0]), ("B", files[1])):
            asyncio.run(session.load(name, {"title": os.path.basename(path), "file": path, "keys": [path]}))
        engine = session.engine
        a = engine.decks["A"]
        a.playing = True
        a.seek(a.time_of_beat(P + 4))
        engine.master_name = "A"
        session.three = True
        real = realdj.RealDJ(session, rng=random.Random(1), vibe="club")
        real.plan = realdj.Plan("blend", 32, 7 * P, 0.0)
        upcoming = {"title": os.path.basename(files[1]), "file": files[1], "keys": [files[1]]}
        real._upcoming_item = lambda m: upcoming
        real._pick_third = lambda m: self.fail("the next song fits: no need for another")
        asyncio.run(real._third_song(a))
        self.assertEqual(real.layer["title"], upcoming["title"])
        self.assertIsNone(real.teased)                 # it's already the next song
        self.assertEqual(len(session.crate.items()), 1)  # nothing taken from the queue

    def test_three_songs_is_off_by_default_and_never_touches_the_layer(self):
        session, engine, a = self.playing(P + 4)
        real = realdj.RealDJ(session, rng=random.Random(1), vibe="club")
        real.plan = realdj.Plan("blend", 32, 7 * P, 0.0)
        asyncio.run(real._third_song(a))
        self.assertIsNone(real.layer)
        self.assertFalse(engine.decks[dj.LAYER].loaded)


if __name__ == "__main__":
    unittest.main()
