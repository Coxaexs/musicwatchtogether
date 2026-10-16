import datetime
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mixer  # noqa: E402
import stem_mixes  # noqa: E402

KEYS = ["8A", "9A", "10A", "8B", "9B", "3A", "4A", "11A", "12B", "1A"]


def library(count=60, missing=12, seed=1):
    rng = random.Random(seed)
    songs = []
    for i in range(count + missing):
        songs.append({
            "title": f"Song {i}",
            "url": f"https://www.youtube.com/watch?v={i:011d}",
            "bpm": rng.choice([88, 96, 100, 110, 118, 122, 124, 126, 128, 140, 174]),
            "camelot": rng.choice(KEYS),
            "energy": round(rng.random(), 2),
            "duration": 200,
            "stemmed": i < count,
            "karaoke": i < count and i % 3 != 0,
        })
    return songs


class StemMixesTests(unittest.TestCase):
    def setUp(self):
        self.day = datetime.date(2026, 10, 8)
        self.mixes = stem_mixes.build(library(), self.day)
        self.by_name = {m["name"]: m for m in self.mixes}

    def test_makes_named_mixes_within_limits(self):
        self.assertIn("Stem mix: Peak time", self.by_name)
        self.assertIn("Stem mix: Smooth harmonic run", self.by_name)
        for mix in self.mixes:
            songs = mix["songs"]
            self.assertLessEqual(len(songs), stem_mixes.SIZE, mix["name"])
            self.assertLessEqual(sum(1 for s in songs if not s["stemmed"]), stem_mixes.MAX_MISSING, mix["name"])
            self.assertEqual(len({s["url"] for s in songs}), len(songs), mix["name"])
            self.assertTrue(mix["description"])

    def test_mixes_keep_to_their_rule(self):
        for song in self.by_name["Stem mix: Peak time"]["songs"]:
            self.assertGreaterEqual(song["energy"], 0.62)
        for song in self.by_name.get("Stem mix: Karaoke night", {"songs": []})["songs"]:
            self.assertTrue(song["karaoke"] or not song["stemmed"])

    def test_harmonic_run_steps_are_mixable(self):
        run = self.by_name["Stem mix: Smooth harmonic run"]["songs"]
        self.assertTrue(all(s["stemmed"] for s in run))
        for a, b in zip(run, run[1:]):
            self.assertLessEqual(mixer.key_distance(a["camelot"], b["camelot"]), 1)
            self.assertLessEqual(stem_mixes._tempo_gap(a, b), 0.06 + 1e-9)

    def test_warm_up_starts_at_its_calmest(self):
        warm = self.by_name.get("Stem mix: Warm-up")
        if warm:
            energies = [s["energy"] for s in warm["songs"]]
            self.assertEqual(energies[0], min(energies))

    def test_same_day_same_mix_and_a_new_day_changes_it(self):
        again = stem_mixes.build(library(), self.day)
        self.assertEqual([[s["url"] for s in m["songs"]] for m in again],
                         [[s["url"] for s in m["songs"]] for m in self.mixes])
        tomorrow = stem_mixes.build(library(), self.day + datetime.timedelta(days=1))
        self.assertNotEqual([s["url"] for s in tomorrow[0]["songs"]], [s["url"] for s in self.mixes[0]["songs"]])

    def test_small_library_makes_nothing(self):
        self.assertEqual(stem_mixes.build(library(count=3, missing=10), self.day), [])


if __name__ == "__main__":
    unittest.main()
