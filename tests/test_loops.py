"""The loop crate (loops.py), its voices on the engine, the borrowed groove, and the club's song map."""
import asyncio
import json
import math
import os
import random
import sys
import tempfile
import time
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import club  # noqa: E402
import dj  # noqa: E402
import loops  # noqa: E402
import realdj  # noqa: E402
import tests.test_realdj_routines as routines  # noqa: E402

SR = dj.SR
P = realdj.PHRASE


class KeysAndTempo(unittest.TestCase):
    def test_same_notes_need_no_shift(self):
        self.assertEqual(loops.key_shift("8A", "8B"), (0, 0.0))     # A minor over C major
        self.assertEqual(loops.key_shift("8A", "8A"), (0, 0.0))

    def test_a_neighbour_fits_as_it_is_and_further_keys_move(self):
        self.assertEqual(loops.key_shift("8A", "9A"), (0, 0.45))
        self.assertEqual(loops.key_shift("8B", "10B"), (2, 0.5))      # C major up a tone to D major
        self.assertIsNone(loops.key_shift("8B", None))

    def test_stretch_allows_half_and_double_time(self):
        factor, tempo = loops.stretch(120, 126)
        self.assertEqual(factor, 1.0)
        self.assertAlmostEqual(tempo, 1.05)
        self.assertEqual(loops.stretch(87, 174), (2.0, 1.0))
        self.assertIsNone(loops.stretch(120, 150))


def _tone(freqs, seconds, rate):
    t = np.arange(int(seconds * rate)) / rate
    return sum(np.sin(2 * np.pi * f * t) for f in freqs) / len(freqs)


def synthetic_parts(bpm=120.0, bars=40, rate=loops._RATE, riff_bars=(16, 20)):
    """A song at loops' analysis rate: drums all through, a vocal in bars 4-12, and a guitar
    riff (C, E, G stabs on a two-bar pattern) alone over the drums in riff_bars."""
    beat = 60.0 / bpm
    n = int(bars * 4 * beat * rate)
    drums = np.zeros(n, np.float32)
    hit = (np.exp(-np.arange(int(0.08 * rate)) / (0.02 * rate)) * np.sin(np.arange(int(0.08 * rate)) * 0.3)).astype(np.float32)
    for b in range(bars * 4):
        i = int(b * beat * rate)
        drums[i:i + len(hit)] += hit[: n - i] * (0.5 if b % 2 else 0.8)
    guitar = np.zeros(n, np.float32)
    pattern = [(0, 261.6), (1.5, 329.6), (3, 392.0), (4.5, 261.6), (6, 392.0)]      # beats into each two bars
    for bar in range(riff_bars[0], riff_bars[1], 2):
        for at, f in pattern:
            i = int((bar * 4 + at) * beat * rate)
            stab = _tone((f, f * 2, f * 1.5), 0.6 * beat, rate) * np.exp(-np.arange(int(0.6 * beat * rate)) / (0.15 * rate))
            guitar[i:i + len(stab)] += stab[: n - i] * 0.6
    vocals = np.zeros(n, np.float32)
    a, b = int(4 * 4 * beat * rate), int(12 * 4 * beat * rate)
    vocals[a:b] = _tone((220,), (b - a) / rate, rate) * 0.3
    return {"drums": drums, "bass": np.zeros(n, np.float32), "vocals": vocals, "guitar": guitar,
            "piano": np.zeros(n, np.float32)}


class Finding(unittest.TestCase):
    def test_finds_the_riff_where_it_plays_alone(self):
        found = loops.find(synthetic_parts(), 120.0, 0.0, "8B")
        guitar = [f for f in found if f["part"] == "guitar"]
        self.assertTrue(guitar, found)
        self.assertIn(guitar[0]["beat"], (64,))                 # bar 16, the riff's first
        self.assertGreater(guitar[0]["key_fit"], 0.45)

    def test_a_riff_out_of_its_songs_key_is_left_out(self):
        found = loops.find(synthetic_parts(), 120.0, 0.0, "2B")  # F# major: C E G don't belong
        self.assertFalse([f for f in found if f["part"] == "guitar"], found)

    def test_a_drum_groove_where_the_drums_play_alone(self):
        found = loops.find(synthetic_parts(), 120.0, 0.0, "8B")
        drums = [f for f in found if f["part"] == "drums"]
        self.assertTrue(drums)
        # Not where the vocal sings (bars 4-12), nor over the riff.
        for f in drums:
            self.assertFalse(4 * 4 - 16 < f["beat"] < 12 * 4, f)


def clicks(beats, bpm, every=1.0):
    """A loop of clicks on its beats, `beats` beats of `bpm` long."""
    n = int(round(beats * 60.0 / bpm * SR))
    audio = np.zeros((n, 2), np.float32)
    for k in range(int(beats / every)):
        i = int(round(k * every * 60.0 / bpm * SR))
        audio[i:i + 24] = 0.5
    return audio


