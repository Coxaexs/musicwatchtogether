"""AI-planned playlist transitions: cut points and styles that AutoMix and the booth follow."""

import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dj  # noqa: E402
import mixer  # noqa: E402
from tests.test_dj import ListCrate, click_track  # noqa: E402

OUT = {"bpm": 120.0, "first_beat": 0.5, "music_end": 200.0, "duration": 210.0, "bpm_confidence": 0.9,
       "camelot": "8A", "energy": 0.6}
IN = {"bpm": 122.0, "first_beat": 0.3, "music_start": 0.3, "duration": 220.0, "bpm_confidence": 0.9,
      "camelot": "8A", "energy": 0.6}


class AutoMixPlanTests(unittest.TestCase):
    def test_planned_cut_points_land_on_bars(self):
        plan = mixer.plan(OUT, IN, {"preset": "blend", "bars": 8, "out_at": 150.3, "in_at": 31.0})
        bar_out, bar_in = 2.0, 4 * 60 / 122.0
        self.assertAlmostEqual((plan["out_at"] - 0.5) / bar_out, round((plan["out_at"] - 0.5) / bar_out), places=2)
        self.assertLess(abs(plan["out_at"] - 150.3), bar_out)
        self.assertAlmostEqual((plan["in_at"] - 0.3) / bar_in, round((plan["in_at"] - 0.3) / bar_in), places=2)
        self.assertLess(abs(plan["in_at"] - 31.0), bar_in)

    def test_auto_preset_keeps_the_cut_points(self):
        spec = mixer.normalize_spec({"preset": "auto", "out_at": 120, "in_at": 16})
        self.assertEqual((spec["out_at"], spec["in_at"]), (120.0, 16.0))
        plan = mixer.plan(OUT, IN, spec)
        self.assertLess(abs(plan["out_at"] - 120), 2.0)

    def test_impossible_cut_points_fall_back(self):
        default = mixer.plan(OUT, IN, {"preset": "fade", "bars": 8})
        late = mixer.plan(OUT, IN, {"preset": "fade", "bars": 8, "out_at": 209.0})     # no room left
        self.assertEqual(late["out_at"], default["out_at"])
        passed = mixer.plan(OUT, IN, {"preset": "fade", "bars": 8, "out_at": 50.0}, out_position=100.0)
        self.assertEqual(passed["out_at"], default["out_at"])
        self.assertIsNone(mixer.normalize_spec({"preset": "fade", "out_at": "soon"}).get("out_at"))


class BoothPlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.a = os.path.join(cls.tmp.name, "a.wav")
        cls.b = os.path.join(cls.tmp.name, "b.wav")
        click_track(cls.a, 120, seconds=90)
        click_track(cls.b, 121, seconds=90)
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

    def test_auto_dj_follows_the_plan(self):
        async def run():
            s = dj.DJSession("t", "test", ListCrate([]), auto=True)
            s.live = True
            await s.load("A", {"title": "a", "file": self.a,
                               "mix": {"preset": "blend", "out_at": 40.0, "in_at": 20.0,
                                       "booth": "filter", "booth_beats": 8}})
            await s.load("B", {"title": "b", "file": self.b})
            a, b = s.engine.decks["A"], s.engine.decks["B"]
            with s.engine.lock:
                a.playing = True
                s.engine.master_name = "A"
                s.engine.xfader = -1.0
                a.seek(30.0)
            plan = dj.planned_mix(a, b)
            self.assertEqual(plan["style"], "filter")
            self.assertEqual(plan["beats"], 8)
            self.assertAlmostEqual(a.time_of_beat(plan["start_beat"]), 40.0, delta=2.0)
            started = None
            for _ in range(800):                 # 16 s of audio, ticking Auto DJ along
                s.engine.render()
                await s._tick()
                tr = s.engine.transition
                if tr and tr.started and started is None:
                    started = a.seconds
                    self.assertEqual(tr.style, "filter")
                    self.assertEqual(tr.beats, 8)
            self.assertIsNotNone(started, "the planned mix never started")
            self.assertAlmostEqual(started, 40.0, delta=2.2)
            self.assertAlmostEqual(b.cue, 20.0, delta=2.0)          # entry on a bar near the planned point
        asyncio.run(run())

    def test_no_plan_is_the_old_behaviour(self):
        async def run():
            s = dj.DJSession("t", "test", ListCrate([]))
            await s.load("A", {"title": "a", "file": self.a})
            await s.load("B", {"title": "b", "file": self.b})
            self.assertEqual(dj.planned_mix(s.engine.decks["A"], s.engine.decks["B"]), {})
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
