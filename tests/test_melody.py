import unittest

import numpy as np

import melody


def sung(notes):
    """Back-to-back held notes (midi, seconds) at melody.RATE, with harmonics."""
    parts = []
    for midi, seconds in notes:
        t = np.arange(int(melody.RATE * seconds)) / melody.RATE
        hz = 440 * 2 ** ((midi - 69) / 12)
        tone = np.sin(2 * np.pi * hz * t) + 0.5 * np.sin(4 * np.pi * hz * t) if midi else 0 * t
        parts.append(0.3 * tone)
    return np.concatenate(parts).astype(np.float32)


class MelodyTests(unittest.TestCase):
    def test_reads_held_notes(self):
        x = sung([(0, 0.3), (60, 0.5), (64, 0.5), (0, 0.3), (67.3, 0.6), (0, 0.3)])
        notes = melody.notes_from(*melody.pitch_track(x))
        found = [(notes[i], notes[i + 1], notes[i + 2]) for i in range(0, len(notes), 3)]
        self.assertEqual([round(m) for _, _, m in found], [60, 64, 67])
        self.assertAlmostEqual(found[2][2], 67.3, delta=0.1)
        # Starts within a couple of hops of where each note began.
        self.assertAlmostEqual(found[0][0], 300, delta=40)
        self.assertAlmostEqual(found[2][0], 1600, delta=40)

    def test_silence_has_no_notes(self):
        self.assertEqual(melody.notes_from(*melody.pitch_track(np.zeros(melody.RATE, np.float32))), [])


if __name__ == "__main__":
    unittest.main()
