"""Key lock, the beat grid on the kick, playing a song from the top, and mixes drawn in the mix editor."""
import os
import random
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import beatgrid  # noqa: E402
import dj  # noqa: E402
import keylock  # noqa: E402
import mixes  # noqa: E402
import realdj  # noqa: E402

SR = dj.SR


def kick(n=int(0.12 * SR)):
    t = np.arange(n) / SR
    return np.sin(2 * np.pi * (50 + 80 * np.exp(-t / 0.02)) * t) * np.exp(-t / 0.05)


def song(bpm=120.0, beats=200, first=0.5, drums_from=0, tag=False, jitter=0.0, seed=1):
    """A deck's worth of song: (audio, stems) as int16, a kick on every beat from `drums_from`, a bass
    note, and with `tag` a sung line over the beats before the drums."""
    rng = np.random.default_rng(seed)
    beat = 60.0 / bpm
    n = int((first + beats * beat + 1.0) * SR)
    t = np.arange(n) / SR
    drums, k = np.zeros(n), kick()
    for b in range(drums_from, beats):
        i = int((first + b * beat + (rng.normal(0, jitter) if jitter else 0.0)) * SR)
        if 0 <= i < n - len(k):
            drums[i:i + len(k)] += k
    a = int((first + drums_from * beat) * SR)
    bass = np.zeros(n)
    bass[a:] = 0.25 * np.sin(2 * np.pi * 110 * t[a:])
    vocals = np.zeros(n)
    if tag:
        v0, v1 = int(first * SR), int((first + (drums_from - 0.5) * beat) * SR)
        vocals[v0:v1] = 0.4 * np.sin(2 * np.pi * 440 * t[v0:v1])
    mix = drums + bass + vocals
    scale = 0.7 * 32767 / np.abs(mix).max()
    stereo = lambda x: np.stack([x, x], axis=1)
    return (stereo(mix) * scale).astype(np.int16), (np.stack([stereo(x) for x in (drums, bass, vocals)]) * scale).astype(np.int16)


def load(deck, audio, stems, bpm=120.0, first=0.5, title="song"):
    deck.audio, deck.stems, deck.stems_status = audio, stems, "ready"
    deck.meta = {"bpm": bpm, "bpm_confidence": 1.0, "first_beat": first, "music_start": first,
                 "music_end": len(audio) / SR - 0.5, "camelot": "8A", "energy": 0.6}
    deck.info = {"title": title, "keys": ["test:" + title]}
    deck.wave = dj.analyze_buffer(audio, [])[1]
    deck.wave_id += 1
    deck.reset_track_state()
    deck.cue = first
    deck.seek(first)
    return deck


