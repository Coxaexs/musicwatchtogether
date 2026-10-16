"""Pausing a set, and bringing booths back after a restart."""
import asyncio
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dj  # noqa: E402
import tests.test_realdj_routines as routines  # noqa: E402

P = routines.P


class ResumeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        routines.RoutineTests.setUpClass()
        cls.files = routines.RoutineTests.files
        cls.by_title = {os.path.basename(f): f for f in cls.files}
        cls.by_title.update({"a": cls.files[0], "b": cls.files[1]})   # two_decks' titles
        cls._resolver = dj.resolver

        async def resolve(query):
            return {"audio_url": cls.by_title[query]}
        dj.resolver = resolve
        cls.tmp = tempfile.TemporaryDirectory()
        cls._state_file = dj.STATE_FILE
        dj.STATE_FILE = os.path.join(cls.tmp.name, "dj_sessions.json")

    @classmethod
    def tearDownClass(cls):
        dj.resolver = cls._resolver
        dj.STATE_FILE = cls._state_file
        cls.tmp.cleanup()
        routines.RoutineTests.tearDownClass()

    def setUp(self):
        if os.path.exists(dj.STATE_FILE):
            os.remove(dj.STATE_FILE)

    two_decks = routines.RoutineTests.two_decks

    def on_air(self, beat=2 * P):
        session, a, b = self.two_decks()
        engine = session.engine
        a.playing = True
        a.seek(a.time_of_beat(beat))
        engine.master_name = "A"
        engine.xfader = -1.0
        return session, engine, a, b

    # ---- pause / resume ----

    def test_pause_freezes_the_mix_and_resume_carries_on_from_there(self):
        session, engine, a, b = self.on_air()
        tr = dj.Transition(a, b, "stem_swap", 32, None)
        engine.transition = tr
        for _ in range(100):
            engine.render()
        elapsed, a_at, b_at = tr.elapsed, a.seconds, b.seconds
        self.assertTrue(tr.started and b.playing)
        session.pause()
        self.assertTrue(session.paused and engine.paused)
        for _ in range(200):
            engine.render()
        self.assertEqual(tr.elapsed, elapsed)                 # the mix stood still
        self.assertAlmostEqual(a.seconds, a_at, delta=0.05)   # so did the songs
        self.assertAlmostEqual(b.seconds, b_at, delta=0.05)
        self.assertFalse(a.playing or b.playing)
        session.resume()
        self.assertTrue(a.playing and b.playing and not engine.paused)
        for _ in range(50):
            engine.render()
        self.assertGreater(tr.elapsed, elapsed)

    def test_pausing_mid_hook_loop_keeps_the_loop_on_time(self):
        import random
        import realdj
        session, engine, a, _b = self.on_air(2 * P - 2)
        real = realdj.RealDJ(session, rng=random.Random(1))
        real._hook_loop(a, 2 * P, 1, 3)
        for _ in range(60):
            engine.render()
        self.assertTrue(a.loop_on)
        session.pause()
        for _ in range(300):                                   # 6 s paused
            engine.render()
        self.assertTrue(a.loop_on)                             # the timer didn't run out while paused
        session.resume()
        for _ in range(200):
            engine.render()
        self.assertFalse(a.loop_on)

    def test_touching_the_decks_resumes_the_set(self):
        session, engine, a, _b = self.on_air()
        session.pause()
        asyncio.run(session.handle({"op": "play", "deck": "A"}))
        self.assertFalse(session.paused)

    # ---- snapshots ----

    def test_snapshot_has_the_set(self):
        session, engine, a, b = self.on_air()
        session.mode, session.vibe, session.three = "real", "hype", True
        snap = session.snapshot()
        self.assertEqual((snap["mode"], snap["vibe"], snap["three"], snap["on_air"]), ("real", "hype", True, "A"))
        self.assertEqual(set(snap["decks"]), {"A", "B"})
        self.assertTrue(snap["decks"]["A"]["playing"])
        self.assertFalse(snap["decks"]["B"]["playing"])
        self.assertAlmostEqual(snap["decks"]["A"]["position"], a.seconds, delta=0.01)
        self.assertEqual(snap["decks"]["A"]["item"]["title"], a.info["title"])

    def test_snapshot_mid_mix_comes_back_on_the_song_that_had_the_room(self):
        session, engine, a, b = self.on_air()
        tr = dj.Transition(a, b, "blend", 32, None)
        engine.transition = tr
        for _ in range(10):
            engine.render()
        self.assertEqual(session.snapshot()["on_air"], "A")
        tr.elapsed = 20
        self.assertEqual(session.snapshot()["on_air"], "B")

    def test_restore_brings_the_set_back(self):
        old, engine, a, b = self.on_air(3 * P)
        old.mode, old.vibe, old.three = "real", "chill", True
        snap = old.snapshot()
        fresh = dj.DJSession("t2", "test", routines.Crate([]))
        asyncio.run(fresh.restore(snap))
        A, B = fresh.engine.decks["A"], fresh.engine.decks["B"]
        self.assertEqual((fresh.mode, fresh.vibe, fresh.three), ("real", "chill", True))
        self.assertIsNotNone(fresh.real)
        self.assertTrue(A.playing)
        self.assertFalse(B.playing)
        self.assertTrue(B.loaded)
        self.assertAlmostEqual(A.seconds, snap["decks"]["A"]["position"], delta=0.3)
        self.assertEqual(fresh.engine.master_name, "A")
        self.assertFalse(fresh.restoring)

    def test_a_paused_set_comes_back_paused(self):
        old, engine, a, b = self.on_air()
        old.pause()
        snap = old.snapshot()
        self.assertTrue(snap["paused"])
        fresh = dj.DJSession("t3", "test", routines.Crate([]))
        asyncio.run(fresh.restore(snap))
        self.assertTrue(fresh.paused and fresh.engine.paused)
        self.assertFalse(fresh.engine.decks["A"].playing)
        fresh.resume()
        self.assertTrue(fresh.engine.decks["A"].playing)

    # ---- the state file ----

    def test_state_is_saved_kept_on_shutdown_and_forgotten_on_stop(self):
        session, engine, a, b = self.on_air()
        session.kind = "discord"
        session.key = "123"
        dj.sessions["123"] = session
        session.save_state(force=True)
        self.assertIn("123", dj.read_state())
        asyncio.run(session.stop(forget=False))                 # the bot going down
        self.assertIn("123", dj.read_state())
        session.active = True
        dj.sessions["123"] = session
        asyncio.run(session.stop())                             # someone turned it off
        self.assertNotIn("123", dj.read_state())

    def test_restore_skips_old_and_unreachable_booths(self):
        dj.write_state({"999": {"kind": "discord", "saved_at": time.time() - 2 * dj.RESTORE_MAX_AGE},
                        "888": {"kind": "elsewhere", "saved_at": time.time()}})
        asyncio.run(dj.restore_sessions(None, wait_seconds=0))
        self.assertEqual(dj.read_state(), {})
        self.assertNotIn("999", dj.sessions)


if __name__ == "__main__":
    unittest.main()
