import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

import realdj
import stems
import words

LINES = [[10.0, "Was an honest man"], [14.0, ""], [15.0, "Asked me for the phone"]]


def timed(fp, fit=0.9, mode="aligned"):
    return {"fp": fp, "mode": mode, "fit": fit, "lines": [
        {"at": 10.1, "end": 11.9, "text": "Was an honest man", "words": [
            [10.1, 10.4, "Was", [100, 200, 300], 0.8], [10.4, 10.6, "an", [100, 200], 0.7],
            [10.6, 11.2, "honest", [100, 200, 300, 400, 500, 600], 0.6], [11.3, 11.9, "man", [200, 400, 600], 0.9]]},
        {"at": 15.2, "end": 17.0, "text": "Asked me for the phone", "words": [
            [15.2, 15.5, "Asked", [60, 120, 180, 240, 300], 0.5], [15.6, 15.8, "me", [100, 200], 0.5],
            [15.9, 16.1, "for", [70, 140, 200], 0.5], [16.2, 16.3, "the", [30, 60, 100], 0.5],
            [16.4, 17.0, "phone", [100, 200, 300, 400, 600], 0.5]]}]}


class WordsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(stems, "CACHE_DIR", self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        words._cache.clear()

    def write(self, data):
        with open(words.words_path("abc"), "w", encoding="utf-8") as f:
            json.dump(data, f)

    def test_fingerprint_ignores_times_case_and_punctuation(self):
        self.assertEqual(words.fingerprint([[1, "Oh, I don't"]]), words.fingerprint([[9, "oh i don t"]]))
        self.assertNotEqual(words.fingerprint([[1, "Oh"]]), words.fingerprint([[1, "Ah"]]))
        self.assertIsNone(words.fingerprint([[1, "  "]]))

    def test_asks_once_for_lyrics_it_has_no_words_for(self):
        self.assertTrue(words.want("abc", LINES, "lrclib", "Hard to Explain"))
        with open(words.request_path("abc"), encoding="utf-8") as f:
            asked = json.load(f)
        self.assertEqual([t for _, t in asked["lines"]], ["Was an honest man", "Asked me for the phone"])
        self.assertEqual(asked["fp"], words.fingerprint(LINES))
        self.assertFalse(words.want("abc", LINES))                        # already asked
        self.write(timed(words.fingerprint(LINES)))
        os.remove(words.request_path("abc"))
        self.assertFalse(words.want("abc", LINES))                        # already timed

    def test_words_follow_the_shown_lines_blank_ones_included(self):
        self.write(timed(words.fingerprint(LINES)))
        out = words.for_lines("abc", LINES)
        self.assertEqual(len(out), 3)
        self.assertEqual(out[1], [])
        self.assertEqual(out[0][0], [10.1, 10.4, "Was", [100, 200, 300]])
        self.assertEqual([w[2] for w in out[2]], ["Asked", "me", "for", "the", "phone"])

    def test_only_a_line_the_aligner_lost_drops_its_words(self):
        data = timed(words.fingerprint(LINES))
        for w in data["lines"][1]["words"]:
            w[4] = 0.002
        data["lines"][0]["words"][0][4] = 0.0          # one unsure word does not hide a line
        self.write(data)
        out = words.for_lines("abc", LINES)
        self.assertEqual(len(out[0]), 4)
        self.assertEqual(out[2], [])

    def test_slightly_different_lyrics_get_words_word_by_word(self):
        # Another upload of the same lyrics: a line split in two, a word dropped, one added.
        self.write(timed(words.fingerprint(LINES)))
        shown = [[10.0, "Was an honest"], [11.0, "man"], [15.0, "Asked me for phone, yeah"]]
        out = words.for_lines("abc", shown)
        self.assertEqual([w[2] for w in out[0]], ["Was", "an", "honest"])
        self.assertEqual(out[1], [[11.3, 11.9, "man", [200, 400, 600]]])
        self.assertEqual([w[2] for w in out[2]], ["Asked", "me", "for", "phone,", "yeah"])
        self.assertEqual(out[2][3][0], 16.4)                       # "phone," is the timed "phone"
        self.assertEqual(out[2][4][3], [])                         # "yeah" only the shown lyrics have
        self.assertGreaterEqual(out[2][4][0], out[2][3][0])

    def test_a_poor_fit_is_trusted_once_whisper_confirmed_the_words(self):
        data = timed(words.fingerprint(LINES), fit=0.1)
        data["match"] = 0.8
        self.write(data)
        self.assertIsNotNone(words.for_lines("abc", LINES))

    def test_other_lyrics_get_no_words(self):
        self.write(timed(words.fingerprint(LINES)))
        self.assertIsNone(words.for_lines("abc", [[10.0, "A different song"]]))

    def test_a_poor_fit_is_not_trusted(self):
        self.write(timed(words.fingerprint(LINES), fit=0.3))
        self.assertIsNone(words.for_lines("abc", LINES))
        self.assertIsNone(words.spans("abc"))

    def test_spans_for_the_dj(self):
        self.write(timed(words.fingerprint(LINES)))
        lines, ws = words.spans("abc")
        self.assertEqual(lines, [(10.1, 11.9), (15.2, 17.0)])
        self.assertEqual(len(ws), 9)


class TwinTests(unittest.TestCase):
    """Another upload of a timed song: its shifted words, looked up by the YouTube id."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(stems, "CACHE_DIR", self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        words._cache.clear()

    def test_asks_then_uses_the_shifted_words(self):
        keys = ["yt:06NxZ4_0KO0"]
        self.assertIsNone(words.twin_lines(keys, LINES))
        self.assertTrue(words.want_twin(keys, LINES, "Hard to Explain", "293"))
        self.assertFalse(words.want_twin(keys, LINES))                 # asked already
        with open(os.path.join(words.twins_dir(), "06NxZ4_0KO0.words.json"), "w") as f:
            json.dump(dict(timed(words.fingerprint(LINES)), twin_of="abc", shift=3.8), f)
        out = words.twin_lines(keys, LINES)
        self.assertEqual(out[0][0][2], "Was")
        self.assertFalse(words.want_twin(keys, LINES))                 # answered

    def test_no_youtube_id_no_twin(self):
        self.assertFalse(words.want_twin(["some query"], LINES))
        self.assertIsNone(words.twin_lines(["some query"], LINES))


class FollowLyricsTests(unittest.TestCase):
    SPANS = ([(10.0, 12.0), (15.0, 17.0)], [(10.0, 10.5), (10.6, 11.2), (11.3, 12.0), (15.0, 17.0)])

    def test_mid_line(self):
        self.assertEqual(realdj.mid_line(self.SPANS, 10.2), 1.0)          # inside a word
        self.assertEqual(realdj.mid_line(self.SPANS, 10.55), 0.6)         # between words of a line
        self.assertEqual(realdj.mid_line(self.SPANS, 13.5), 0.0)          # between lines
        self.assertEqual(realdj.mid_line(None, 10.2), 0.0)

    def test_hook_loop_ends_with_the_line(self):
        # 120 bpm: a beat is 0.5 s. A two-word line sung from 20.0 to 20.9: a 2-beat loop holds all of it.
        deck = SimpleNamespace(beat_seconds=0.5)
        spans = ([(20.0, 20.9)], [(20.0, 20.4), (20.45, 20.9)])
        hook = {"time": 20.08, "length": 1}
        realdj._fit_hook_to_words(deck, hook, spans)
        self.assertEqual(hook, {"time": 20.0, "length": 2})

    def test_hook_loop_does_not_chop_a_word(self):
        deck = SimpleNamespace(beat_seconds=0.5)
        spans = ([(30.0, 32.0)], [(30.0, 30.45), (30.6, 31.3), (31.35, 32.0)])
        hook = {"time": 30.0, "length": 2}                                 # 2 beats end inside "word two"
        realdj._fit_hook_to_words(deck, hook, spans)
        self.assertEqual(hook["length"], 1)

    def test_lyric_spans_only_when_on_and_on_the_cached_audio(self):
        deck = SimpleNamespace(lyrics=True, stems=np.zeros((3, 1000, 2), np.int16), wave_id=1, info={"keys": ["yt:x"]})
        with mock.patch.object(realdj, "_digest", return_value="abc"), \
                mock.patch.object(stems, "stem_frames", return_value=1000), \
                mock.patch.object(words, "spans", return_value=self.SPANS):
            self.assertEqual(realdj.lyric_spans(deck), self.SPANS)
            deck.lyrics = False
            self.assertIsNone(realdj.lyric_spans(deck))
        other = SimpleNamespace(lyrics=True, stems=np.zeros((3, 999, 2), np.int16), wave_id=2, info={"keys": ["yt:x"]})
        with mock.patch.object(realdj, "_digest", return_value="abc"), \
                mock.patch.object(stems, "stem_frames", return_value=1000), \
                mock.patch.object(words, "spans", return_value=self.SPANS):
            self.assertIsNone(realdj.lyric_spans(other))                    # a stream: may be offset


if __name__ == "__main__":
    unittest.main()