def tone_of(block_list):
    x = np.concatenate(block_list)[:, 0]
    x = x[len(x) // 4:]
    spectrum = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    return float(np.fft.rfftfreq(len(x), 1 / SR)[int(np.argmax(spectrum))])


@unittest.skipUnless(keylock.available(), "Rubber Band is not installed")
class KeyLockTests(unittest.TestCase):
    def deck(self):
        t = np.arange(SR * 12) / SR
        x = (0.3 * 32767 * np.sin(2 * np.pi * 440 * t)).astype(np.int16)
        deck = dj.Deck("A")
        load(deck, np.stack([x, x], axis=1), None)
        deck.stems = None
        deck.playing = True
        return deck

    def play(self, deck, blocks=300):
        return [deck.read(dj.BLOCK, 0.0) for _ in range(blocks)]

    def test_tempo_moves_and_the_key_stays(self):
        deck = self.deck()
        deck.pitch = 4.0
        start = deck.pos
        out = self.play(deck)
        self.assertAlmostEqual(tone_of(out), 440.0, delta=1.5)
        # The playhead still moves 4% faster (after the first block's run-up): the song is ahead, not higher.
        self.assertAlmostEqual((deck.pos - start) / (300 * dj.BLOCK), 1.04, delta=0.003)

    def test_without_key_lock_the_pitch_follows_the_tempo(self):
        deck = self.deck()
        deck.keylock = False
        deck.pitch = 4.0
        self.assertAlmostEqual(tone_of(self.play(deck)), 440.0 * 1.04, delta=1.5)

    def test_at_its_own_tempo_the_deck_plays_the_song_untouched(self):
        deck, plain = self.deck(), self.deck()
        plain.keylock = False
        self.assertTrue(np.array_equal(np.concatenate(self.play(deck, 20)), np.concatenate(self.play(plain, 20))))
        self.assertFalse(deck.stretch.active)

    def test_a_scratch_is_the_record_itself(self):
        deck = self.deck()
        deck.pitch = 4.0
        self.play(deck, 20)
        self.assertTrue(deck.stretch.active)
        deck.scratch, deck.scratch_at = 1.5, 0.0
        deck.read(dj.BLOCK, 0.0)
        self.assertFalse(deck.stretch.active)

    def test_the_room_is_told_the_key_did_not_move(self):
        deck = self.deck()
        deck.pitch = 6.0
        session = dj.DJSession.__new__(dj.DJSession)
        self.assertEqual(session._deck_on_air(deck)["keyShift"], 0.0)
        deck.keylock = False
        self.assertEqual(session._deck_on_air(deck)["keyShift"], 1.0)


class BeatGridTests(unittest.TestCase):
    def test_a_song_that_keeps_time_gets_its_grid_on_the_kick(self):
        audio, stems = song(bpm=124.37, first=0.37, beats=400)
        # What the analysis says: a tenth of a bpm off, and a quarter beat late.
        meta = {"bpm": 124.5, "bpm_confidence": 0.9, "first_beat": 0.49}
        fixed = beatgrid.refine(meta, stems[0])
        self.assertGreater(fixed["grid_steady"], beatgrid.STEADY)
        self.assertAlmostEqual(fixed["bpm"], 124.37, delta=0.02)
        beat = 60.0 / fixed["bpm"]
        off = ((fixed["first_beat"] - 0.37) / beat + 0.5) % 1.0 - 0.5
        self.assertLess(abs(off * beat), 0.012)
        # Beat 0 is still the beat it was: the grid moved by less than half a beat.
        self.assertLess(abs(fixed["first_beat"] - 0.49), 0.5 * beat)

    def test_a_band_that_breathes_keeps_the_grid_it_had(self):
        audio, stems = song(bpm=118.0, beats=400, jitter=0.06)
        meta = {"bpm": 118.0, "bpm_confidence": 0.9, "first_beat": 0.5}
        fixed = beatgrid.refine(meta, stems[0])
        self.assertLess(fixed["grid_steady"], beatgrid.STEADY)
        self.assertEqual((fixed["bpm"], fixed["first_beat"]), (118.0, 0.5))

    def test_no_drums_no_change(self):
        meta = {"bpm": 120.0, "bpm_confidence": 0.9, "first_beat": 0.5}
        self.assertEqual(beatgrid.refine(dict(meta), None), meta)
        self.assertEqual(beatgrid.refine(dict(meta), np.zeros((SR * 30, 2), np.int16)), meta)


class FromTheTopTests(unittest.TestCase):
    def decks(self):
        engine = dj.DJEngine()
        out = load(engine.decks["A"], *song(bpm=120.0, seed=1), title="old")
        inn = load(engine.decks["B"], *song(bpm=120.0, drums_from=4, tag=True, seed=2), title="new")
        return engine, out, inn

    def test_an_opening_line_before_the_drums_is_found(self):
        _engine, out, inn = self.decks()
        self.assertIsNone(realdj.cold_open(out))            # drums from the first beat
        opening = realdj.cold_open(inn)
        self.assertEqual((opening["drum_beat"], opening["beats"], opening["sung"]), (4, 4, True))
        cue, gap, delay = realdj.top_entry(inn)
        self.assertEqual((gap, delay), (4, 0))
        self.assertAlmostEqual(cue, inn.time_of_beat(0), places=3)

    def test_a_long_intro_is_not_a_tag(self):
        engine = dj.DJEngine()
        inn = load(engine.decks["B"], *song(bpm=120.0, drums_from=16, tag=True), title="intro")
        self.assertIsNone(realdj.cold_open(inn))

    def test_auto_dj_plays_such_a_song_from_the_top(self):
        _engine, out, inn = self.decks()
        self.assertEqual(dj.choose_style(out, inn), "top")
        self.assertNotEqual(dj.choose_style(inn, out), "top")

    def test_the_old_song_cuts_and_the_new_beat_lands_on_the_bar(self):
        engine, out, inn = self.decks()
        out.seek(out.time_of_beat(62))
        out.playing, inn.sync = True, True
        engine.master_name, engine.xfader = "A", -1.0
        cue, gap, delay = realdj.top_entry(inn)
        inn.cue = cue
        inn.seek(cue)
        tr = engine.transition = dj.Transition(out, inn, "top", gap + 4, 64)
        tr.gap, tr.in_delay = gap, delay
        landed = None
        for _ in range(int(12 * SR / dj.BLOCK)):
            engine.render()
            if tr.started and not tr.done and 1.0 <= tr.elapsed < gap:
                self.assertEqual(out.fader, 0.0)              # the old song is cut; only its echo rings
                self.assertTrue(inn.playing)                   # the opening line plays in the gap
            if landed is None and tr.elapsed >= gap:
                landed = inn.beat_at(inn.seconds)
            if tr.done:
                break
        self.assertTrue(tr.done)
        self.assertAlmostEqual(landed, 4.0, delta=0.15)       # its drums land as the gap is up
        self.assertFalse(out.playing)
        self.assertEqual(engine.master_name, "B")
        self.assertFalse(engine.fx.on)

    def test_real_dj_plans_it_for_a_sung_opening(self):
        _engine, out, inn = self.decks()
        out.playing = True
        plan = realdj.plan_transition(out, inn, 8, [], random.Random(3), "club")
        self.assertEqual((plan.style, plan.gap), ("top", 4))
        self.assertEqual(plan.start_beat % 32, 28)             # the gap is the last bar of a phrase
        self.assertEqual(plan.beats, 8)


class MixEditorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._file, mixes.FILE, mixes._data = mixes.FILE, os.path.join(self.tmp.name, "dj_mixes.json"), None
        self.engine = dj.DJEngine()
        self.out = load(self.engine.decks["A"], *song(bpm=120.0, seed=1), title="old")
        self.inn = load(self.engine.decks["B"], *song(bpm=124.0, seed=2), bpm=124.0, title="new")
        self.mix = {"beats": 16, "out_at": self.out.time_of_beat(64), "in_at": self.inn.time_of_beat(8),
                    "lanes": {"out.vol": [[0, 1], [16, 0]], "in.vol": [[0, 0], [8, 1]],
                              "out.low": [[0, 1], [7, 1], [8, 0]], "in.bass": [[0, 0], [8, 1]]}}

    def tearDown(self):
        mixes.FILE, mixes._data = self._file, None
        self.tmp.cleanup()

    def test_a_mix_is_checked_and_tidied(self):
        for beats in (0, 3, 6, 132, "x"):
            with self.assertRaises(ValueError):
                mixes.clean({"beats": beats})
        mix = mixes.clean({"beats": 16, "out_at": 12.5, "in_start": 99,
                           "lanes": {"out.vol": [[20, 3], [4, -1], ["x", 1]], "out.filter": [[0, -2]],
                                     "in.nonsense": [[0, 1]], "in.vol": "no"}})
        self.assertEqual(mix["lanes"], {"out.vol": [[4, 0], [16, 1]], "out.filter": [[0, -1]]})
        self.assertEqual((mix["in_start"], mix["in_at"]), (15, None))

    def test_a_lane_is_straight_lines_between_its_points(self):
        points = [[4, 1], [8, 0], [8, 0.5], [12, 0.5]]
        self.assertEqual([mixes.value_at(points, b) for b in (0, 4, 6, 8, 10, 20)], [1, 1, 0.5, 0, 0.5, 0.5])

    def test_saved_for_the_pair_in_that_order_only(self):
        saved = mixes.put(self.out.info, self.inn.info, self.mix, by="someone")
        self.assertEqual(mixes.get(self.out.info, self.inn.info), saved)
        self.assertIsNone(mixes.get(self.inn.info, self.out.info))
        mixes._data = None                                    # read back from disk
        self.assertEqual(mixes.get(self.out.info, self.inn.info), saved)
        self.assertEqual(mixes.followers(self.out.info), {"test:new"})
        self.assertTrue(mixes.delete(self.out.info, self.inn.info))
        self.assertIsNone(mixes.get(self.out.info, self.inn.info))

    def test_the_curves_move_the_real_controls(self):
        engine, out, inn = self.engine, self.out, self.inn
        mix = mixes.clean(self.mix)
        out.seek(out.time_of_beat(62))
        out.playing = True
        engine.master_name, engine.xfader = "A", -1.0
        saved = dj.saved_mix(out, inn, mix=mix)
        self.assertEqual(saved["start_beat"], 64)
        inn.cue, inn.sync = saved["cue"], saved["sync"]
        inn.seek(inn.cue)
        tr = engine.transition = dj.custom_transition(out, inn, saved)
        seen = {}
        for _ in range(int(14 * SR / dj.BLOCK)):
            engine.render()
            if tr.started and not tr.done:
                for at in (4, 8, 12):
                    if at not in seen and tr.elapsed >= at:
                        seen[at] = (round(out.fader, 2), round(inn.fader, 2), round(out.eq["low"]),
                                    round(inn.stem_gain["bass"], 2), engine._xf_gain(out), engine._xf_gain(inn))
            if tr.done:
                break
        self.assertEqual(seen[4][:2], (0.75, 0.5))
        self.assertEqual(seen[8][:4], (0.5, 1.0, round(dj.KILL_DB), 1.0))
        self.assertEqual(seen[12][4:], (1.0, 1.0))             # both decks at the level the curves say
        self.assertTrue(tr.done)
        self.assertFalse(out.playing)
        self.assertEqual((inn.fader, inn.eq["low"], inn.stem_gain["bass"], engine.master_name), (1.0, 0.0, 1.0, "B"))
        # The new song came in tempo-matched, where the mix said.
        self.assertTrue(inn.sync)

    def test_a_saved_mix_is_what_the_djs_play(self):
        mixes.put(self.out.info, self.inn.info, self.mix)
        self.out.playing = True
        plan = realdj.plan_transition(self.out, self.inn, 8, [], random.Random(1), "club")
        self.assertEqual((plan.style, plan.beats, plan.start_beat), (dj.CUSTOM, 16, 64))
        self.assertAlmostEqual(plan.in_cue, self.inn.time_of_beat(8), places=2)
        # Past where it starts, the DJ plans its own; the editor's MIX NOW takes the next bar.
        self.out.seek(self.out.time_of_beat(70))
        self.assertIsNone(dj.saved_mix(self.out, self.inn))
        self.assertEqual(dj.saved_mix(self.out, self.inn, soon=True)["start_beat"], 72)
        self.assertNotEqual(realdj.plan_transition(self.out, self.inn, 70, [], random.Random(1), "club").style, dj.CUSTOM)

    def test_a_preview_plays_off_air(self):
        self.out.playing = True
        before = (self.out.pos, self.inn.pos, self.out.fader, self.inn.fader, self.inn.playing)
        pcm, lead = dj.preview_mix(self.out, self.inn, mixes.clean(self.mix), lead_beats=4, tail_beats=4)
        seconds = len(pcm) / 4 / SR
        self.assertAlmostEqual(lead, 2.0, places=2)
        self.assertAlmostEqual(seconds, (4 + 16 + 4) * 0.5, delta=1.0)
        self.assertGreater(np.abs(np.frombuffer(pcm, "<i2")).max(), 2000)
        self.assertEqual((self.out.pos, self.inn.pos, self.out.fader, self.inn.fader, self.inn.playing), before)
        self.assertIsNone(self.engine.transition)


if __name__ == "__main__":
    unittest.main()
