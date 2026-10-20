"""Guitar and piano stems (htdemucs_6s, stems_cache/<hash>.gp.npy) in the DJ engine."""

import asyncio
import json
import os
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dj  # noqa: E402
import realdj  # noqa: E402
import stems  # noqa: E402
from tests.test_dj import ListCrate, click_track  # noqa: E402


class StemGainsTests(unittest.TestCase):
    def test_other_moves_guitar_and_piano_unless_given(self):
        g = stems.StemGains()
        self.assertEqual(set(g), set(stems.ALL_CONTROLS))
        g.update(other=0.0, drums=0.5)
        self.assertEqual((g["guitar"], g["piano"], g["drums"]), (0.0, 0.0, 0.5))
        g.update(other=1.0, guitar=0.2)
        self.assertEqual((g["guitar"], g["piano"]), (0.2, 1.0))
        g["other"] = 0.3
        self.assertEqual((g["guitar"], g["piano"]), (0.3, 0.3))
        g.set_one("other", 0.0)
        self.assertEqual((g["other"], g["guitar"]), (0.0, 0.3))


class ExtraStemEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.a = os.path.join(cls.tmp.name, "a.wav")
        cls.b = os.path.join(cls.tmp.name, "b.wav")
        click_track(cls.a, 120)
        click_track(cls.b, 121)
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

    def setUp(self):
        self.session = dj.DJSession("t", "test", ListCrate([self.a, self.b]))
        self.session.live = True
        asyncio.run(self.session.load("A", {"title": "a", "file": self.a}))
        asyncio.run(self.session.load("B", {"title": "b", "file": self.b}))
        self.engine = self.session.engine
        self.A, self.B = self.engine.decks["A"], self.engine.decks["B"]

    @staticmethod
    def fake(deck, drums=0.15, bass=0.15, vocals=0.15, guitar=0.15, piano=0.15, extra=True):
        """Stems that are fixed fractions of the mix; other is what's left."""
        audio = deck.audio.astype(np.float32)
        deck.stems = np.stack([audio * f for f in (drums, bass, vocals)]).astype(np.int16)
        deck.extra = np.stack([audio * f for f in (guitar, piano)]).astype(np.int16) if extra else None
        deck.stems_status = "ready"

    @staticmethod
    def read_at(deck, seconds, gains=None):
        deck.seek(seconds)
        deck.playing = True
        deck.rate_now = deck.base_rate()
        if gains is not None:
            deck.stem_gain = gains
            deck.stem_now = dict(gains)
        return deck.read(dj.BLOCK, 0.0)

    def test_six_stems_add_up_to_the_track(self):
        plain = self.read_at(self.A, 5.0)
        self.fake(self.A)
        np.testing.assert_allclose(self.read_at(self.A, 5.0, stems.StemGains()), plain, atol=2e-4)
        no_guitar = stems.StemGains()
        no_guitar.set_one("guitar", 0.0)
        np.testing.assert_allclose(self.read_at(self.A, 5.0, no_guitar), plain * 0.85, atol=2e-4)
        # The booth's OTHER button leaves guitar and piano playing ...
        rest_off = stems.StemGains()
        rest_off.set_one("other", 0.0)
        np.testing.assert_allclose(self.read_at(self.A, 5.0, rest_off), plain * 0.75, atol=2e-4)
        # ... while a move that says other=0 means all the music.
        music_off = stems.StemGains({"other": 0.0})
        np.testing.assert_allclose(self.read_at(self.A, 5.0, music_off), plain * 0.45, atol=2e-4)

    def test_without_extra_guitar_and_piano_are_part_of_other(self):
        plain = self.read_at(self.A, 5.0)
        self.fake(self.A, extra=False)
        gains = stems.StemGains()
        gains.set_one("guitar", 0.0)          # nothing to mute: no separate guitar
        np.testing.assert_allclose(self.read_at(self.A, 5.0, gains), plain, atol=2e-4)
        self.assertIsNone(self.A.stem_presence("guitar", 4.0, 8.0))

    def test_booth_buttons_and_state(self):
        self.fake(self.A)
        asyncio.run(self.session.handle({"op": "set", "deck": "A", "param": "stem_guitar", "value": 0}))
        asyncio.run(self.session.handle({"op": "set", "deck": "A", "param": "stem_other", "value": 0}))
        state = self.A.state()["stems"]
        self.assertTrue(state["extra"])
        self.assertEqual((state["gain"]["guitar"], state["gain"]["other"], state["gain"]["piano"]), (0.0, 0.0, 1.0))
        self.assertFalse(self.B.state()["stems"]["extra"])

    def test_auto_dj_picks_a_guitar_ride_for_a_guitar_outro(self):
        self.fake(self.A, drums=0.1, bass=0.1, vocals=0.0, guitar=0.6, piano=0.0)
        self.fake(self.B)
        self.A.meta["camelot"] = self.B.meta["camelot"] = "8A"
        self.assertEqual(dj.choose_style(self.A, self.B), "guitar_ride")
        self.A.extra = None
        self.assertNotEqual(dj.choose_style(self.A, self.B), "guitar_ride")

    def test_guitar_ride_keeps_the_old_guitar_alone_and_never_two_guitars(self):
        a, b, engine = self.A, self.B, self.engine
        self.fake(a, guitar=0.5)
        self.fake(b)
        a.playing = True
        engine.xfader = -1
        asyncio.run(self.session.handle({"op": "mix", "style": "guitar_ride", "beats": 16, "now": True}))
        alone = False
        for _ in range(900):
            engine.render()
            tr = engine.transition
            if not (tr and tr.started):
                if tr is None:
                    break
                continue
            ga, gb = a.stem_gain, b.stem_gain
            self.assertEqual(tr.style, "guitar_ride")
            self.assertFalse(ga["guitar"] > 0.01 and gb["guitar"] > 0.01, (ga, gb))
            if ga["guitar"] == 1.0 and max(ga["drums"], ga["vocals"], ga["other"], ga["bass"]) < 0.01 \
                    and gb["drums"] == 1.0:
                alone = True
        self.assertIsNone(engine.transition)
        self.assertTrue(alone, "the old guitar never rode alone over the new drums")
        self.assertTrue(b.playing and not a.playing)
        self.assertEqual(set(b.stem_gain.values()), {1.0})
        self.assertEqual(set(b.stem_gain), set(stems.ALL_CONTROLS))

    def test_guitar_ride_without_a_guitar_is_a_stem_swap(self):
        self.fake(self.A, extra=False)
        self.fake(self.B)
        self.A.playing = True
        self.engine.xfader = -1
        asyncio.run(self.session.handle({"op": "mix", "style": "guitar_ride", "beats": 8, "now": True}))
        self.engine.render()
        self.assertEqual(self.engine.transition.style, "stem_swap")

    def test_scratch_gesture_restores_guitar(self):
        self.fake(self.A)
        self.A.stem_gain.set_one("guitar", 0.4)
        g = dj.Gesture(self.A, "scratch", lambda t: 1.0, 0.05, stems={"drums": 0.0, "bass": 0.0, "other": 0.0,
                                                                       "vocals": 1.0})
        self.A.playing = True
        self.assertTrue(self.engine.start_gesture(g))
        self.assertEqual(self.A.stem_gain["guitar"], 0.0)
        for _ in range(10):
            self.engine.render()
        self.assertEqual(self.A.stem_gain["guitar"], 0.4)

    def test_real_dj_profile_and_scripts_know_guitar(self):
        a, b = self.A, self.B
        self.fake(a, guitar=0.5, piano=0.1)
        self.fake(b)
        prof = realdj.beat_profile(a)
        self.assertIn("guitar", prof["raw"])
        self.assertGreater(float(prof["raw"]["guitar"].mean()), 0)
        script = realdj.classic_script("guitar_ride", 32)
        mid = dj.script_at(script, 18)            # p = 0.56: the old guitar alone over the new beat
        self.assertAlmostEqual(mid["out.guitar"], 1.0, places=3)
        self.assertLess(mid["out.other"], 0.01)
        self.assertGreater(mid["in.drums"], 0.99)
        self.assertEqual(dj.script_at(script, 30)["in.guitar"], 1.0)
        # A script that never names guitar leaves it out: the engine then moves it with "other".
        self.assertNotIn("in.guitar", dj.script_at(realdj.classic_script("stem_swap", 32), 31))
        drop = realdj.predicted_drop(script, 32, prof, realdj.beat_profile(b), 32, 0)
        self.assertLessEqual(drop, 0.0)
        s = realdj.structure(a)
        self.assertTrue(s and s["phrases"][0]["guitar"] > 0.3)


    def test_real_dj_picks_guitar_ride_only_with_a_guitar(self):
        import random
        a, b = self.A, self.B
        a.meta["camelot"] = b.meta["camelot"] = "8A"
        self.fake(a, drums=0.1, bass=0.1, vocals=0.0, guitar=0.6, piano=0.0)
        self.fake(b)
        rng = random.Random(1)
        so, si = realdj.structure(a), realdj.structure(b)
        picks = {realdj.choose_style(a, b, so, si, [], rng)[0] for _ in range(300)}
        self.assertIn("guitar_ride", picks)
        a.extra = None
        a._structure_cache = None
        so = realdj._structure(a)
        picks = {realdj.choose_style(a, b, so, si, [], rng)[0] for _ in range(300)}
        self.assertNotIn("guitar_ride", picks)


class ExtraStemCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (stems.CACHE_DIR, stems.CACHE_BYTES)
        stems.CACHE_DIR = self.tmp.name
        self.audio = (np.random.default_rng(3).standard_normal((48000 * 4, 2)) * 1000).astype(np.int16)
        self.digest = stems.track_hash(self.audio)
        np.save(stems.stems_path(self.digest), np.zeros((3, len(self.audio), 2), np.int16))
        np.save(stems.extra_path(self.digest), np.ones((2, len(self.audio), 2), np.int16))
        with open(os.path.join(self.tmp.name, "index.json"), "w") as f:
            json.dump({"tracks": {"yt:abc": {"hash": self.digest, "title": "x"}}, "pins": {}}, f)

    def tearDown(self):
        stems.CACHE_DIR, stems.CACHE_BYTES = self.saved
        self.tmp.cleanup()

    def test_found_by_keys_or_audio_and_checked(self):
        self.assertEqual(stems.extras(keys=["yt:abc"]).shape, (2, len(self.audio), 2))
        self.assertIsNotNone(stems.extras(audio=self.audio))
        self.assertIsNone(stems.extras(keys=["yt:abc"], frames=len(self.audio) - 1))
        self.assertIsNone(stems.extras(keys=["yt:nope"]))
        self.assertEqual(stems.status()["library"], 1)        # the .gp.npy is not a second song
        self.assertTrue(stems.library()[0]["extra"])

    def test_forget_and_prune_remove_it(self):
        stems.forget_song(["yt:abc"])
        self.assertFalse(os.path.exists(stems.extra_path(self.digest)))
        np.save(stems.stems_path(self.digest), np.zeros((3, 10, 2), np.int16))
        np.save(stems.extra_path(self.digest), np.zeros((2, 10, 2), np.int16))
        stems.CACHE_BYTES = 0
        stems._prune()
        self.assertFalse(os.path.exists(stems.extra_path(self.digest)))
        self.assertFalse(os.path.exists(stems.stems_path(self.digest)))


if __name__ == "__main__":
    unittest.main()
