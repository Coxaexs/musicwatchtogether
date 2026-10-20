import json
import os
import random
import tempfile
import unittest
from unittest import mock

import numpy as np

import djbrain
import realdj


def make_brain(directory, vectors):
    ids = list(vectors)
    emb = np.array([vectors[i] for i in ids], dtype=np.float32)
    emb /= np.linalg.norm(emb, axis=1, keepdims=True)
    with open(directory + "/songs.json", "w") as f:
        json.dump({i: {"bpm": 120, "camelot": "8A", "energy": 0.5, "lang": "en"} for i in ids}, f)
    with open(directory + "/ids.json", "w") as f:
        json.dump(ids, f)
    np.save(directory + "/emb.npy", emb.astype(np.float16))


class Deck:
    def __init__(self, keys, bpm=120, camelot="8A", energy=0.5):
        self.ai = True
        self.info = {"keys": keys}
        self.meta = {"bpm": bpm, "camelot": camelot, "energy": energy}
        self.bpm = bpm

    def live_bpm(self):
        return self.bpm


class BrainTests(unittest.TestCase):
    def use(self, directory):
        djbrain.BRAIN_DIR = directory
        djbrain.reload()

    def tearDown(self):
        djbrain.BRAIN_DIR = os.path.join(os.path.dirname(os.path.abspath(djbrain.__file__)), "dj_brain")
        djbrain.reload()

    def test_missing_folder_means_no_opinion(self):
        self.use("/nonexistent/dj_brain")
        self.assertFalse(djbrain.enabled())
        self.assertIsNone(djbrain.similarity(["yt:a"], ["yt:b"]))
        self.assertIsNone(djbrain.lookup(["yt:a"]))
        self.assertEqual(djbrain.vibe_bonus(None), 0.0)

    def test_damaged_files_do_not_raise(self):
        with tempfile.TemporaryDirectory() as d:
            for name, text in (("songs.json", "{ not json"), ("ids.json", "{ not json")):
                with open(f"{d}/{name}", "w") as f:
                    f.write(text)
            with open(f"{d}/emb.npy", "wb") as f:
                f.write(b"junk")
            self.use(d)
            self.assertIsNone(djbrain.similarity(["yt:a"], ["yt:b"]))

    def test_switch_turns_it_off(self):
        with tempfile.TemporaryDirectory() as d:
            make_brain(d, {"a": [1, 0], "b": [1, 0]})
            with mock.patch.dict(os.environ, {"DJ_BRAIN": "0"}):
                self.use(d)
                self.assertIsNone(djbrain.similarity(["yt:a"], ["yt:b"]))
            self.use(d)
            self.assertAlmostEqual(djbrain.similarity(["yt:a"], ["yt:b"]), 1.0, places=2)

    def test_unknown_song_and_non_youtube_keys(self):
        with tempfile.TemporaryDirectory() as d:
            make_brain(d, {"a": [1, 0], "b": [0, 1]})
            self.use(d)
            self.assertIsNone(djbrain.similarity(["yt:a"], ["yt:zzz"]))
            self.assertIsNone(djbrain.similarity(["spotify:search:x"], ["yt:a"]))
            self.assertAlmostEqual(djbrain.similarity(["yt:a"], ["yt:b"]), 0.0, places=2)

    def test_bonus_is_bounded_and_signed(self):
        self.assertLess(djbrain.vibe_bonus(0.9), 0)          # alike: better (lower) score
        self.assertGreater(djbrain.vibe_bonus(0.1), 0)
        self.assertLessEqual(abs(djbrain.vibe_bonus(5.0)), 0.7 + 1e-9)

    def test_pick_next_prefers_the_song_that_sounds_alike(self):
        with tempfile.TemporaryDirectory() as d:
            make_brain(d, {"cur": [1, 0, 0], "alike": [0.9, 0.1, 0], "other": [0, 0, 1]})
            self.use(d)
            items = [{"title": "other", "keys": ["yt:other"]}, {"title": "alike", "keys": ["yt:alike"]}]
            out = Deck(["yt:cur"])
            with mock.patch.object(realdj.mixer.cache, "get", return_value={"bpm": 120, "camelot": "8A", "energy": 0.5}), \
                    mock.patch.object(realdj.stems, "enabled", return_value=False):
                picks = [realdj.pick_next(items, out, 10, [], random.Random(s)) for s in range(40)]
            self.assertGreater(picks.count(1), 30)           # "alike" wins almost every time

    def test_style_weights_lean_with_similarity(self):
        with tempfile.TemporaryDirectory() as d:
            make_brain(d, {"a": [1, 0], "same": [1, 0.05], "far": [0, 1]})
            self.use(d)
            a, same, far = Deck(["yt:a"]), Deck(["yt:same"]), Deck(["yt:far"])
            for deck in (a, same, far):
                deck.has_stems = False
                deck.bpm = 120
            count = lambda inn: sum(realdj.choose_style(a, inn, None, None, [], random.Random(s), "club")[0] in realdj.DROP_STYLES
                                    for s in range(400))
            self.assertGreater(count(far), count(same))


if __name__ == "__main__":
    unittest.main()
