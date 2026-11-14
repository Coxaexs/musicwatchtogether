"""VINYL STOP & START: pause and play slow a deck down and bring it up like a record."""
import os
import sys
import time
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dj  # noqa: E402
import tests.test_realdj_routines as routines  # noqa: E402

SR = dj.SR
BLOCK = 960


def deck_with_tone():
    deck = dj.Deck("A")
    t = np.arange(SR * 20) / SR
    tone = (np.sin(2 * np.pi * 440 * t) * 12000).astype(np.int16)
    deck.audio = np.stack([tone, tone], axis=1)
    deck.keylock = False
    return deck


def run(deck, seconds):
    """Rates of the blocks rendered over `seconds`."""
    rates, now = [], time.monotonic()
    for _ in range(int(seconds * SR / BLOCK)):
        deck.read(BLOCK, now)
        rates.append(deck.rate_now)
    return rates


class SpinTests(unittest.TestCase):
    def test_spin_down_slows_to_a_stop_then_the_deck_is_stopped(self):
        deck = deck_with_tone()
        deck.playing = True
        run(deck, 0.2)
        self.assertAlmostEqual(deck.rate_now, 1.0, places=3)
        deck.spin_down()
        rates = run(deck, dj.SPIN_STOP + 0.1)
        self.assertTrue(all(b <= a + 1e-9 for a, b in zip(rates, rates[1:])), "never speeds up while stopping")
        self.assertGreater(rates[len(rates) // 4], 0.3)               # a slide, not a cut
        self.assertEqual(deck.rate_now, 0.0)
        self.assertFalse(deck.playing)
        self.assertIsNone(deck.brake)

    def test_spin_up_comes_up_to_speed(self):
        deck = deck_with_tone()
        deck.spin_up()
        rates = run(deck, dj.SPIN_START + 0.1)
        self.assertLess(rates[0], 0.3)
        self.assertTrue(all(b >= a - 1e-9 for a, b in zip(rates, rates[1:])))
        self.assertAlmostEqual(rates[-1], 1.0, places=3)
        self.assertTrue(deck.playing)
        self.assertIsNone(deck.brake)

    def test_play_while_still_slowing_picks_up_from_there(self):
        deck = deck_with_tone()
        deck.playing = True
        run(deck, 0.1)
        deck.spin_down()
        run(deck, dj.SPIN_STOP / 2)
        midway = deck.rate_now
        deck.spin_up()
        rates = run(deck, 0.05)
        self.assertGreater(rates[0], midway * 0.8)                    # no drop to zero on the way back up


class SetPauseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        routines.RoutineTests.setUpClass()
        cls.files = routines.RoutineTests.files

    @classmethod
    def tearDownClass(cls):
        routines.RoutineTests.tearDownClass()

    two_decks = routines.RoutineTests.two_decks

    def test_pause_and_resume_keep_two_playing_decks_together(self):
        session, a, b = self.two_decks()
        a.playing = b.playing = True
        a.seek(10.0)
        b.seek(20.0)
        gap = b.seconds - a.seconds
        session.pause()
        self.assertTrue(session.paused)
        self.assertTrue(a.playing and a.brake)                        # still turning, slowing
        for _ in range(int((dj.SPIN_STOP + 0.2) * SR / BLOCK)):
            session.engine.render()
        self.assertFalse(a.playing or b.playing)
        session.resume()
        self.assertTrue(a.brake and a.brake[0] == "start")
        for _ in range(int((dj.SPIN_START + 0.2) * SR / BLOCK)):
            session.engine.render()
        self.assertAlmostEqual(b.seconds - a.seconds, gap, delta=0.005)
        self.assertAlmostEqual(a.rate_now, a.play_rate(), places=3)

    def test_off_stops_at_once(self):
        session, a, _b = self.two_decks()
        session.spin = False
        a.playing = True
        session.pause()
        self.assertFalse(a.playing)
        session.resume()
        self.assertTrue(a.playing)
        self.assertIsNone(a.brake)


if __name__ == "__main__":
    unittest.main()
