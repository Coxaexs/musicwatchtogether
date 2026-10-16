import asyncio
import os
import sys
import tempfile
import unittest
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dj  # noqa: E402


def click_track(path, bpm, seconds=60):
    """A kick on every beat with a hat in between, so tempo analysis locks on."""
    sr = dj.SR
    n = int(seconds * sr)
    t = np.arange(n) / sr
    audio = np.zeros(n)
    beat = 60.0 / bpm
    kick_len = int(0.12 * sr)
    kt = np.arange(kick_len) / sr
    kick = np.sin(2 * np.pi * (50 + 80 * np.exp(-kt / 0.02)) * kt) * np.exp(-kt / 0.05)
    for start in np.arange(0.5, seconds - 0.2, beat):
        i = int(start * sr)
        audio[i:i + kick_len] += kick[: n - i]
    audio += 0.05 * np.sin(2 * np.pi * 220 * t)
    pcm = (np.stack([audio, audio], axis=1) * 0.6 * 32767).astype("<i2")
    with wave.open(path, "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(sr)
        out.writeframes(pcm.tobytes())


class ListCrate:
    def __init__(self, files):
        self.files = list(files)

    def items(self):
        return [{"title": os.path.basename(f)} for f in self.files]

    async def take(self, index=0):
        if not 0 <= index < len(self.files):
            return None
        path = self.files.pop(index)
        return {"title": os.path.basename(path), "file": path, "keys": []}

    async def add(self, query, requester=None):
        return query


class DJEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.a = os.path.join(cls.tmp.name, "a.wav")
        cls.b = os.path.join(cls.tmp.name, "b.wav")
        click_track(cls.a, 120)
        click_track(cls.b, 126)
        cls._stems_env = os.environ.get("DJ_STEMS")
        os.environ["DJ_STEMS"] = "0"     # tests never start the real separator
        cls._settings_file = dj.SETTINGS_FILE
        dj.SETTINGS_FILE = os.path.join(cls.tmp.name, "dj_settings.json")
        cls._cache = dj.mixer.cache
        dj.mixer.cache = dj.mixer.AnalysisCache(os.path.join(cls.tmp.name, "cache.json"))

    @classmethod
    def tearDownClass(cls):
        dj.mixer.cache = cls._cache
        dj.SETTINGS_FILE = cls._settings_file
        if cls._stems_env is None:
            os.environ.pop("DJ_STEMS", None)
        else:
            os.environ["DJ_STEMS"] = cls._stems_env
        cls.tmp.cleanup()

    def session(self, auto=False):
        session = dj.DJSession("t", "test", ListCrate([self.a, self.b]), auto=auto)
        session.live = True
        return session

    def run_async(self, coro):
        return asyncio.run(coro)

    def test_blocks_are_one_opus_frame_even_when_empty(self):
        session = self.session()
        self.assertEqual(len(session.engine.render()), dj.FRAME_BYTES)

    def test_load_analyses_and_cues_on_a_beat(self):
        session = self.session()
        self.run_async(session.load("A", {"title": "a", "file": self.a}))
        deck = session.engine.decks["A"]
        self.assertTrue(deck.loaded)
        self.assertAlmostEqual(deck.bpm, 120, delta=2)
        self.assertEqual(len(deck.wave["low"]) > 0, True)
        beat = deck.beat_at(deck.cue)
        self.assertAlmostEqual(beat, round(beat), delta=0.05)

    def test_sync_matches_tempo(self):
        session = self.session()
        self.run_async(session.load("A", {"title": "a", "file": self.a}))
        self.run_async(session.load("B", {"title": "b", "file": self.b}))
        engine = session.engine
        engine.decks["A"].playing = True
        engine.master_name = "A"
        self.run_async(session.handle({"op": "sync", "deck": "B"}))
        self.run_async(session.handle({"op": "play", "deck": "B"}))
        for _ in range(50):
            engine.render()
        a, b = engine.decks["A"], engine.decks["B"]
        self.assertAlmostEqual(a.live_bpm(), b.live_bpm(), delta=0.05)
        phase = (a.beat_at(a.seconds) - b.beat_at(b.seconds) + 0.5) % 1 - 0.5
        self.assertLess(abs(phase), 0.05)

    def test_loop_keeps_the_playhead_inside(self):
        session = self.session()
        self.run_async(session.load("A", {"title": "a", "file": self.a}))
        deck = session.engine.decks["A"]
        deck.playing = True
        self.run_async(session.handle({"op": "loop", "deck": "A", "beats": 1}))
        for _ in range(200):  # 4 s, eight trips round a half-second loop
            session.engine.render()
        self.assertTrue(deck.loop_in <= deck.seconds <= deck.loop_out + 0.001)

    def test_hot_cue_sets_then_jumps(self):
        session = self.session()
        self.run_async(session.load("A", {"title": "a", "file": self.a}))
        deck = session.engine.decks["A"]
        deck.seek(10.0)
        self.run_async(session.handle({"op": "hotcue", "deck": "A", "index": 2}))
        stored = deck.hotcues[2]
        deck.seek(30.0)
        self.run_async(session.handle({"op": "hotcue", "deck": "A", "index": 2}))
        self.assertAlmostEqual(deck.seconds, stored, delta=0.001)
        self.assertTrue(deck.playing)

    def test_every_effect_and_sample_renders(self):
        session = self.session()
        self.run_async(session.load("A", {"title": "a", "file": self.a}))
        session.engine.decks["A"].playing = True
        for kind in dj.FX_TYPES:
            self.run_async(session.handle({"op": "fx", "type": kind, "on": True, "level": 1}))
            for _ in range(10):
                self.assertEqual(len(session.engine.render()), dj.FRAME_BYTES)
        for kind in dj.COLOR_FX:
            self.run_async(session.handle({"op": "set", "deck": "A", "param": "color_fx", "value": kind}))
            self.run_async(session.handle({"op": "set", "deck": "A", "param": "color", "value": 0.8}))
            session.engine.render()
        for name in dj.SAMPLES:
            self.run_async(session.handle({"op": "sample", "name": name}))
        self.assertEqual(len(session.engine.render()), dj.FRAME_BYTES)

    def test_manual_mix_hands_over_to_the_other_deck(self):
        session = self.session()
        self.run_async(session.load("A", {"title": "a", "file": self.a}))
        self.run_async(session.load("B", {"title": "b", "file": self.b}))
        engine = session.engine
        engine.decks["A"].playing = True
        engine.xfader = -1
        self.run_async(session.handle({"op": "mix", "style": "blend", "beats": 8}))
        for _ in range(400):  # 8 s: one bar of wait plus eight beats of blend
            engine.render()
        self.assertIsNone(engine.transition)
        self.assertFalse(engine.decks["A"].playing)
        self.assertTrue(engine.decks["B"].playing)
        self.assertEqual(engine.xfader, 1.0)
        self.assertEqual(engine.decks["B"].eq["low"], 0.0)

    def test_auto_dj_starts_from_the_queue(self):
        session = self.session(auto=True)

        async def run():
            await session._tick()
            await asyncio.gather(*session.load_tasks.values())
        self.run_async(run())
        self.assertTrue(any(d.playing for d in session.engine.decks.values()))
        self.assertEqual(len(session.crate.files), 1)

    # ---- stems ----

    @staticmethod
    def fake_stems(deck, drums=0.25, bass=0.25, vocals=0.25):
        """Stems that are fixed fractions of the mix; other gets the rest."""
        audio = deck.audio.astype(np.float32)
        deck.stems = np.stack([audio * f for f in (drums, bass, vocals)]).astype(np.int16)
        deck.stems_status = "ready"

    def read_at(self, deck, seconds, gains=None):
        deck.seek(seconds)
        deck.playing = True
        deck.rate_now = deck.base_rate()
        if gains is not None:
            deck.stem_gain = dict(gains)
            deck.stem_now = dict(gains)
        return deck.read(dj.BLOCK, 0.0)

    def test_full_stems_are_the_original_track(self):
        session = self.session()
        self.run_async(session.load("A", {"title": "a", "file": self.a}))
        deck = session.engine.decks["A"]
        plain = self.read_at(deck, 5.0)
        self.fake_stems(deck)
        np.testing.assert_array_equal(self.read_at(deck, 5.0), plain)
        half = {name: 0.5 for name in dj.stems.CONTROLS}
        np.testing.assert_allclose(self.read_at(deck, 5.0, half), plain * 0.5, atol=1e-6)

    def test_muting_a_stem_takes_it_out(self):
        session = self.session()
        self.run_async(session.load("A", {"title": "a", "file": self.a}))
        deck = session.engine.decks["A"]
        plain = self.read_at(deck, 5.0)
        self.fake_stems(deck, drums=1.0, bass=0.0, vocals=0.0)   # all drums
        gains = {name: 1.0 for name in dj.stems.CONTROLS}
        gains["drums"] = 0.0
        self.assertLess(np.abs(self.read_at(deck, 5.0, gains)).max(), 1e-3)
        gains = {name: 0.0 for name in dj.stems.CONTROLS}
        gains["drums"] = 1.0
        np.testing.assert_allclose(self.read_at(deck, 5.0, gains), plain, atol=1e-3)

    def test_stem_gain_changes_are_ramped(self):
        session = self.session()
        self.run_async(session.load("A", {"title": "a", "file": self.a}))
        deck = session.engine.decks["A"]
        plain = self.read_at(deck, 5.0)
        self.fake_stems(deck, drums=1.0, bass=0.0, vocals=0.0)
        self.read_at(deck, 5.0, {name: 1.0 for name in dj.stems.CONTROLS})
        deck.seek(5.0)
        deck.stem_gain["drums"] = 0.0           # mid-play: no click
        out = deck.read(dj.BLOCK, 0.0)
        np.testing.assert_allclose(out[0], plain[0], atol=2e-3)
        self.assertLess(np.abs(out[-1]).max(), 1e-3)

    def test_stem_controls_and_state(self):
        session = self.session()
        self.run_async(session.load("A", {"title": "a", "file": self.a}))
        self.run_async(session.handle({"op": "set", "deck": "A", "param": "stem_vocals", "value": 3}))
        self.run_async(session.handle({"op": "set", "deck": "A", "param": "stem_bass", "value": 0}))
        state = session.engine.decks["A"].state()["stems"]
        self.assertEqual(state["gain"]["vocals"], 1.0)
        self.assertEqual(state["gain"]["bass"], 0.0)
        with self.assertRaises(ValueError):
            self.run_async(session.handle({"op": "set", "deck": "A", "param": "stem_kazoo", "value": 0}))

    def test_stem_styles_need_stems_on_both_decks(self):
        session = self.session()
        self.run_async(session.load("A", {"title": "a", "file": self.a}))
        self.run_async(session.load("B", {"title": "b", "file": self.b}))
        engine = session.engine
        a, b = engine.decks["A"], engine.decks["B"]
        self.assertNotIn(dj.choose_style(a, b), dj.STEM_STYLES)
        self.fake_stems(a)
        self.fake_stems(b)
        self.assertIn(dj.choose_style(a, b), dj.STEM_STYLES)
        b.stems = None
        a.playing = True
        engine.xfader = -1
        self.run_async(session.handle({"op": "mix", "style": "stem_swap", "beats": 8, "now": True}))
        engine.render()
        self.assertEqual(engine.transition.style, "blend")

    def test_stem_transitions_hand_over_and_reset(self):
        for style in dj.STEM_STYLES:
            session = self.session()
            self.run_async(session.load("A", {"title": "a", "file": self.a}))
            self.run_async(session.load("B", {"title": "b", "file": self.b}))
            engine = session.engine
            a, b = engine.decks["A"], engine.decks["B"]
            self.fake_stems(a)
            self.fake_stems(b)
            a.playing = True
            engine.xfader = -1
            self.run_async(session.handle({"op": "mix", "style": style, "beats": 8}))
            seen_partial = False
            for _ in range(400):
                engine.render()
                if engine.transition and engine.transition.started:
                    gains = list(a.stem_gain.values()) + list(b.stem_gain.values())
                    seen_partial |= any(0 < g < 1 for g in gains)
            self.assertIsNone(engine.transition, style)
            self.assertTrue(seen_partial, style)
            self.assertTrue(b.playing and not a.playing, style)
            self.assertEqual(engine.xfader, 1.0)
            self.assertEqual(set(b.stem_gain.values()), {1.0})
            self.assertEqual(set(a.stem_gain.values()), {1.0})

    def test_stems_switch_turns_off_cleanly_and_persists(self):
        session = self.session()
        self.run_async(session.load("A", {"title": "a", "file": self.a}))
        self.run_async(session.load("B", {"title": "b", "file": self.b}))
        a, b = session.engine.decks["A"], session.engine.decks["B"]
        self.fake_stems(a, drums=1.0, bass=0.0, vocals=0.0)
        self.fake_stems(b)
        plain_gains = {name: 1.0 for name in dj.stems.CONTROLS}
        muted = dict(plain_gains, drums=0.0)
        self.read_at(a, 5.0, muted)
        self.assertIn(dj.choose_style(a, b), dj.STEM_STYLES)
        session.set_stems(False)
        self.assertFalse(session.state()["stems"]["on"])
        self.assertNotIn(dj.choose_style(a, b), dj.STEM_STYLES)
        with self.assertRaises(ValueError):
            self.run_async(session.handle({"op": "set", "deck": "A", "param": "stem_bass", "value": 0}))
        a.seek(5.0)
        ramp = a.read(dj.BLOCK, 0.0)             # fades the drums back in
        self.assertLess(np.abs(ramp[0]).max(), 2e-3)
        self.assertGreater(np.abs(ramp[-1]).max(), 0)
        a.read(dj.BLOCK, 0.0)
        self.assertIsNone(a.stems)               # memory let go once it is back to full
        self.assertFalse(self.session().stems_on)   # remembered for the room
        session.set_stems(True)
        self.assertTrue(self.session().stems_on)

    def test_key_clash_bridges_on_the_drums_only(self):
        session = self.session()
        self.run_async(session.load("A", {"title": "a", "file": self.a}))
        self.run_async(session.load("B", {"title": "b", "file": self.b}))
        a, b = session.engine.decks["A"], session.engine.decks["B"]
        a.meta["camelot"], b.meta["camelot"] = "8A", "2B"
        self.assertEqual(dj.choose_style(a, b), "filter")
        self.assertEqual(dj.auto_beats("filter", a, b), 8)
        self.fake_stems(a)
        self.fake_stems(b)
        self.assertEqual(dj.choose_style(a, b), "drum_bridge")
        engine = session.engine
        a.playing = True
        engine.xfader = -1
        self.run_async(session.handle({"op": "mix", "style": "drum_bridge", "beats": 16, "now": True}))
        overlap = 0.0
        for _ in range(600):
            engine.render()
            if engine.transition and engine.transition.started:
                overlap = max(overlap, min(a.stem_gain["other"], b.stem_gain["other"]))
        self.assertIsNone(engine.transition)
        self.assertTrue(b.playing)
        self.assertLess(overlap, 0.01)          # the two melodies never sound together

    def test_auto_dj_mixes_out_where_the_vocal_rests(self):
        session = self.session()
        self.run_async(session.load("A", {"title": "a", "file": self.a}))
        a = session.engine.decks["A"]
        self.fake_stems(a)
        end_beat = a.beat_at(a.music_end())
        latest = dj.mix_out_beat(a, "blend", 16, end_beat, 0)
        # Sing over the last phrases only: the mix should move earlier.
        vocals = np.zeros_like(a.audio)
        sing_from = int(a.time_of_beat(latest - 8) * dj.SR)
        vocals[sing_from:] = a.audio[sing_from:]
        a.stems[2] = vocals
        moved = dj.mix_out_beat(a, "blend", 16, end_beat, 0)
        self.assertLess(moved, latest)
        self.assertEqual((latest - moved) % 8, 0)
        self.assertEqual(dj.mix_out_beat(a, "echo", 8, end_beat, 0) % 8, 0)

    def test_loop_roll_shrinks_then_drops_the_new_song(self):
        session = self.session()
        self.run_async(session.load("A", {"title": "a", "file": self.a}))
        self.run_async(session.load("B", {"title": "b", "file": self.b}))
        engine = session.engine
        a, b = engine.decks["A"], engine.decks["B"]
        a.playing = True
        a.seek(20.0)
        engine.xfader = -1
        self.run_async(session.handle({"op": "mix", "style": "loop_roll", "now": True}))
        sizes = set()
        for _ in range(400):
            engine.render()
            tr = engine.transition
            if tr and tr.started and a.loop_on:
                sizes.add(round((a.loop_out - a.loop_in) / a.beat_seconds, 2))
                self.assertFalse(b.playing)       # nothing of B until the drop
        self.assertIsNone(engine.transition)
        self.assertTrue(b.playing and not a.playing)
        self.assertEqual(engine.xfader, 1.0)
        self.assertTrue({4, 2, 1}.issubset(sizes), sizes)

    def test_separator_uses_worker_then_cache(self):
        fake = os.path.join(self.tmp.name, "fake_worker.py")
        with open(fake, "w") as f:
            f.write(
                "import json, sys\n"
                "import numpy as np\n"
                "print(json.dumps({'ready': True, 'device': 'cpu', 'model': 'fake'}), flush=True)\n"
                "for line in sys.stdin:\n"
                "    job = json.loads(line)\n"
                "    pcm = np.fromfile(job['input'], dtype='<i2').reshape(-1, 2)\n"
                "    np.save(job['output'], np.stack([pcm // 2, pcm // 4, pcm // 8]))\n"
                "    print(json.dumps({'id': job['id'], 'ok': True, 'seconds': 0}), flush=True)\n")
        saved = (dj.stems.PYTHON, dj.stems.WORKER, dj.stems.CACHE_DIR)
        dj.stems.PYTHON, dj.stems.WORKER = sys.executable, fake
        dj.stems.CACHE_DIR = os.path.join(self.tmp.name, "stems")
        os.environ["DJ_STEMS"] = "1"
        audio = (np.arange(48_000 * 2, dtype=np.int64) % 2000 - 1000).astype(np.int16).reshape(-1, 2)

        async def run():
            sep = dj.stems.Separator()
            try:
                first = await sep.separate(audio)
                await sep._kill()
                second = await sep.separate(audio)          # cache hit, no worker
                self.assertIsNone(sep.proc)
                skipped = await sep.separate(audio[:-2], wanted=lambda: False)
                return first, second, skipped
            finally:
                await sep._kill()
                if sep.idle_task:
                    sep.idle_task.cancel()
        try:
            first, second, skipped = self.run_async(run())
        finally:
            dj.stems.PYTHON, dj.stems.WORKER, dj.stems.CACHE_DIR = saved
            os.environ["DJ_STEMS"] = "0"
        self.assertEqual(first.shape, (3,) + audio.shape)
        np.testing.assert_array_equal(first[0], audio // 2)
        np.testing.assert_array_equal(first, second)
        self.assertIsNone(skipped)

    def test_on_air_reports_changes_drift_and_silence(self):
        session = self.session()
        self.run_async(session.load("A", {"title": "a", "artist": "x", "file": self.a}))
        self.run_async(session.load("B", {"title": "b", "file": self.b}))
        engine = session.engine
        sent = []

        async def report(info):
            sent.append(info)
        session.report_on_air = report
        a = engine.decks["A"]
        a.playing = True
        engine.xfader = -1
        self.run_async(session._report())
        self.assertEqual(sent[-1]["title"], "a")
        self.assertFalse(sent[-1]["paused"])
        self.run_async(session._report())          # nothing changed: quiet
        self.assertEqual(len(sent), 1)
        a.seek(30.0)                                # a jump is drift
        session.reported = (session.reported[0], session.reported[1] - 2)
        self.run_async(session._report())
        self.assertEqual(len(sent), 2)
        self.assertAlmostEqual(sent[-1]["positionMs"], 30_000, delta=50)
        engine.xfader = 1                           # deck A out, B silent
        a.playing = False
        self.run_async(session._report())
        self.assertEqual(sent[-1]["title"], "a")
        self.assertTrue(sent[-1]["paused"])
        engine.decks["B"].playing = True
        self.run_async(session._report())
        self.assertEqual(sent[-1]["title"], "b")

    def test_expired_stream_link_is_resolved_again(self):
        session = self.session()
        asked = []

        async def resolver(query):
            asked.append(query)
            return {"audio_url": self.a, "page_url": "https://www.youtube.com/watch?v=aaaaaaaaaaa"}
        saved, dj.resolver = dj.resolver, resolver
        try:
            self.run_async(session.load("A", {"title": "a", "query": "song a",
                                              "audio_url": "/nonexistent/expired.webm"}))
        finally:
            dj.resolver = saved
        self.assertEqual(asked, ["song a"])
        self.assertTrue(session.engine.decks["A"].loaded)

    def test_loading_a_deck_on_air_is_refused(self):
        session = self.session()
        self.run_async(session.load("A", {"title": "a", "file": self.a}))
        session.engine.decks["A"].playing = True
        session.engine.xfader = -1
        with self.assertRaises(ValueError):
            self.run_async(session.handle({"op": "load", "deck": "A", "query": "x"}))


if __name__ == "__main__":
    unittest.main()
