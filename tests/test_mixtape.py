"""Transitions are recorded for the mix reviewer: three tracks (A, B, mix) with what the mix was."""
import json
import os
import subprocess
import tempfile
import time
import unittest
from unittest import mock

import numpy as np

import dj
import mixtape
import tests.test_realdj_routines as routines

P = routines.P


class MixtapeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        routines.RoutineTests.setUpClass()
        cls.files = routines.RoutineTests.files

    @classmethod
    def tearDownClass(cls):
        routines.RoutineTests.tearDownClass()

    two_decks = routines.RoutineTests.two_decks

    def record(self, folder, b_late_ms=0.0, beats=16):
        session, a, b = self.two_decks()
        engine = session.engine
        engine.recorder = mixtape.MixRecorder(lambda: {"room": "test-room", "mode": "real"})
        a.playing = True
        a.seek(a.time_of_beat(2 * P))
        engine.master_name, engine.xfader = "A", -1.0
        # Starts once the 8 s before it are in the recorder's memory.
        tr = dj.Transition(a, b, "blend", beats, a.beat_at(a.seconds + mixtape.PRE + 1))
        engine.transition = tr
        with mock.patch.object(mixtape, "FOLDER", folder):
            for _ in range(int((mixtape.PRE + 6) * 50)):
                engine.render()
                if tr.started:
                    break
            self.assertTrue(tr.started)
            if b_late_ms:
                b.seek(b.seconds - b_late_ms / 1000)          # a deck that came in late: its beats trail
            for _ in range(3000):
                engine.render()
                if engine.recorder.take is None and tr.done:
                    break
            for _ in range(50):
                time.sleep(0.05)
                if any(n.endswith(".json") for n in os.listdir(folder)):
                    break
        return tr

    def test_a_transition_is_recorded_with_both_decks_and_what_it_was(self):
        with tempfile.TemporaryDirectory() as folder:
            tr = self.record(folder)
            names = sorted(os.listdir(folder))
            self.assertEqual([n.rsplit(".", 1)[1] for n in names], ["flac", "json"])
            meta = json.load(open(os.path.join(folder, names[1])))
            self.assertEqual((meta["style"], meta["room"], meta["tracks"]), ("blend", "test-room", ["A", "B", "mix"]))
            self.assertEqual((meta["out"]["deck"], meta["inn"]["deck"]), ("A", "B"))
            self.assertAlmostEqual(meta["start"], mixtape.PRE, delta=0.5)
            length = tr.beats * tr.out.beat_seconds
            self.assertAlmostEqual(meta["end"] - meta["start"], length, delta=1.0)
            probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=channels,sample_rate:format=duration",
                                    "-of", "json", os.path.join(folder, names[0])], capture_output=True, text=True)
            info = json.loads(probe.stdout)
            self.assertEqual((info["streams"][0]["channels"], int(info["streams"][0]["sample_rate"])), (3, 24000))
            self.assertAlmostEqual(float(info["format"]["duration"]), meta["end"] + mixtape.POST, delta=0.5)

    def test_nothing_is_recorded_without_a_transition(self):
        with tempfile.TemporaryDirectory() as folder:
            session, a, _b = self.two_decks()
            session.engine.recorder = mixtape.MixRecorder()
            a.playing = True
            with mock.patch.object(mixtape, "FOLDER", folder):
                for _ in range(500):
                    session.engine.render()
            self.assertIsNone(session.engine.recorder.take)
            self.assertEqual(os.listdir(folder), [])


if __name__ == "__main__":
    unittest.main()