class Crate:
    """A loop crate written to a temp index (and its ratings beside it), with render() making clicks."""

    def __init__(self, tmp, entries):
        self.path = os.path.join(tmp, "index.json")
        with open(self.path, "w") as f:
            json.dump({"loops": entries}, f)
        self.patches = [mock.patch.object(loops, "INDEX_FILE", self.path),
                        mock.patch.object(loops, "RATINGS_FILE", os.path.join(tmp, "ratings.json")),
                        mock.patch.object(loops, "render", lambda choice, bpm: clicks(16 * choice["factor"], bpm))]

    def __enter__(self):
        loops._index["mtime"] = None
        for p in self.patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self.patches:
            p.stop()
        loops._index["mtime"] = None
        loops._ratings["mtime"] = None
        loops.recent.clear()
        loops.played.clear()


def entry(part, bpm=124.0, camelot="8A", digest="feed" * 8, title=None):
    return {"id": f"{digest[:12]}-{part}-0", "digest": digest, "keys": [], "title": title or f"other {part}", "part": part,
            "bpm": bpm, "camelot": camelot, "energy": 0.6, "start": 10.0, "beats": 16, "score": 3.0}


class Picking(unittest.TestCase):
    def test_picks_a_loop_that_fits_and_never_the_songs_own(self):
        with tempfile.TemporaryDirectory() as tmp, Crate(tmp, [entry("guitar", camelot="2B"), entry("guitar", digest="own" * 10),
                                                                entry("guitar", bpm=100), entry("guitar", title="fits")]):
            choice = loops.pick(("guitar",), 124.0, "8A", exclude={"own" * 10})
            self.assertEqual(choice["loop"]["title"], "fits")
            self.assertEqual((choice["shift"], choice["factor"]), (0, 1.0))

    def test_both_keys_have_to_take_the_same_shift(self):
        with tempfile.TemporaryDirectory() as tmp, Crate(tmp, [entry("piano", camelot="8B")]):
            self.assertIsNotNone(loops.pick(("piano",), 124.0, "8A", keys_also="9A"))
            self.assertIsNone(loops.pick(("piano",), 124.0, "8A", keys_also="3A"))

    def test_off_switch(self):
        with tempfile.TemporaryDirectory() as tmp, Crate(tmp, [entry("drums")]):
            with mock.patch.dict(os.environ, {"DJ_LOOPS": "0"}):
                self.assertIsNone(loops.pick(("drums",), 124.0, None))


