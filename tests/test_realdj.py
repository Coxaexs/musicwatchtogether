import asyncio
import os
import random
import sys
import tempfile
import unittest
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dj  # noqa: E402
import realdj  # noqa: E402

SR = dj.SR


def song(path, bpm, shape, phrase_beats=32):
    """A kick + bass + pad song whose 8-bar phrases follow `shape` (0..1 loudness)."""
    beat = 60.0 / bpm
    phrase = phrase_beats * beat
    n = int((len(shape) * phrase + 1.0) * SR)
    t = np.arange(n) / SR
    audio = np.zeros(n)
    kick_len = int(0.12 * SR)
    kt = np.arange(kick_len) / SR
    kick = np.sin(2 * np.pi * (50 + 80 * np.exp(-kt / 0.02)) * kt) * np.exp(-kt / 0.05)
    for start in np.arange(0.5, n / SR - 0.2, beat):
        level = shape[min(len(shape) - 1, int((start - 0.5) // phrase))]
        i = int(start * SR)
        audio[i:i + kick_len] += kick[: n - i] * (0.15 + 0.85 * level)
    loud = np.array([shape[min(len(shape) - 1, int(max(0, x - 0.5) // phrase))] for x in t[::480]])
    loud = np.repeat(loud, 480)[:n]
    audio += loud * 0.25 * np.sin(2 * np.pi * 110 * t) + loud * 0.12 * np.sin(2 * np.pi * 880 * t)
    pcm = (np.stack([audio, audio], axis=1) / max(1e-9, np.abs(audio).max()) * 0.7 * 32767).astype("<i2")
    with wave.open(path, "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(SR)
        out.writeframes(pcm.tobytes())


class Crate:
    def __init__(self, files):
        self.files = list(files)

    def items(self):
        return [{"title": os.path.basename(f), "keys": [f]} for f in self.files]

    async def take(self, index=0):
        if not 0 <= index < len(self.files):
            return None
        path = self.files.pop(index)
        return {"title": os.path.basename(path), "file": path, "keys": [path]}

    async def add(self, query, requester=None):
        return query


# intro, intro, CHORUS, CHORUS, verse, CHORUS, CHORUS, outro
SHAPE = [0.15, 0.3, 1.0, 1.0, 0.45, 1.0, 1.0, 0.2]


class RealDJTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.files = []
        for name, bpm in (("a", 124), ("b", 126), ("c", 122)):
            path = os.path.join(cls.tmp.name, name + ".wav")
            song(path, bpm, SHAPE)
            cls.files.append(path)
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

    def loaded(self, path, name="A"):
        session = dj.DJSession("t", "test", Crate([]))
        asyncio.run(session.load(name, {"title": "x", "file": path, "keys": [path]}))
        return session, session.engine.decks[name]

    def test_structure_finds_the_choruses(self):
        _session, deck = self.loaded(self.files[0])
        s = realdj.structure(deck)
        self.assertIsNotNone(s)
        big = [p["big"] for p in s["phrases"]]
        self.assertEqual(big[:7], [False, False, True, True, False, True, True])
        self.assertEqual(s["entry"], 1)                 # one phrase before the first chorus
        self.assertEqual(s["exit"], 7)                  # after the second chorus
        played = deck.time_of_beat(s["exit_beat"]) - deck.time_of_beat(s["entry_beat"])
        self.assertTrue(realdj.MIN_PLAY <= played <= realdj.MAX_PLAY, played)

    def test_next_song_follows_tempo_key_and_energy(self):
        out = {"bpm": 124, "camelot": "8A", "energy": 0.6}
        same = {"bpm": 125, "camelot": "8A", "energy": 0.62}
        clash = {"bpm": 125, "camelot": "2B", "energy": 0.62}
        far = {"bpm": 90, "camelot": "8A", "energy": 0.62}
        score = lambda m: realdj.score_candidate(m, out, 0.62, [])
        self.assertLess(score(same), score(clash))
        self.assertLess(score(same), score(far))
        self.assertLess(realdj.energy_target(30), 0.9)
        self.assertGreater(realdj.energy_target(30), realdj.energy_target(0))

    def test_plan_builds_into_the_next_chorus(self):
        session = dj.DJSession("t", "test", Crate([]))
        asyncio.run(session.load("A", {"title": "a", "file": self.files[0], "keys": [self.files[0]]}))
        asyncio.run(session.load("B", {"title": "b", "file": self.files[1], "keys": [self.files[1]]}))
        a, b = session.engine.decks["A"], session.engine.decks["B"]
        plan = realdj.plan_transition(a, b, 0, [], random.Random(1))
        sb = realdj.structure(b)
        cue_beat = round(b.beat_at(plan.in_cue))
        if plan.style in realdj.DROP_STYLES:
            self.assertEqual(cue_beat, sb["first_big_beat"])
        else:
            self.assertEqual(cue_beat + plan.beats, sb["first_big_beat"])
        self.assertEqual(plan.start_beat % 8, 0)

    def test_vibe_changes_how_often_it_drops(self):
        session = dj.DJSession("t", "test", Crate([]))
        asyncio.run(session.load("A", {"title": "a", "file": self.files[0], "keys": [self.files[0]]}))
        asyncio.run(session.load("B", {"title": "b", "file": self.files[1], "keys": [self.files[1]]}))
        a, b = session.engine.decks["A"], session.engine.decks["B"]
        a.meta["energy"], b.meta["energy"] = 0.6, 0.6
        drops = {}
        for vibe in ("chill", "hype"):
            rng = random.Random(7)
            picks = [realdj.choose_style(a, b, None, None, [], rng, vibe)[0] for _ in range(300)]
            drops[vibe] = sum(p in ("loop_roll", "cut", "echo") for p in picks)
        self.assertLess(drops["chill"], 10)
        self.assertGreater(drops["hype"], 100)

    def test_slip_loop_stutters_and_lands_on_time(self):
        session, deck = self.loaded(self.files[0])
        engine = session.engine
        deck.playing = True
        deck.seek(deck.time_of_beat(60))
        engine.master_name = "A"
        real = realdj.RealDJ(session, rng=random.Random(1), vibe="hype")
        real._fl_slip_loop(deck, 64, None)
        looped = False
        for _ in range(250):                 # 5 s
            engine.render()
            looped |= deck.loop_on
        self.assertTrue(looped)
        self.assertFalse(deck.loop_on)
        # 5 s of playing from beat 60, as if nothing happened (slip).
        expected = deck.time_of_beat(60) + 250 * dj.BLOCK / SR
        self.assertAlmostEqual(deck.seconds, expected, delta=0.05)

    def test_loop_roll_drop_fires_the_sampler(self):
        session = dj.DJSession("t", "test", Crate([]))
        asyncio.run(session.load("A", {"title": "a", "file": self.files[0], "keys": [self.files[0]]}))
        asyncio.run(session.load("B", {"title": "b", "file": self.files[1], "keys": [self.files[1]]}))
        engine = session.engine
        engine.decks["A"].playing = True
        engine.xfader = -1
        tr = dj.Transition(engine.decks["A"], engine.decks["B"], "loop_roll", 8, None)
        tr.samples = {"start": "riser", "drop": "boom"}
        engine.transition = tr
        fired = 0
        for _ in range(300):
            before = len(engine.voices)
            engine.render()
            fired += max(0, len(engine.voices) - before)
        self.assertEqual(fired, 2)
        self.assertTrue(engine.decks["B"].playing)

    def test_a_short_set_is_performed(self):
        session = dj.DJSession("t", "test", Crate(self.files), auto=True)
        session.live = True
        session.mode = "real"
        session.real = realdj.RealDJ(session, rng=random.Random(3), vibe="hype")
        engine = session.engine
        changes, titles = 0, []

        async def run():
            nonlocal changes
            seconds = 0.0
            while seconds < 420 and changes < 2:
                await session._tick()
                await asyncio.sleep(0)
                for task in list(session.load_tasks.values()):
                    if not task.done():
                        await task
                for _ in range(12):             # 0.24 s of audio per tick
                    engine.render()
                seconds += 0.24
                with engine.lock:
                    events = list(engine.events)
                for kind, _x in events:
                    if kind == "transition_done":
                        changes += 1
                on = [d.info.get("title") for d in engine.decks.values() if d.playing]
                for t in on:
                    if t and t not in titles:
                        titles.append(t)
            return seconds
        seconds = asyncio.run(run())
        self.assertGreaterEqual(len(titles), 3, titles)
        self.assertTrue(session.real.touches, "hype played no live touches")
        # Songs are cut to their good part: three songs in well under 3 × 125 s.
        self.assertLess(seconds, 3 * 125)


if __name__ == "__main__":
    unittest.main()
