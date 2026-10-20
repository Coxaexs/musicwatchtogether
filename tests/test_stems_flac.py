"""Stems stored as lossless FLAC (<hash>.stems.flac, <hash>.gp.flac) read like the raw arrays."""

import json
import os
import subprocess
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import stems  # noqa: E402


def write_flac(array, path):
    """(k, n, 2) int16 -> 2k-channel FLAC, like stems-sync does."""
    k, n, _ = array.shape
    pcm = np.ascontiguousarray(array.transpose(1, 0, 2).reshape(n, 2 * k)).astype("<i2").tobytes()
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "s16le", "-ar", str(stems.SR),
                    "-ac", str(2 * k), "-i", "pipe:0", "-c:a", "flac", path], input=pcm, check=True)


class FlacStemsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (stems.CACHE_DIR, stems.CACHE_BYTES)
        stems.CACHE_DIR = self.tmp.name
        rng = np.random.default_rng(5)
        self.audio = (rng.standard_normal((48000 * 3, 2)) * 2000).astype(np.int16)
        self.digest = stems.track_hash(self.audio)
        self.main = (rng.standard_normal((3, len(self.audio), 2)) * 500).astype(np.int16)
        self.extra = (rng.standard_normal((2, len(self.audio), 2)) * 300).astype(np.int16)
        write_flac(self.main, stems.stems_flac_path(self.digest))
        write_flac(self.extra, stems.extra_flac_path(self.digest))
        inst = np.clip(self.audio.astype(np.int32) - self.main[2], -32768, 32767).astype(np.int16)
        stems.write_instrumental(self.audio, self.main, self.digest)
        with open(os.path.join(self.tmp.name, "index.json"), "w") as f:
            json.dump({"tracks": {"yt:abc": {"hash": self.digest, "title": "x"}}, "pins": {}}, f)

    def tearDown(self):
        stems.CACHE_DIR, stems.CACHE_BYTES = self.saved
        self.tmp.cleanup()

    def test_read_back_bit_exact(self):
        self.assertTrue(stems.has_stems(self.digest))
        self.assertEqual(stems.lookup(["yt:abc"]), self.digest)
        np.testing.assert_array_equal(stems.load_stems(self.digest), self.main)
        np.testing.assert_array_equal(stems.extras(keys=["yt:abc"]), self.extra)
        self.assertEqual(stem_frames := stems.stem_frames(self.digest), len(self.audio))
        audio, st = stems.local_audio(["yt:abc"])
        np.testing.assert_array_equal(st, self.main)
        self.assertEqual(len(audio), len(self.audio))

    def test_library_status_and_cleanup_know_flac(self):
        lib = stems.library()
        self.assertEqual(len(lib), 1)
        self.assertTrue(lib[0]["extra"])
        self.assertEqual(lib[0]["seconds"], 3)
        self.assertEqual(stems.status()["library"], 1)
        stems.CACHE_BYTES = 0
        stems._prune()
        self.assertFalse(any(os.path.exists(p) for p in stems.song_files(self.digest)))


class LibrarySourceTests(unittest.TestCase):
    """A library song: FLAC stems + a .src naming its download, no instrumental."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = stems.CACHE_DIR
        stems.CACHE_DIR = self.tmp.name
        self.song = os.path.join(self.tmp.name, "song.flac")
        rng = np.random.default_rng(7)
        pcm = (rng.standard_normal((48000 * 4, 2)) * 1500).astype("<i2")
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "s16le", "-ar", "48000", "-ac", "2",
                        "-i", "pipe:0", self.song], input=pcm.tobytes(), check=True)
        self.audio = stems.decode(self.song)
        self.digest = stems.track_hash(self.audio)
        self.main = (rng.standard_normal((3, len(self.audio), 2)) * 400).astype(np.int16)
        write_flac(self.main, stems.stems_flac_path(self.digest))
        with open(stems.source_path(self.digest), "w") as f:
            f.write(self.song)
        with open(os.path.join(self.tmp.name, "index.json"), "w") as f:
            json.dump({"tracks": {"yt:lib": {"hash": self.digest, "title": "x"}}, "pins": {}}, f)

    def tearDown(self):
        stems.CACHE_DIR = self.saved
        self.tmp.cleanup()

    def test_rebuilt_from_the_download(self):
        audio, st = stems.local_audio(["yt:lib"])
        np.testing.assert_array_equal(audio, self.audio)
        np.testing.assert_array_equal(st, self.main)

    def test_a_changed_download_is_not_trusted(self):
        os.remove(self.song)
        self.assertIsNone(stems.local_audio(["yt:lib"]))


if __name__ == "__main__":
    unittest.main()