class Ratings(unittest.TestCase):
    def test_a_thumbs_down_is_never_played_and_a_thumbs_up_is_preferred(self):
        with tempfile.TemporaryDirectory() as tmp, Crate(tmp, [entry("guitar", title="bad", digest="b" * 32), entry("guitar", title="good", digest="g" * 32)]):
            bad, good = (l["id"] for l in loops.crate())
            loops.vote(bad, "down")
            for seed in range(8):
                self.assertEqual(loops.pick(("guitar",), 124.0, "8A", rng=random.Random(seed))["loop"]["title"], "good")
            loops.vote(bad, None)
            loops.vote(good, "up")
            picks = {loops.pick(("guitar",), 124.0, "8A", rng=random.Random(seed))["loop"]["title"] for seed in range(8)}
            self.assertEqual(picks, {"good"})
            with self.assertRaises(LookupError):
                loops.vote("nope", "up")
            with self.assertRaises(ValueError):
                loops.vote(good, "meh")

    def test_approved_only(self):
        with tempfile.TemporaryDirectory() as tmp, Crate(tmp, [entry("drums")]):
            loops.set_approved_only(True)
            self.assertIsNone(loops.pick(("drums",), 124.0, None))
            loops.vote(loops.crate()[0]["id"], "up")
            self.assertIsNotNone(loops.pick(("drums",), 124.0, None))
            self.assertTrue(loops.ratings()["approved_only"])

    def test_preview_is_a_wav_twice_round(self):
        with tempfile.TemporaryDirectory() as tmp, Crate(tmp, [entry("drums")]):
            data = loops.preview(loops.crate()[0]["id"])
            self.assertEqual(data[:4], b"RIFF")
            self.assertEqual((len(data) - 44) // 4, 2 * len(clicks(16, 124.0)))


def profile(notes):
    """A pitch-class profile with these notes sounding."""
    c = np.zeros(12, np.float32)
    c[list(notes)] = 1.0
    return c


class ByEar(unittest.TestCase):
    C_MAJOR = (0, 2, 4, 5, 7, 9, 11)

    def test_moves_a_riff_to_where_its_notes_agree(self):
        d_major = [(n + 2) % 12 for n in self.C_MAJOR]
        shift, agree = loops.best_shift(profile(self.C_MAJOR), [profile(d_major)])
        self.assertEqual(shift, 2)
        self.assertGreater(agree, 0.95)
        self.assertEqual(loops.best_shift(profile(self.C_MAJOR), [profile(self.C_MAJOR)])[0], 0)

    def test_both_songs_have_to_agree(self):
        f_sharp = [(n + 6) % 12 for n in self.C_MAJOR]
        self.assertIsNone(loops.best_shift(profile(self.C_MAJOR), [profile(self.C_MAJOR), profile(f_sharp)]))

    def test_pick_listens_to_the_music_rather_than_the_labels(self):
        class Deck:
            def __init__(self, notes):
                t = np.arange(SR * 6) / SR
                x = sum(np.sin(2 * np.pi * 261.63 * 2 ** (((n - 0) % 12) / 12) * t) for n in notes) * 3000
                self.audio = np.stack([x, x], axis=1).astype(np.int16)
                self.stems = None
        e_major = [(n + 4) % 12 for n in (0, 4, 7)]
        riff = dict(entry("guitar", camelot="2B"), chroma=[float(v) for v in profile((0, 4, 7))])      # labelled F#, sounds C
        with tempfile.TemporaryDirectory() as tmp, Crate(tmp, [riff]):
            # Labelled a clash with 8A, but it sounds like C major and the song sounds like E major: up 4 is too far...
            self.assertIsNone(loops.pick(("guitar",), 124.0, "8A", against=[(Deck(e_major), 0, 6)]))
            # ...and a song that sounds like C: it fits as it is, whatever the labels say.
            choice = loops.pick(("guitar",), 124.0, "8A", against=[(Deck((0, 4, 7)), 0, 6)])
            self.assertEqual(choice["shift"], 0)
            self.assertGreater(choice["harmony"], 0.9)


class Voices(unittest.TestCase):
    """LoopVoice on a real engine: a deck of plain clicks to lock to, its fader down so only the loop is heard."""

    def setUp(self):
        self.engine = dj.DJEngine()
        deck = self.deck = self.engine.decks["A"]
        deck.audio = (clicks(400, 120) * 32767).astype(np.int16)
        deck.meta = {"bpm": 120.0, "bpm_confidence": 1.0, "first_beat": 0.0}
        deck.playing = True
        deck.fader = 0.0
        self.engine.master_name = "A"

    def play(self, seconds, change=None):
        out, beats = [], []
        for k in range(int(seconds * SR / dj.BLOCK)):
            if change and k == change[0]:
                change[1]()
            pcm = np.frombuffer(self.engine.render(), "<i2").reshape(-1, 2)
            out.append(pcm[:, 0].astype(np.float32) / 32767)
            beats.append(self.deck.beat_at(self.deck.seconds))
        return np.concatenate(out), beats

    def onsets(self, x):
        """Where the clicks start: 24 samples loud (a lone sample is the loop's seam blending into its next click)."""
        loud = np.abs(x) > 0.1
        runs = np.convolve(loud, np.ones(12), "valid") >= 12
        starts = np.flatnonzero(runs & ~np.concatenate([[False], runs[:-1]]))
        return starts

    def assert_on_the_beats(self, x, beats, start, end):
        # Where each beat starts in the output, from the deck's beat after every block.
        want = []
        prev = beats[0] - dj.BLOCK / SR * 2
        for k, b in enumerate(beats):
            a = prev
            for whole in range(math.ceil(a), math.floor(b) + 1):
                if start <= whole < end and whole > a:
                    want.append(int(k * dj.BLOCK + (whole - a) / (b - a) * dj.BLOCK))
            prev = b
        got = self.onsets(x)
        self.assertEqual(len(got), len(want), (got[:8], want[:8]))
        self.assertLess(np.abs(got - np.array(want)).max(), 0.002 * SR)

    def test_a_loop_plays_on_the_decks_beat_from_its_start_to_its_end(self):
        audio = clicks(16, 120)
        self.engine.add_loop(dj.LoopVoice(audio, 16, "A", 4, 36, [(4, 1.0), (36, 1.0)], "drums"))
        x, beats = self.play(20)
        self.assert_on_the_beats(x, beats, 4, 36)
        self.assertFalse(self.engine.loops)                  # gone once it's over

    def test_it_stays_on_the_beat_when_the_tempo_moves(self):
        audio = clicks(16, 120)
        self.engine.add_loop(dj.LoopVoice(audio, 16, "A", 2, 30, [(2, 1.0)], "drums"))
        x, beats = self.play(15, change=(150, lambda: setattr(self.deck, "pitch", 4.0)))
        self.assert_on_the_beats(x, beats, 2, 30)

    def test_gains_shape_it(self):
        audio = np.full((dj.SR, 2), 0.3, np.float32)
        voice = dj.LoopVoice(audio, 2, "A", 0, 8, [(0, 0.0), (4, 1.0), (8, 0.0)], "guitar")
        self.assertAlmostEqual(voice.gain_at(2), 0.5)
        self.assertAlmostEqual(voice.gain_at(6), 0.5)
        self.assertEqual(voice.gain_at(-1), 0.0)

    def test_a_called_off_mix_takes_its_loop_with_it(self):
        tr = dj.Transition(self.deck, self.engine.decks["B"], "drum_bridge", 32, 0)
        self.engine.transition = tr
        self.engine.add_loop(dj.LoopVoice(np.full((SR, 2), 0.3, np.float32), 2, "A", 0, 1000, [(0, 1.0)], "guitar", transition=tr))
        self.play(0.5)
        self.assertTrue(self.engine.loops)
        self.engine.transition = None                         # a hand on the crossfader
        x, _ = self.play(0.6)
        self.assertFalse(self.engine.loops)
        self.assertLess(np.abs(x[-dj.BLOCK:]).max(), 1e-3)

    def test_its_light(self):
        self.engine.light = club.Feed()
        voice = dj.LoopVoice(clicks(16, 120), 16, "A", 0, 64, [(0, 1.0)], "drums")
        voice.light = np.full((2000, 9), 200, np.uint8)
        self.engine.add_loop(voice)
        got = club.loop_frames(self.engine, 0.1)
        self.assertEqual(len(got), 1)
        self.assertGreater(got[0][0], 0.5)                    # the kick
        self.assertEqual(got[0][2], 0.0)                      # not the bass


class BorrowedGroove(unittest.TestCase):
    setUpClass = classmethod(routines.RoutineTests.setUpClass.__func__)
    tearDownClass = classmethod(routines.RoutineTests.tearDownClass.__func__)
    two_decks = routines.RoutineTests.two_decks
    run_until = routines.RoutineTests.run_until

    def test_the_script_hands_over_part_by_part(self):
        script, beats, drums, riff = realdj.borrow_script(False, "guitar")
        at = lambda b: dj.script_at(script, b)
        self.assertEqual(beats, P + 2)
        self.assertEqual(at(4)["out.drums"], 0.0)              # the borrowed drums have the beat
        self.assertEqual(at(4)["out.vocals"], 1.0)
        self.assertEqual((at(12)["out.bass"], at(12)["out.other"]), (0.0, 0.0))
        self.assertEqual((at(20)["out.vocals"], at(20)["in.drums"]), (0.0, 1.0))
        self.assertEqual(at(33)["in.vocals"], 1.0)
        self.assertEqual(riff[0][0], 7.5)
        clash, *_rest, clash_riff = realdj.borrow_script(True, "bass")
        self.assertEqual(clash_riff[0][0], 15.75)              # a riff in the new key waits for the old vocal
        self.assertEqual(clash_riff[-1][0], 24.0)              # a borrowed bass line leaves as the new one comes

    def test_planned_with_parts_of_other_songs_and_played(self):
        session, a, b = self.two_decks()
        session.engine.light = club.Feed()
        with tempfile.TemporaryDirectory() as tmp, Crate(tmp, [entry("drums", title="Drums Song"), entry("guitar", digest="cafe" * 8, title="Riff Song")]):
            with mock.patch.dict(realdj.TASTE["club"], {"borrow": 20.0}):
                plan = realdj.plan_transition(a, b, 0, [], random.Random(3), "club")
            self.assertEqual((plan.style, plan.move, plan.label), (dj.ROUTINE, "borrow", "Borrowed groove"))
            self.assertEqual([x["part"] for x in plan.loops], ["drums", "guitar"])
            self.assertIn("Riff Song", plan.why)
            self.assertEqual(plan.json()["loops"][1]["title"], "Riff Song")
            engine = session.engine
            a.seek(a.time_of_beat(plan.start_beat - 4))
            a.playing, b.sync = True, True
            b.cue = b.snap(plan.in_cue, force=True)
            tr = engine.transition = dj.Transition(a, b, plan.style, plan.beats, plan.start_beat)
            tr.script = plan.script
            real = realdj.RealDJ(session)
            real._lay_loops(a, plan, tr)
            self.assertEqual(len(engine.loops), 2)
            self.run_until(engine, tr, 11)
            self.assertLess(a.stem_gain["drums"], 0.01)
            self.assertLess(a.stem_gain["other"], 0.01)
            heard = {v.part: v.level for v in engine.loops}
            self.assertGreater(heard["drums"], 0.9)
            self.assertGreater(heard["guitar"], 0.8)
            self.assertEqual(engine.state()["loops"][1]["title"], "Riff Song")
            events = [e for e in engine.light.events if e["k"] == "loop"]
            self.assertEqual(sorted(e["name"] for e in events), ["drums", "guitar"])
            self.run_until(engine, tr, 20)
            self.assertEqual([v.part for v in engine.loops], ["guitar"])     # the new drums took over
            self.assertGreater(b.stem_gain["drums"], 0.99)

    def test_without_a_crate_nothing_changes(self):
        _session, a, b = self.two_decks()
        with tempfile.TemporaryDirectory() as tmp, Crate(tmp, []):
            plan = realdj.plan_transition(a, b, 0, [], random.Random(2), "club")
        self.assertEqual(plan.move, "trade")
        self.assertEqual(plan.loops, [])


class BorrowPad(unittest.TestCase):
    setUpClass = classmethod(routines.RoutineTests.setUpClass.__func__)
    tearDownClass = classmethod(routines.RoutineTests.tearDownClass.__func__)
    two_decks = routines.RoutineTests.two_decks

    def test_the_pad_swaps_the_songs_drums_for_eight_bars(self):
        session, a, _b = self.two_decks()
        engine = session.engine
        engine.light = club.Feed()
        a.playing = True
        engine.master_name = "A"
        a.seek(a.time_of_beat(16.3))
        with tempfile.TemporaryDirectory() as tmp, Crate(tmp, [entry("drums", title="Groove")]):
            asyncio.run(session.handle({"op": "club_loop", "part": "drums"}))
            self.assertIn("Groove", session.notice)
            voice = engine.loops[0]
            self.assertEqual((voice.start, voice.end), (20.0, 52.0))
            for _ in range(400):
                engine.render()
                if a.beat_at(a.seconds) > 24:
                    break
            self.assertEqual(a.stem_gain["drums"], 0.0)
            self.assertEqual(a.stem_gain["bass"], 1.0)
            with self.assertRaises(ValueError):
                asyncio.run(session.handle({"op": "club_loop", "part": "drums"}))     # one at a time
            self.assertEqual(loops.played[-1]["over"], "a")
            asyncio.run(session.handle({"op": "loop_vote", "id": voice.loop_id, "vote": "down"}))
            self.assertEqual(loops.ratings()["votes"][voice.loop_id], "down")
            for _ in range(2000):
                engine.render()
                if a.beat_at(a.seconds) > 53:
                    break
            self.assertEqual(a.stem_gain["drums"], 1.0)                          # its own drums back
            self.assertFalse(engine.loops)
        with self.assertRaises(ValueError):
            asyncio.run(club.borrow(session, "kazoo"))


class CratePage(unittest.TestCase):
    def test_list_vote_and_listen(self):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer
        import loops_ui

        async def run():
            app = web.Application()
            loops_ui.setup(app)
            async with TestClient(TestServer(app)) as client:
                page = await client.get("/dj/loops/")
                self.assertIn("Loop Crate", await page.text())
                data = await (await client.get("/api/loops")).json()
                self.assertEqual(len(data["loops"]), 2)
                loop_id = data["loops"][0]["id"]
                voted = await (await client.post(f"/api/loops/{loop_id}/vote", json={"vote": "down"})).json()
                self.assertEqual(voted["loops"][0]["vote"], "down")
                self.assertEqual((await client.post("/api/loops/nope/vote", json={"vote": "up"})).status, 404)
                settings = await (await client.post("/api/loops/settings", json={"approved_only": True})).json()
                self.assertTrue(settings["approved_only"])
                audio = await client.get(f"/api/loops/{loop_id}/audio")
                self.assertEqual(audio.content_type, "audio/wav")
                self.assertEqual((await audio.read())[:4], b"RIFF")
                loops.remember(loop_id, "Some Song")
                played = (await (await client.get("/api/loops")).json())["played"]
                self.assertEqual((played[0]["id"], played[0]["over"]), (loop_id, "Some Song"))
        with tempfile.TemporaryDirectory() as tmp, Crate(tmp, [entry("drums"), entry("guitar", digest="c" * 32)]):
            asyncio.run(run())


class SongMap(unittest.TestCase):
    def env(self, shape, bpm=120.0):
        """A light envelope bar by bar: (level of the bar, sings?)."""
        bar_frames = int(round(4 * 60 / bpm * 50))
        rows = []
        for level, sings in shape:
            row = [level * 200, level * 120, level * 180, 200 if sings else 0, level * 150, level * 230, 0, 0, 0]
            rows += [row] * bar_frames
        return np.array(rows, np.uint8)

    def test_verses_and_choruses(self):
        shape = [(0.2, False)] * 8 + [(0.45, True)] * 8 + [(1.0, True)] * 8 + [(0.45, True)] * 8 + [(1.0, True)] * 8 + [(0.15, False)] * 4
        made = club.song_map(self.env(shape), 120.0, 0.0, len(shape) * 2.0)
        tiers = [(start, tier) for start, _length, tier, _flags, _name in made["sec"]]
        self.assertFalse(made["named"])
        self.assertEqual([s for s, t in tiers if t == 3], [16, 32])
        self.assertTrue(all(t < 3 for s, t in tiers if s not in (16, 32)))
        self.assertLess(tiers[0][1], tiers[1][1])            # the intro is quieter than the verse
        bars = np.frombuffer(__import__("base64").b64decode(made["bars"]), np.uint8)
        self.assertEqual(len(bars), len(shape))
        self.assertEqual(bars[20] >> 4, 15)
        self.assertTrue(bars[10] & 1)                          # someone sings
        self.assertGreater(made["dyn"], 0.4)

    def test_named_sections_from_someone_who_listened(self):
        # Verse, pre-chorus, chorus, a quiet bridge, chorus, outro: named at their seconds (a bar is 2 s at 120 bpm).
        shape = [(0.45, True)] * 8 + [(0.6, True)] * 4 + [(1.0, True)] * 8 + [(0.3, False)] * 4 + [(1.0, True)] * 8 + [(0.4, False)] * 4
        named = [{"label": "verse", "at": 0.0, "energy": 5}, {"label": "prechorus", "at": 16.1, "energy": 6},
                 {"label": "chorus", "at": 23.9, "energy": 9}, {"label": "bridge", "at": 40.0, "energy": 3},
                 {"label": "chorus", "at": 48.0, "energy": 9}, {"label": "outro", "at": 64.0, "energy": 4}]
        made = club.song_map(self.env(shape), 120.0, 0.0, len(shape) * 2.0, named)
        self.assertTrue(made["named"])
        got = [(bar, tier, club.SECTION_NAMES[name]) for bar, _n, tier, _f, name in made["sec"]]
        self.assertEqual(got, [(0, 1, "verse"), (8, 2, "prechorus"), (12, 3, "chorus"), (20, 0, "bridge"), (24, 3, "chorus"), (32, 0, "outro")])
        # Names that don't fit the music at all are not used.
        wrong = [{"label": "chorus", "at": 0.0}, {"label": "verse", "at": 24.0}, {"label": "chorus", "at": 40.0}, {"label": "verse", "at": 48.0}]
        self.assertFalse(club.song_map(self.env(shape), 120.0, 0.0, len(shape) * 2.0, wrong)["named"])

    def test_mood_words_pick_a_palette(self):
        import djbrain
        self.assertEqual(djbrain.mood_name(["Aggressive", "rebellious"]), "fierce")
        self.assertEqual(djbrain.mood_name(["energetic", "melancholic"]), "fierce")       # hard and sad: angst
        self.assertEqual(djbrain.mood_name(["melancholic", "reflective"]), "blue")
        self.assertEqual(djbrain.mood_name(["Upbeat", "playful"]), "bright")
        self.assertEqual(djbrain.mood_name(["dreamy", "atmospheric"]), "dreamy")
        self.assertEqual(djbrain.mood_name(["romantic"]), "warm")
        self.assertIsNone(djbrain.mood_name(["purple"]))
        self.assertEqual(club.song_mood({"energy": 0.7, "camelot": "8A"}, {"dyn": 0.5}, {"words": ["dreamy"], "name": "dreamy"}),
                         {"energy": 0.7, "minor": True, "dyn": 0.5, "words": ["dreamy"], "name": "dreamy"})

    def test_bars_start_where_the_song_says(self):
        # 120 bpm, grid from 0.0 s, but the bars really start on the grid's second beat (0.5 s).
        downbeats = [0.5 + 2.0 * k for k in range(40)]
        self.assertEqual(club.bar_phase(downbeats, 120.0, 0.0), 1)
        self.assertEqual(club.bar_phase(downbeats[:2], 120.0, 0.0), 0)                  # too few to tell
        self.assertEqual(club.bar_phase([0.25 + 2.0 * k for k in range(40)], 120.0, 0.0), 0)   # off the grid
        shape = [(0.3, False)] * 8 + [(1.0, True)] * 8
        made = club.song_map(self.env(shape), 120.0, -0.5, 32.0, None, phase=1)        # the grid one beat early
        self.assertEqual(made["phase"], 1)
        self.assertEqual([(bar, tier) for bar, _n, tier, _f, _name in made["sec"]][-1], (8, 3))

    def test_no_grid_no_map(self):
        self.assertIsNone(club.song_map(self.env([(1, True)] * 8), None, 0.0, 16.0))

    def test_the_feed_sends_it_with_the_song_and_again_later(self):
        class Deck:
            name, wave_id, loaded, bpm, first_beat, duration = "A", 3, True, 120.0, 0.0, 64.0
            meta = {"energy": 0.8, "camelot": "8A"}
            light = self.env([(0.3, False)] * 8 + [(1.0, True)] * 8 + [(0.3, False)] * 16)
        feed = club.Feed()
        feed.lead(Deck)
        feed.add(1000.0, [0.5] * 6, beat=10.0, bpm=120.0)
        first = feed.take(now=1.0)
        self.assertEqual(first[-1]["w"], "A3")
        self.assertEqual(first[-1]["map"]["id"], "A3")
        self.assertEqual(first[-1]["map"]["mood"], {"energy": 0.8, "minor": True, "dyn": first[-1]["map"]["dyn"]})
        feed.lead(Deck)
        feed.add(1020.0, [0.5] * 6, beat=10.04, bpm=120.0)
        self.assertNotIn("map", feed.take(now=1.1)[-1])
        with mock.patch.object(club.time, "time", return_value=time.time() + club.MAP_RESEND_S + 1):
            feed.lead(Deck)
        self.assertIn("map", feed.take(now=2.0)[-1])


if __name__ == "__main__":
    unittest.main()


class Autoplay(unittest.TestCase):
    """A Hoffle room's DJ set carries on from the library when its queue runs out."""

    def setUp(self):
        self.added = []

        async def refill(session):
            self.added.append(len(session.crate.files))
            session.crate.files += ["/lib/one.wav", "/lib/two.wav"]
            return 2
        self.patch = mock.patch.object(dj, "refill", refill)
        self.patch.start()
        self.session = dj.DJSession("huddle:room", "huddle", routines.Crate([]))
        self.session.crate.used = set()          # a Hoffle room's crate remembers what it handed out

    def tearDown(self):
        self.patch.stop()

    def test_an_empty_queue_is_topped_up_once_in_a_while(self):
        s = self.session
        self.assertIsNone(asyncio.run(s._next_item()))
        self.assertEqual(self.added, [0])
        self.assertIn("2 songs from the library", s.notice)
        self.assertEqual(len(s.crate.items()), 2)               # there by the next tick
        s.crate.files.clear()
        asyncio.run(s._next_item())
        self.assertEqual(self.added, [0])                       # not again so soon
        s.refilled_at -= dj.REFILL_EVERY_S + 1
        asyncio.run(s._next_item())
        self.assertEqual(self.added, [0, 0])

    def test_real_dj_tops_up_before_it_runs_out_and_starts_cold_from_the_library(self):
        s = self.session
        real = realdj.RealDJ(s)
        asyncio.run(real._start_cold())                         # nothing on, nothing queued: songs come
        self.assertEqual(self.added, [0])
        s.refilled_at = -math.inf
        s.crate.files = ["/q/only.wav"]
        with mock.patch.object(s, "load_in_background"):
            asyncio.run(real._load_next(s.engine.decks["B"], None))
        self.assertEqual(self.added, [0, 1])                    # one left: more to choose from

    def test_off_and_not_for_discord(self):
        s = self.session
        asyncio.run(s.handle({"op": "auto", "autoplay": False}))
        self.assertFalse(s.state()["auto"]["autoplay"])
        asyncio.run(s._next_item())
        self.assertEqual(self.added, [])
        discord = dj.DJSession("t", "test", routines.Crate([]))
        self.assertEqual(asyncio.run(discord.refill_crate()), 0)

    def test_the_switch_and_what_was_played_survive_a_restart(self):
        s = self.session
        s.autoplay = False
        s.played.update({"song a", "song b"})
        snap = s.snapshot()
        again = dj.DJSession("huddle:room", "huddle", routines.Crate([]))
        again.crate.used = set()
        asyncio.run(again.restore(snap))
        self.assertFalse(again.autoplay)
        self.assertEqual(again.played, {"song a", "song b"})


class RealBars(unittest.TestCase):
    """Real DJ's beat grid on the song's real bars (the structure run's downbeats and sections)."""

    def song(self, bpm=120.3, first=1.0, bars=96):
        bar = 240.0 / bpm
        downbeats = [first + k * bar for k in range(bars)]
        downbeats[10] += 0.3                        # a stray one
        del downbeats[20]                           # and a missed one
        # intro 6 bars, verse 8, chorus 8, verse 8, chorus 8, ...: the sections start 6 bars past the 8-bar lines
        segments, at = [[0.0, first, "start"]], first
        for label, n in [("intro", 6), ("verse", 8), ("chorus", 8), ("verse", 8), ("chorus", 8), ("bridge", 8), ("chorus", 16), ("outro", 8)]:
            segments.append([round(at, 2), round(at + n * bar, 2), label])
            at += n * bar
        return {"downbeats": downbeats, "segments": segments}, bar

    def test_the_fit_shrugs_off_strays(self):
        song, bar = self.song()
        fitted_bar, first = realdj.fit_bars(song["downbeats"])
        self.assertAlmostEqual(fitted_bar, bar, places=4)
        self.assertAlmostEqual(first, 1.0, places=2)
        rubato = [k * 2.0 + (0.4 if k % 3 else 0) for k in range(40)]
        self.assertIsNone(realdj.fit_bars(rubato))

    def test_grid_moves_onto_the_bars_and_the_phrases(self):
        song, bar = self.song()
        meta = {"bpm": 120.0, "bpm_confidence": 0.9, "first_beat": 1.75}      # 0.25% slow, and a beat late
        with mock.patch.object(realdj.djbrain, "downbeats", return_value=song["downbeats"]), \
                mock.patch.object(realdj.djbrain, "structure", return_value=song):
            fixed = realdj.aligned_grid(meta, ["yt:x"])
        self.assertAlmostEqual(fixed["bpm"], 120.3, places=2)
        beat = 60 / fixed["bpm"]
        chorus = [s for s, _e, label in song["segments"] if label == "chorus"]
        for s in chorus:
            b = (s - fixed["first_beat"]) / beat
            self.assertAlmostEqual(b / 32, round(b / 32), delta=0.01)          # every chorus on a phrase line
        # Beat 0 on the verse after the six-bar intro: the earliest phrase line that lies in the song.
        self.assertAlmostEqual(fixed["first_beat"], 1.0 + 6 * 240 / 120.3, places=2)
        self.assertEqual(fixed["grid_fix"]["bars"], 6)

    def test_a_song_in_half_time_stays_in_half_time_and_strangers_are_left_alone(self):
        song, _bar = self.song()
        with mock.patch.object(realdj.djbrain, "downbeats", return_value=song["downbeats"]), \
                mock.patch.object(realdj.djbrain, "structure", return_value=song):
            half = realdj.aligned_grid({"bpm": 60.0, "bpm_confidence": 0.9, "first_beat": 1.0}, ["yt:x"])
            other = realdj.aligned_grid({"bpm": 100.0, "bpm_confidence": 0.9, "first_beat": 1.0}, ["yt:x"])
        self.assertAlmostEqual(half["bpm"], 60.15, places=2)
        self.assertNotIn("tempo", other.get("grid_fix", {}))
        with mock.patch.object(realdj.djbrain, "downbeats", return_value=[]):
            meta = {"bpm": 120.0, "bpm_confidence": 0.9, "first_beat": 1.0}
            self.assertIs(realdj.aligned_grid(meta, ["yt:x"]), meta)

    def test_real_dj_takes_its_big_phrases_from_the_choruses(self):
        class Deck:
            duration = 200.0
            info = {"keys": ["yt:x"]}
            ai = False
        phrases = [{"start": k * 16.0} for k in range(12)]
        segments = [[0, 32, "intro"], [32, 64, "verse"], [64, 96, "chorus"], [96, 128, "solo"], [128, 160, "chorus"], [160, 200, "outro"]]
        with mock.patch.object(realdj.djbrain, "structure", return_value={"segments": segments}):
            labels = realdj._phrase_labels(Deck(), phrases, 16.0)
        self.assertEqual(labels[4:6], ["chorus", "chorus"])
        self.assertEqual(labels[6], "solo")
        with mock.patch.object(realdj.djbrain, "structure", return_value=None):
            self.assertIsNone(realdj._phrase_labels(Deck(), phrases, 16.0))


class AutoplayPicks(unittest.TestCase):
    def test_known_songs_first_and_no_near_silence_on_a_club_night(self):
        import webui
        analyses = {"yt:loud" + str(i): {"bpm": 120, "bpm_confidence": 0.8, "energy": 0.6} for i in range(16)}
        analyses["yt:speech"] = {"bpm": 171, "bpm_confidence": 0.5, "energy": 0.04}
        cache = mock.Mock()
        cache.get = lambda *keys: next((analyses[k] for k in keys if k in analyses), None)
        entries = [{"url": f"https://www.youtube.com/watch?v={vid[3:]}", "title": vid} for vid in analyses] + \
                  [{"url": "https://www.youtube.com/watch?v=unknown", "title": "new"}]
        with mock.patch.object(webui.mixer, "cache", cache), \
                mock.patch.object(webui.mixer, "track_key", lambda url: "yt:" + url.rsplit("=", 1)[1] if url else None):
            club_night = [e["title"] for e in webui.autoplay_entries(entries, "club")]
            chill = [e["title"] for e in webui.autoplay_entries(entries, "chill")]
        self.assertNotIn("yt:speech", club_night)
        self.assertNotIn("new", club_night)                       # sixteen known songs: no need for unknown ones
        self.assertIn("yt:speech", chill)
