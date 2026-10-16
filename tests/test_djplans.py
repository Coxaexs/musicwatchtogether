import asyncio
import json
import os
import random
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dj  # noqa: E402
import djbrain  # noqa: E402
import realdj  # noqa: E402
from tests.test_realdj import Crate, SHAPE, song  # noqa: E402

VID = "aaaaaaaaaaa"


def plan_file(directory, songs):
    with open(os.path.join(directory, "plans.json"), "w") as f:
        json.dump({"version": 1, "songs": songs}, f)


class PlanLoading(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        djbrain.BRAIN_DIR = self.tmp.name
        djbrain.reload()

    def tearDown(self):
        djbrain.BRAIN_DIR = os.path.join(os.path.dirname(os.path.abspath(djbrain.__file__)), "dj_brain")
        djbrain.reload()
        self.tmp.cleanup()

    def test_no_file_means_no_opinion(self):
        self.assertIsNone(djbrain.plan([f"yt:{VID}"]))
        self.assertEqual(djbrain.exits([f"yt:{VID}"]), [])
        self.assertEqual(djbrain.moves([f"yt:{VID}"], 0, 999), [])

    def test_low_trust_and_broken_songs_are_never_used(self):
        good = {"trust": "high", "exits": [{"character": "safe", "at": 90, "styles": ["blend"], "why": ""}], "moves": []}
        plan_file(self.tmp.name, {VID: good, "bbbbbbbbbbb": dict(good, trust="low"), "ccccccccccc": dict(good, broken=True)})
        self.assertIsNotNone(djbrain.plan([f"yt:{VID}"]))
        self.assertIsNone(djbrain.plan(["yt:bbbbbbbbbbb"]))
        self.assertIsNone(djbrain.plan(["yt:ccccccccccc"]))
        self.assertIsNone(djbrain.plan(["spotify:search:x"]))

    def test_avoid_ranges_remove_exits_and_moves_are_windowed(self):
        plan_file(self.tmp.name, {VID: {"trust": "ok", "avoid": [{"start": 80, "end": 100, "why": "tempo change"}],
                                        "exits": [{"character": "safe", "at": 90, "styles": ["fade"], "why": ""},
                                                  {"character": "energetic", "at": 120, "styles": ["cut"], "why": ""}],
                                        "moves": [{"at": 10, "type": "roll", "why": ""}, {"at": 20, "type": "echo", "why": ""}]}})
        self.assertEqual([e["character"] for e in djbrain.exits([f"yt:{VID}"])], ["energetic"])
        self.assertEqual([m["type"] for m in djbrain.moves([f"yt:{VID}"], 5, 15)], ["roll"])
        self.assertEqual(djbrain.moves([f"yt:{VID}"], 20, 20), [])

    def test_flags_report_damage_even_when_the_notes_are_not_trusted(self):
        plan_file(self.tmp.name, {VID: {"trust": "low", "broken": True, "issues": ["cuts off at 1:10"], "exits": [], "moves": []},
                                  "bbbbbbbbbbb": {"trust": "low", "issues": [], "exits": [], "moves": []}})
        self.assertEqual(djbrain.flags([f"yt:{VID}"]), ["cuts off at 1:10"])
        self.assertEqual(djbrain.flags(["yt:bbbbbbbbbbb"]), [])
        self.assertEqual(djbrain.flags(["yt:zzzzzzzzzzz"]), [])

    def test_a_newer_plans_file_is_picked_up_without_a_restart(self):
        plan_file(self.tmp.name, {VID: {"trust": "high", "exits": [], "moves": []}})
        self.assertIsNotNone(djbrain.plan([f"yt:{VID}"]))
        self.assertIsNone(djbrain.plan(["yt:bbbbbbbbbbb"]))
        plan_file(self.tmp.name, {VID: {"trust": "high", "exits": [], "moves": []}, "bbbbbbbbbbb": {"trust": "high", "exits": [], "moves": []}})
        os.utime(os.path.join(self.tmp.name, "plans.json"), (1, 1))             # a different modification time
        with mock.patch.object(djbrain, "PLANS_RECHECK", 0.0):
            self.assertIsNotNone(djbrain.plan(["yt:bbbbbbbbbbb"]))

    def test_switch_and_damaged_file(self):
        with open(os.path.join(self.tmp.name, "plans.json"), "w") as f:
            f.write("{ nope")
        self.assertIsNone(djbrain.plan([f"yt:{VID}"]))
        plan_file(self.tmp.name, {VID: {"trust": "high", "exits": [], "moves": []}})
        djbrain.reload()
        with mock.patch.dict(os.environ, {"DJ_BRAIN": "0"}):
            self.assertIsNone(djbrain.plan([f"yt:{VID}"]))


class Engine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.path = os.path.join(cls.tmp.name, "a.wav")
        song(cls.path, 124, SHAPE)
        cls._cache = dj.mixer.cache
        dj.mixer.cache = dj.mixer.AnalysisCache(os.path.join(cls.tmp.name, "cache.json"))
        cls._stems = os.environ.get("DJ_STEMS")
        os.environ["DJ_STEMS"] = "0"

    @classmethod
    def tearDownClass(cls):
        dj.mixer.cache = cls._cache
        if cls._stems is None:
            os.environ.pop("DJ_STEMS", None)
        else:
            os.environ["DJ_STEMS"] = cls._stems
        cls.tmp.cleanup()

    def setUp(self):
        self.brain = tempfile.TemporaryDirectory()
        djbrain.BRAIN_DIR = self.brain.name
        djbrain.reload()

    def tearDown(self):
        djbrain.BRAIN_DIR = os.path.join(os.path.dirname(os.path.abspath(djbrain.__file__)), "dj_brain")
        djbrain.reload()
        self.brain.cleanup()

    def deck(self):
        session = dj.DJSession("t", "test", Crate([]))
        asyncio.run(session.load("A", {"title": "x", "file": self.path, "keys": [f"yt:{VID}"]}))
        return session, session.engine.decks["A"]

    def test_default_exit_without_notes(self):
        _, deck = self.deck()
        s = realdj.structure(deck)
        self.assertNotIn("brain_exit", s)

    def test_a_trusted_exit_option_replaces_the_default_and_stays_put(self):
        _, deck = self.deck()
        default = realdj.structure(deck)["exit"]
        target = default - 1 if default > 4 else default + 1
        at = deck.time_of_beat(target * realdj.PHRASE)
        plan_file(self.brain.name, {VID: {"trust": "high", "exits": [{"character": "energetic", "at": at, "styles": ["cut", "echo"], "why": "after the chorus"}], "moves": []}})
        djbrain.reload()
        _, deck = self.deck()
        s = realdj.structure(deck)
        self.assertEqual(s["exit"], target)
        self.assertEqual(s["exit_beat"], target * realdj.PHRASE)
        self.assertEqual(s["brain_exit"]["styles"], ["cut", "echo"])
        deck.stems_status = "ready"                          # structure() is recomputed; the choice must not change
        self.assertEqual(realdj.structure(deck)["exit"], target)

    def test_an_exit_that_would_make_the_song_too_short_is_ignored(self):
        _, deck = self.deck()
        default = realdj.structure(deck)["exit"]
        plan_file(self.brain.name, {VID: {"trust": "high", "exits": [{"character": "creative", "at": 5.0, "styles": ["cut"], "why": ""}], "moves": []}})
        djbrain.reload()
        _, deck = self.deck()
        s = realdj.structure(deck)
        self.assertEqual(s["exit"], default)
        self.assertNotIn("brain_exit", s)

    def test_a_chosen_exit_leans_the_style_choice(self):
        _, a = self.deck()
        _, b = self.deck()
        for d in (a, b):
            d.meta["energy"] = 0.6
        so = realdj.structure(a)
        cuts = lambda s: sum(realdj.choose_style(a, b, s, None, [], random.Random(i), "club")[0] == "cut" for i in range(400))
        plain = cuts(dict(so))
        boosted = cuts(dict(so, brain_exit={"character": "energetic", "styles": ["cut", "echo"], "why": ""}))
        self.assertGreater(boosted, plain * 1.5)

    def test_moves_near_a_phrase_end_are_suggested(self):
        _, deck = self.deck()
        boundary = 64
        t = deck.time_of_beat(boundary - 3)
        plan_file(self.brain.name, {VID: {"trust": "ok", "exits": [], "moves": [{"at": t, "type": "echo", "why": ""}, {"at": t + 300, "type": "roll", "why": ""}]}})
        djbrain.reload()
        self.assertEqual(realdj._suggested_touches(deck, boundary), {"echo"})
        self.assertEqual(realdj._suggested_touches(deck, boundary + 96), set())


if __name__ == "__main__":
    unittest.main()
