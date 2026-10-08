"""Real DJ with stems: the vocal map, hooks, opportunity planning, routines."""
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
P = realdj.PHRASE
# intro, intro, CHORUS, CHORUS, verse, CHORUS, CHORUS, outro
SHAPE = [0.15, 0.3, 1.0, 1.0, 0.45, 1.0, 1.0, 0.2]
BIG = {2, 3, 5, 6}


def words_for(shape):
    """Sung words in the choruses, like a real line: in every bar a short word
    on the one (0.6 beat), a breath, then a longer phrase from beat 2 (2.4
    beats), then a breath before the next bar."""
    out = []
    for k, level in enumerate(shape):
        if k in BIG:
            for bar in range(8):
                out.append((k * P + bar * 4, 0.6))
                out.append((k * P + bar * 4 + 1, 2.4))
    return out


def make_song(path, bpm, shape=SHAPE):
    """Writes the mix as a WAV and returns its (drums, bass, vocals) stems,
    scaled exactly like the WAV."""
    beat = 60.0 / bpm
    first = 0.5
    n = int((len(shape) * P * beat + 1.5) * SR)
    t = np.arange(n) / SR
    level_at = np.array([shape[min(len(shape) - 1, int(max(0.0, x - first) // (P * beat)))] for x in t[::480]])
    level = np.repeat(level_at, 480)[:n]
    drums = np.zeros(n)
    kick_len = int(0.12 * SR)
    kt = np.arange(kick_len) / SR
    kick = np.sin(2 * np.pi * (50 + 80 * np.exp(-kt / 0.02)) * kt) * np.exp(-kt / 0.05)
    for start in np.arange(first, n / SR - 0.2, beat):
        i = int(start * SR)
        lv = shape[min(len(shape) - 1, int((start - first) // (P * beat)))]
        drums[i:i + kick_len] += kick[: n - i] * (0.15 + 0.85 * lv)
    bass = level * 0.25 * np.sin(2 * np.pi * 110 * t)
    other = level * 0.12 * np.sin(2 * np.pi * 880 * t)
    vocals = np.zeros(n)
    for b, length in words_for(shape):
        a = int((first + b * beat) * SR)
        m = int(length * beat * SR)
        env = np.minimum(1.0, np.minimum(np.arange(m), np.arange(m)[::-1]) / (0.01 * SR))
        vocals[a:a + m] += 0.4 * env * np.sin(2 * np.pi * 440 * t[a:a + m])
    verse = int((first + 4 * P * beat) * SR), int((first + 5 * P * beat) * SR)
    vocals[verse[0]:verse[1]] += 0.12 * np.sin(2 * np.pi * 330 * t[verse[0]:verse[1]])   # a low hum
    mix = drums + bass + other + vocals
    scale = 0.7 * 32767 / max(1e-9, np.abs(mix).max())
    pcm = (np.stack([mix, mix], axis=1) * scale).astype("<i2")
    with wave.open(path, "wb") as f:
        f.setnchannels(2)
        f.setsampwidth(2)
        f.setframerate(SR)
        f.writeframes(pcm.tobytes())
    st = np.stack([np.stack([x, x], axis=1) for x in (drums, bass, vocals)]) * scale
    return st.astype(np.int16)


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


class RoutineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.files, cls.stems = [], {}
        for name, bpm in (("a", 124), ("b", 125), ("c", 123)):
            path = os.path.join(cls.tmp.name, name + ".wav")
            cls.stems[path] = make_song(path, bpm)
            cls.files.append(path)
        cls._cache = dj.mixer.cache
        dj.mixer.cache = dj.mixer.AnalysisCache(os.path.join(cls.tmp.name, "cache.json"))
        cls._env = os.environ.get("DJ_STEMS")
        os.environ["DJ_STEMS"] = "0"
        # Loading attaches the song's stems, as the stems cache would.
        cls._load = dj.DJSession.load
        stems_by_file = cls.stems

        async def load(self, deck_name, item, *args, **kwargs):
            result = await cls._load(self, deck_name, item, *args, **kwargs)
            deck = self.engine.decks[deck_name]
            st = stems_by_file.get(item.get("file"))
            if st is not None and deck.loaded:
                n = len(deck.audio)
                deck.stems = st[:, :n] if st.shape[1] >= n else np.pad(st, ((0, 0), (0, n - st.shape[1]), (0, 0)))
                deck.stems_status = "ready"
                for key in ("camelot",):
                    deck.meta[key] = "8A"
            return result
        dj.DJSession.load = load

    @classmethod
    def tearDownClass(cls):
        dj.DJSession.load = cls._load
        dj.mixer.cache = cls._cache
        if cls._env is None:
            os.environ.pop("DJ_STEMS", None)
        else:
            os.environ["DJ_STEMS"] = cls._env
        cls.tmp.cleanup()

    def two_decks(self):
        session = dj.DJSession("t", "test", Crate([]))
        asyncio.run(session.load("A", {"title": "a", "file": self.files[0], "keys": [self.files[0]]}))
        asyncio.run(session.load("B", {"title": "b", "file": self.files[1], "keys": [self.files[1]]}))
        return session, session.engine.decks["A"], session.engine.decks["B"]

    # ---- the keyframes ----

    def test_script_glides_and_holds(self):
        script = [{"at": 0, "ramp": 4, "set": {"xf": 1.0, "in.drums": 1.0}},
                  {"at": 8, "ramp": 0, "set": {"out.vocals": 0.0}}]
        start = dj.script_at(script, 0)
        self.assertEqual((start["xf"], start["in.drums"], start["out.vocals"]), (0.0, 0.0, 1.0))
        mid = dj.script_at(script, 2)
        self.assertAlmostEqual(mid["xf"], 0.5, places=2)
        held = dj.script_at(script, 7)
        self.assertEqual((held["xf"], held["in.drums"], held["out.vocals"]), (1.0, 1.0, 1.0))
        self.assertEqual(dj.script_at(script, 8)["out.vocals"], 0.0)
        self.assertEqual(dj.script_at([], 3), dj.SCRIPT_START)

    # ---- the vocal map ----

    def test_hooks_are_the_words_after_a_breath(self):
        _session, a, _b = self.two_decks()
        hooks = realdj.find_hooks(a)
        self.assertGreaterEqual(len(hooks), 6)
        beat = 60.0 / 124
        sung = {0.5 + b * beat: length for b, length in words_for(SHAPE)}   # true start (s) -> beats
        for hook in hooks:
            # The loop starts on the word itself, whatever the analysed grid says.
            nearest = min(sung, key=lambda t: abs(t - hook["time"]))
            self.assertLess(abs(nearest - hook["time"]), 0.03, hook)
            self.assertEqual(hook["length"], 1 if sung[nearest] < 1 else 2, hook)
        # Nothing in the hummed verse or the intro.
        self.assertFalse([h for h in hooks if h["beat"] // P not in BIG], hooks)

    def test_vocal_map_follows_the_singing(self):
        _session, a, _b = self.two_decks()
        prof = realdj.beat_profile(a)
        word_beat = words_for(SHAPE)[0][0]
        self.assertGreater(prof["vocals"][word_beat * 2], 0.6)
        self.assertLess(prof["vocals"][(word_beat - 1) * 2 + 1], 0.2)   # the breath before it

    # ---- planning ----

    def test_planner_goes_back_and_forth_on_singing_songs(self):
        _session, a, b = self.two_decks()
        plan = realdj.plan_transition(a, b, 0, [], random.Random(2), "club")
        self.assertEqual(plan.style, dj.ROUTINE)
        self.assertEqual(plan.move, "trade")
        self.assertEqual(plan.label, "Back and forth")
        self.assertEqual(plan.start_beat % P, 0)
        start = plan.start_beat // P
        # The old song sings in the phrase where its vocal rides the new music.
        self.assertGreater(realdj.structure(a)["phrases"][start + 1]["vocals"], 0.3)
        # The new song lands on a big phrase three phrases in.
        sb = realdj.structure(b)
        cue_phrase = round(b.beat_at(plan.in_cue)) // P
        self.assertTrue(sb["phrases"][cue_phrase + 3]["big"])
        # A hook loops in the build, inside the old song's a cappella half phrase.
        self.assertTrue(plan.hooks)
        beat, length, repeats, at = plan.hooks[0]
        self.assertAlmostEqual(at, a.time_of_beat(beat), delta=0.6 * a.beat_seconds)
        self.assertTrue(plan.start_beat + 2 * P + 8 <= beat < plan.start_beat + 3 * P - 6)
        # The old song goes a cappella from that hook's bar.
        acappella = [f for f in plan.script if f["set"].get("out.other") == 0.0 and f["at"] >= 2 * P]
        self.assertEqual(acappella[0]["at"] + 1, (beat - plan.start_beat) // 4 * 4)
        self.assertLessEqual(beat + length * repeats, plan.start_beat + 3 * P - 2)

    def test_planner_is_not_a_dice_roll(self):
        _session, a, b = self.two_decks()
        moves = {realdj.plan_transition(a, b, 0, [], random.Random(seed), "club").move for seed in range(12)}
        self.assertEqual(moves, {"trade"})

    def test_variety_moves_on_after_two_of_the_same(self):
        _session, a, b = self.two_decks()
        plan = realdj.plan_transition(a, b, 0, ["trade", "trade"], random.Random(1), "club")
        self.assertNotEqual(plan.move, "trade")

    def test_clashing_keys_never_layer_the_music(self):
        _session, a, b = self.two_decks()
        b.meta["camelot"] = "2B"
        for seed in range(6):
            plan = realdj.plan_transition(a, b, 0, [], random.Random(seed), "club")
            self.assertIn(plan.move, ("drum_bridge", "tease", "loop_roll"))
            if plan.script:
                for frame in plan.script:
                    self.assertFalse(frame["set"].get("in.other") and frame["at"] < plan.beats - 3, frame)

    def test_without_stems_it_plans_as_before(self):
        _session, a, b = self.two_decks()
        a.stems = None
        plan = realdj.plan_transition(a, b, 0, [], random.Random(1), "club")
        self.assertNotEqual(plan.style, dj.ROUTINE)

    # ---- performing ----

    def run_until(self, engine, tr, beat):
        for _ in range(20000):
            if tr.done or (tr.started and tr.elapsed >= beat):
                return
            engine.render()
        self.fail("transition never got there")

    def test_back_and_forth_moves_the_stems(self):
        session, a, b = self.two_decks()
        engine = session.engine
        a.playing, b.sync = True, True
        a.seek(a.time_of_beat(4 * P - 2))
        engine.master_name = "A"
        engine.xfader = -1.0
        script, beats = realdj.trade_script("out")
        tr = dj.Transition(a, b, dj.ROUTINE, beats, 4 * P)
        tr.script = script
        engine.transition = tr
        self.run_until(engine, tr, 16)
        self.assertEqual((a.stem_gain["drums"], b.stem_gain["drums"], b.stem_gain["vocals"]), (0.0, 1.0, 0.0))
        self.assertTrue(b.playing)
        self.run_until(engine, tr, P + 16)            # the old vocal on the new music
        self.assertEqual((a.stem_gain["vocals"], a.stem_gain["other"], b.stem_gain["other"]), (1.0, 0.0, 1.0))
        self.run_until(engine, tr, 2 * P + 8)         # the old song comes back on its own
        self.assertEqual(set(a.stem_gain.values()), {1.0})
        self.assertEqual(set(b.stem_gain.values()), {0.0})
        self.run_until(engine, tr, 2 * P + 24)        # a cappella over the new drums and bass
        self.assertEqual((a.stem_gain["drums"], a.stem_gain["vocals"], b.stem_gain["bass"]), (0.0, 1.0, 1.0))
        self.run_until(engine, tr, beats + 1)
        for _ in range(5):
            engine.render()
        self.assertTrue(tr.done)
        self.assertFalse(a.playing)
        self.assertTrue(b.playing)
        self.assertEqual(set(b.stem_gain.values()), {1.0})
        self.assertAlmostEqual(engine.xfader, 1.0)

    def test_routine_falls_back_to_a_blend_without_stems(self):
        session, a, b = self.two_decks()
        b.stems = None
        engine = session.engine
        a.playing = True
        tr = dj.Transition(a, b, dj.ROUTINE, 98, None)
        tr.script = realdj.trade_script("out")[0]
        engine.transition = tr
        engine.render()
        self.assertEqual((tr.style, tr.beats), ("blend", 32))

    def test_hook_repeat_stutters_with_the_drums_out_and_lands_on_time(self):
        session, a, _b = self.two_decks()
        engine = session.engine
        a.playing = True
        a.seek(a.time_of_beat(2 * P - 2))
        engine.master_name = "A"
        real = realdj.RealDJ(session, rng=random.Random(1), vibe="club")
        real._hook_loop(a, 2 * P, 1, 3, drop_drums=True)
        looped = drums_cut = False
        for _ in range(250):                        # 5 s
            engine.render()
            looped |= a.loop_on
            drums_cut |= a.loop_on and a.stem_gain["drums"] == 0.0
        self.assertTrue(looped and drums_cut)
        self.assertFalse(a.loop_on)
        self.assertEqual(a.stem_gain["drums"], 1.0)
        expected = a.time_of_beat(2 * P - 2) + 250 * dj.BLOCK / SR
        self.assertAlmostEqual(a.seconds, expected, delta=0.05)

    def test_a_set_with_stems_goes_back_and_forth(self):
        session = dj.DJSession("t", "test", Crate(self.files), auto=True)
        session.live = True
        session.mode = "real"
        session.real = realdj.RealDJ(session, rng=random.Random(3), vibe="club")
        engine = session.engine
        styles, titles = [], []

        seen = {}

        async def run():
            seconds = 0.0
            while seconds < 480 and sum(t.done for t in seen.values()) < 2:
                await session._tick()
                await asyncio.sleep(0)
                for task in list(session.load_tasks.values()):
                    if not task.done():
                        await task
                with engine.lock:
                    tr = engine.transition
                    if tr and tr.started:
                        seen[id(tr)] = tr
                        if (tr.style, tr.label) not in styles:
                            styles.append((tr.style, tr.label))
                for _ in range(12):
                    engine.render()
                seconds += 0.24
                for t in [d.info.get("title") for d in engine.decks.values() if d.playing]:
                    if t and t not in titles:
                        titles.append(t)
        asyncio.run(run())
        self.assertGreaterEqual(len(titles), 3, titles)
        self.assertIn((dj.ROUTINE, "Back and forth"), styles, styles)
        self.assertIn("hook_repeat", session.real.touches)


if __name__ == "__main__":
    unittest.main()
