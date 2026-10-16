import json
import os
import tempfile
import time
import unittest
from unittest import mock

import numpy as np

import mixer
import songlib
import stems

VID = "aBcDeFgHiJk"
OTHER = "zYxWvUtSrQp"


class FakeMusic:
    """Stands in for music.py: a library folder walked on demand."""

    def __init__(self, root):
        self.root = root

    def _walk(self):
        return tuple(os.path.join(d, f) for d, _, names in os.walk(self.root) for f in names if f.endswith(".mp3"))

    def list_library_files(self, max_age=0):
        return self._walk()

    refresh_library_index = _walk

    def parse_local_song_name(self, path):
        stem = os.path.splitext(os.path.basename(path))[0].split(" - [")[0]
        return stem, os.path.basename(os.path.dirname(path))


class SongLibTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lib = os.path.join(self.tmp.name, "musics")
        self.cache = os.path.join(self.tmp.name, "stems_cache")
        os.makedirs(os.path.join(self.lib, "Duman"))
        os.makedirs(self.cache)
        self.good = os.path.join(self.lib, "Duman", f"Sarhoş - [{VID}].mp3")
        self.other = os.path.join(self.lib, "Duman", f"Yürekten - [{OTHER}].mp3")
        for path in (self.good, self.other):
            open(path, "wb").write(b"mp3")
        self.patches = [
            mock.patch.object(songlib, "_music", lambda: FakeMusic(self.lib)),
            mock.patch.object(stems, "CACHE_DIR", self.cache),
            mock.patch.object(mixer, "cache", mixer.AnalysisCache(os.path.join(self.tmp.name, "mix.json"))),
        ]
        for p in self.patches:
            p.start()
        # a prepared song: stems on disk, index entries under two keys, a pin, an analysis
        np.save(stems.stems_path("hash1"), np.zeros((3, 4, 2), np.int16))
        open(stems.instrumental_path("hash1"), "wb").write(b"flac")
        np.save(stems.stems_path("hash2"), np.zeros((3, 4, 2), np.int16))
        stems.remember([f"yt:{VID}", "spotify:search:sarhos duman"], "hash1", "Sarhoş")
        stems.remember([f"yt:{OTHER}"], "hash2", "Yürekten")
        stems.pin("hash1", "playlist:x")
        mixer.cache.put({"bpm": 120}, f"yt:{VID}")
        mixer.cache.put({"bpm": 90}, f"yt:{OTHER}")

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def test_gemini_flags_are_reported_and_listed_first(self):
        with mock.patch.object(songlib.djbrain, "flags", lambda keys: ["cuts off at 1:10"] if f"yt:{OTHER}" in keys else []):
            rows = songlib.search("")
        self.assertEqual(rows[0]["id"], OTHER)
        self.assertEqual(rows[0]["issues"], ["cuts off at 1:10"])
        self.assertEqual(rows[1]["issues"], [])

    def test_search_finds_by_name_and_reports_state(self):
        rows = songlib.search("sarho")
        self.assertEqual([r["id"] for r in rows], [VID])
        self.assertTrue(rows[0]["stems"] and rows[0]["analysed"])
        self.assertEqual(rows[0]["artist"], "Duman")
        self.assertEqual(len(songlib.search("")), 2)

    def test_delete_removes_file_stems_index_pin_and_analysis(self):
        result = songlib.delete(VID)
        self.assertEqual(result["files"], [os.path.basename(self.good)])
        self.assertTrue(result["stems"] and result["analysis"])
        self.assertFalse(os.path.exists(self.good))
        self.assertFalse(os.path.exists(stems.stems_path("hash1")))
        self.assertFalse(os.path.exists(stems.instrumental_path("hash1")))
        index = stems.load_index()
        self.assertNotIn(f"yt:{VID}", index["tracks"])
        self.assertNotIn("spotify:search:sarhos duman", index["tracks"])         # every key of that song
        self.assertNotIn("hash1", index["pins"])
        self.assertIsNone(mixer.cache.get(f"yt:{VID}"))

    def test_delete_leaves_other_songs_alone(self):
        songlib.delete(VID)
        self.assertTrue(os.path.exists(self.other))
        self.assertTrue(os.path.exists(stems.stems_path("hash2")))
        self.assertIn(f"yt:{OTHER}", stems.load_index()["tracks"])
        self.assertEqual(mixer.cache.get(f"yt:{OTHER}"), {"bpm": 90})

    def test_a_deleted_song_is_gone_from_search_and_can_be_redone(self):
        songlib.delete(VID)
        self.assertEqual(songlib.search("sarho"), [])
        with self.assertRaises(LookupError):
            songlib.delete(VID)

    def test_works_when_the_stems_file_is_already_missing_or_never_existed(self):
        os.remove(stems.stems_path("hash1"))                                     # a damaged cache entry
        self.assertTrue(songlib.delete(VID)["stems"])
        os.remove(self.other)
        os.makedirs(os.path.join(self.lib, "X"), exist_ok=True)
        fresh = os.path.join(self.lib, "X", "New - [qqqqqqqqqqq].mp3")
        open(fresh, "wb").write(b"mp3")
        result = songlib.delete("qqqqqqqqqqq")                                   # never analysed, never split
        self.assertFalse(result["stems"] or result["analysis"])

    def test_rejects_anything_that_is_not_a_video_id(self):
        for bad in ("", None, "../etc/passwd", "short", "a" * 12, "bad id here"):
            with self.assertRaises(ValueError):
                songlib.delete(bad)
        self.assertTrue(os.path.exists(self.good))

    def test_files_without_an_id_are_listed_but_not_deletable(self):
        plain = os.path.join(self.lib, "Duman", "No id here.mp3")
        open(plain, "wb").write(b"mp3")
        rows = {r["file"]: r for r in songlib.search("no id")}
        self.assertIsNone(rows["No id here.mp3"]["id"])


if __name__ == "__main__":
    unittest.main()
