"""The Hoffle club's lights (light envelopes, frames, the wire feed) and its DJ moves."""
import asyncio
import base64
import os
import random
import sys
import time
import unittest
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import club  # noqa: E402
import dj  # noqa: E402

SR = dj.SR
BPM = 120
SECONDS = 8


def synthetic_song():
    """8 s at 120 bpm: a kick on every beat (drums), a bass note throughout, a
    pad (other), and a voice that only sings in the second half."""
    n = SR * SECONDS
    t = np.arange(n) / SR
    drums = np.zeros(n)
    kick_t = np.arange(int(0.12 * SR)) / SR
    kick = np.sin(2 * np.pi * (50 + 60 * np.exp(-kick_t / 0.02)) * kick_t) * np.exp(-kick_t / 0.04)
    for start in np.arange(0, SECONDS, 60 / BPM):
        i = int(start * SR)
        drums[i:i + len(kick)] += kick[: n - i]
    bass = 0.25 * np.sin(2 * np.pi * 82 * t)
    vocals = np.where(t >= SECONDS / 2, 0.3 * np.sin(2 * np.pi * 620 * t), 0.0)
    other = 0.12 * np.sin(2 * np.pi * 1300 * t)
    to16 = lambda x: (np.stack([x, x], axis=1) * 0.8 * 32767).astype(np.int16)
    parts = np.stack([to16(drums), to16(bass), to16(vocals)])
    audio = to16(drums + bass + vocals + other)
    return audio, parts


def frame_at(env, seconds):
    return env[int(seconds * SR / club.BLOCK)]


class EnvelopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.audio, cls.parts = synthetic_song()

    def test_shape_and_rate(self):
        env = club.envelope(self.audio, self.parts)
        self.assertEqual(env.dtype, np.uint8)
        self.assertEqual(env.shape, (len(self.audio) // club.BLOCK, len(club.CHANNELS)))

    def test_stems_light_the_kick_on_the_beat_and_the_voice_when_it_sings(self):
        env = club.envelope(self.audio, self.parts)
        kick = club.CHANNELS.index("kick")
        vocals = club.CHANNELS.index("vocals")
        on = [frame_at(env, beat * 0.5 + 0.03)[kick] for beat in range(2, 14)]
        off = [frame_at(env, beat * 0.5 + 0.3)[kick] for beat in range(2, 14)]
        self.assertGreater(min(on), 120)
        self.assertLess(max(off), 40)
        self.assertLess(frame_at(env, 1.0)[vocals], 15)
        self.assertGreater(frame_at(env, 6.0)[vocals], 100)

    def test_without_stems_the_bands_stand_in_and_there_is_no_voice(self):
        env = club.envelope(self.audio)
        kick = club.CHANNELS.index("kick")
        self.assertEqual(int(env[:, club.CHANNELS.index("vocals")].max()), 0)
        on = np.mean([frame_at(env, beat * 0.5 + 0.03)[kick] for beat in range(2, 14)])
        off = np.mean([frame_at(env, beat * 0.5 + 0.3)[kick] for beat in range(2, 14)])
        self.assertGreater(on, off + 60)

    def test_a_long_song_is_quick_to_light(self):
        audio = np.tile(self.audio, (30, 1))            # four minutes
        parts = np.tile(self.parts, (1, 30, 1))
        started = time.perf_counter()
        env = club.envelope(audio, parts)
        self.assertEqual(len(env), len(audio) // club.BLOCK)
        self.assertLess(time.perf_counter() - started, 8.0)


def lit_engine():
    audio, parts = synthetic_song()
    engine = dj.DJEngine()
    deck = engine.decks["A"]
    deck.audio, deck.stems = audio, parts
    deck.meta = {"bpm": BPM, "bpm_confidence": 1.0, "first_beat": 0.0}
    deck.light = club.envelope(audio, parts)
    deck.playing = True
    engine.master_name = "A"
    engine.xfader = -1.0
    engine.light = club.Feed()
    return engine, deck


class FrameTests(unittest.TestCase):
    def test_the_frame_is_the_deck_where_it_will_be(self):
        engine, deck = lit_engine()
        deck.seek(5.97)
        deck.rate_now = 1.0
        values, beat, bpm, lit_by_stems = club.frame(engine, ahead=0.05)   # lands on the kick at 6.0 s
        self.assertTrue(lit_by_stems)
        self.assertAlmostEqual(bpm, BPM)
        self.assertAlmostEqual(beat, 12.04, places=2)
        self.assertGreater(values[club.CHANNELS.index("kick")], 0.3)
        self.assertGreater(values[club.CHANNELS.index("vocals")], 0.3)

    def test_what_the_room_does_not_hear_does_not_light(self):
        engine, deck = lit_engine()
        deck.seek(6.0)
        deck.rate_now = 1.0
        deck.stem_now = dict(deck.stem_now, vocals=0.0)
        values, *_ = club.frame(engine, ahead=0.0)
        self.assertEqual(values[club.CHANNELS.index("vocals")], 0.0)
        deck.eq["low"] = dj.KILL_DB
        values, *_ = club.frame(engine, ahead=0.0)
        self.assertEqual(values[club.CHANNELS.index("kick")], 0.0)
        self.assertEqual(values[club.CHANNELS.index("bass")], 0.0)
        engine.xfader = 1.0                               # over to deck B
        values, *_ = club.frame(engine, ahead=0.0)
        self.assertEqual(max(values), 0.0)

    def test_rendering_feeds_the_lights_only_for_a_room_booth(self):
        engine, deck = lit_engine()
        for _ in range(10):
            engine.render()
        self.assertEqual(len([f for f in engine.light.frames if f]), 10)
        engine.light = None
        engine.render()                                   # a Discord booth: no light work at all


class FeedTests(unittest.TestCase):
    def test_a_run_of_frames_is_one_message(self):
        feed = club.Feed()
        for i in range(5):
            feed.add(1000 + i * club.FRAME_MS, [i / 10] * 6, beat=8 + i * 0.04, bpm=120, stems=True)
        [message] = feed.take(now=1.0)
        self.assertEqual(message["at"], 1000)
        self.assertEqual(len(base64.b64decode(message["f"])), 30)
        self.assertAlmostEqual(message["b"], 8.0, places=2)
        self.assertEqual(message["bpm"], 120)
        self.assertTrue(message["stems"])
        self.assertEqual(feed.take(now=1.05), [])

    def test_frames_keep_the_audio_clock_and_a_stall_starts_a_new_message(self):
        feed = club.Feed()
        for t in (0, 23, 38, 300, 322):                  # sender jitter, then a stall
            feed.add(t, [0.5] * 6)
        messages = feed.take(now=1.0)
        self.assertEqual([m["at"] for m in messages], [0, 300])
        self.assertEqual([len(base64.b64decode(m["f"])) for m in messages], [18, 12])
        feed.add(340, [0.5] * 6)                         # the clock carries on across a take
        self.assertEqual(feed.take(now=1.1)[0]["at"], 340)

    def test_moments_and_info_ride_along(self):
        feed = club.Feed()
        feed.event("drop", 5000)
        feed.info = {"mode": "real"}
        [message] = feed.take(now=4.0)
        self.assertEqual(message["ev"], [{"k": "drop", "at": 5000}])
        self.assertEqual(message["info"], {"mode": "real"})
        self.assertEqual(feed.take(now=4.1), [])            # info again only when it changes ...
        self.assertEqual(feed.take(now=5.5)[0]["info"], {"mode": "real"})   # ... or each second


class PlayerMeterTests(unittest.TestCase):
    def block(self, x):
        return (np.stack([x, x], axis=1) * 32767).astype("<i2").tobytes()

    def test_bands(self):
        meter = club.PlayerMeter()
        t = np.arange(club.BLOCK) / SR
        low = self.block(0.5 * np.sin(2 * np.pi * 70 * t))
        hiss = self.block(np.random.default_rng(1).normal(0, 0.1, club.BLOCK) * np.sin(2 * np.pi * 5000 * t))
        values = [meter.frame(low) for _ in range(20)][-1]
        self.assertGreater(values[club.CHANNELS.index("bass")], 0.8)
        self.assertEqual(values[club.CHANNELS.index("vocals")], 0.0)
        values = meter.frame(hiss)
        self.assertGreater(values[club.CHANNELS.index("snare")], 0.8)
        self.assertLess(values[club.CHANNELS.index("kick")], 0.05)

    def test_player_beat(self):
        self.assertEqual(club.player_beat(None, 3.0), (None, None))
        beat, bpm = club.player_beat({"bpm": 120, "bpm_confidence": 0.8, "first_beat": 0.5}, 3.0)
        self.assertAlmostEqual(beat, 5.0)
        self.assertEqual(bpm, 120)


class MoveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dj.sample_bank(wait=True)

    def run_to(self, engine, deck, beat):
        while deck.beat_at(deck.seconds) < beat:
            self.assertTrue(deck.playing, "the song ended first")
            engine.render()

    def test_build_and_drop(self):
        engine, deck = lit_engine()
        session = SimpleNamespace(engine=engine)
        deck.seek(0.6)                                    # beat 1.2: the drop builds from beat 4
        engine.render()
        notice = club.perform(session, "drop")
        self.assertIn("next bar", notice)
        kinds = [e["k"] for e in engine.light.events]
        self.assertEqual(kinds, ["build", "drop"])
        build, drop = engine.light.events
        self.assertAlmostEqual((drop["at"] - build["at"]) / 1000, 8 * 0.5, delta=0.05)
        self.run_to(engine, deck, 6.2)
        self.assertEqual(deck.stem_gain["bass"], 0.0)
        self.assertGreater(deck.color, 0.2)
        self.run_to(engine, deck, 9.5)
        self.assertEqual((engine.fx.on, engine.fx.type, engine.fx.beat), (True, "roll", 2))
        self.run_to(engine, deck, 10.5)
        self.assertEqual((engine.fx.on, engine.fx.beat), (True, 1))
        self.run_to(engine, deck, 11.2)
        self.assertEqual(max(deck.stem_gain[k] for k in ("drums", "bass", "vocals", "other")), 0.0)
        self.run_to(engine, deck, 12.2)
        self.assertEqual(min(deck.stem_gain.values()), 1.0)
        self.assertEqual(deck.color, 0.0)
        self.assertFalse(engine.fx.on)
        hits = [e["name"] for e in engine.light.events if e["k"] == "sample"]
        self.assertEqual(hits, ["riser", "boom"])
        with self.assertRaises(ValueError):
            club.perform(SimpleNamespace(engine=engine, club_busy_until=((deck.name, deck.wave_id), 99)), "drop")

    def test_acapella_needs_stems_and_brings_the_band_back(self):
        engine, deck = lit_engine()
        session = SimpleNamespace(engine=engine)
        deck.seek(0.6)
        engine.render()
        club.perform(session, "acapella")             # beat 1.2: from bar 4 to 12
        self.run_to(engine, deck, 5.5)
        self.assertEqual((deck.stem_gain["drums"], deck.stem_gain["vocals"]), (0.0, 1.0))
        self.run_to(engine, deck, 12.5)
        self.assertEqual(deck.stem_gain["drums"], 1.0)
        deck.stems = None
        with self.assertRaises(ValueError):
            club.perform(SimpleNamespace(engine=engine), "acapella")

    def test_nothing_playing(self):
        engine, deck = lit_engine()
        deck.playing = False
        with self.assertRaises(ValueError):
            club.perform(SimpleNamespace(engine=engine), "stutter")


class NightTests(unittest.TestCase):
    def test_night_entries_skip_what_is_queued(self):
        entries = [{"title": f"Song {i}", "url": f"https://youtu.be/{i}"} for i in range(50)]
        picks = club.night_entries(entries, ["https://youtu.be/3", "Song 4"], count=30, rng=random.Random(2))
        self.assertEqual(len(picks), 30)
        titles = {p["title"] for p in picks}
        self.assertNotIn("Song 3", titles)
        self.assertNotIn("Song 4", titles)


class StyledSkipTests(unittest.TestCase):
    def test_the_club_mix_buttons_pick_the_style(self):
        audio, parts = synthetic_song()
        session = dj.DJSession("huddle:test", "huddle", crate=SimpleNamespace(items=lambda: []))
        engine = session.engine
        for name in ("A", "B"):
            deck = engine.decks[name]
            deck.audio, deck.stems = audio, parts
            deck.meta = {"bpm": BPM, "bpm_confidence": 1.0, "first_beat": 0.0}
        a = engine.decks["A"]
        a.playing = True
        a.seek(1.0)
        engine.master_name = "A"
        asyncio.run(session.skip("echo"))
        self.assertEqual(engine.transition.style, "echo")
        self.assertEqual(engine.transition.beats, dj.STYLES["echo"][1])
        engine.transition = None
        asyncio.run(session.skip("mashup"))
        self.assertEqual(engine.transition.style, "mashup")
        engine.decks["B"].stems = None                   # a stem style without stems: the booth's pick
        engine.transition = None
        asyncio.run(session.skip("mashup"))
        self.assertNotEqual(engine.transition.style, "mashup")


if __name__ == "__main__":
    unittest.main()


class StartNightTests(unittest.TestCase):
    """webui's "club" op: the booth on air in Real DJ club mode, stemmed songs queued."""

    def setUp(self):
        import huddle
        import huddle_voice
        import webui
        self.webui, self.huddle, self.huddle_voice = webui, huddle, huddle_voice
        self.saved = (huddle.room_state, huddle.play_many, huddle_voice.MANAGER, dj.start_huddle)
        self.queued = []
        self.session = dj.DJSession("huddle:room-1", "huddle", crate=SimpleNamespace(items=lambda: []))

        async def room_state(key):
            return {}

        async def play_many(key, entries, name, cover=None, start_now=False, mixes=None):
            self.queued.append((key, [e["title"] for e in entries], name))

        async def start_huddle(channel_id, auto=True, restore=False):
            self.started = channel_id
            return self.session

        huddle.room_state, huddle.play_many, dj.start_huddle = room_state, play_many, start_huddle
        huddle_voice.MANAGER = SimpleNamespace(players={"room-1": {"track": None, "queue": [{"title": "Already queued", "query": "q"}]}})
        library = [{"title": f"Stemmed {i}", "youtube": f"vid{i:08d}x", "seconds": 200, "karaoke": True} for i in range(40)]
        self.ui = SimpleNamespace(_refresh_stems_library=lambda: asyncio.sleep(0, result=library))

    def tearDown(self):
        self.huddle.room_state, self.huddle.play_many, self.huddle_voice.MANAGER, dj.start_huddle = self.saved

    def test_the_night_starts_with_real_dj_and_stemmed_songs(self):
        session = asyncio.run(self.webui.WebUI._start_club(self.ui, "huddle:room-1", "hype"))
        self.assertIs(session, self.session)
        self.assertEqual(self.started, "room-1")
        self.assertEqual((session.mode, session.vibe, session.auto), ("real", "hype", True))
        [(key, titles, name)] = self.queued
        self.assertEqual((key, name, len(titles)), ("huddle:room-1", self.webui.STEMS_PLAYLIST, 30))
        self.assertIn("30 stemmed songs", session.notice)

    def test_a_stocked_queue_is_left_alone(self):
        self.huddle_voice.MANAGER.players["room-1"]["queue"] = [{"title": f"Q{i}"} for i in range(10)]
        session = asyncio.run(self.webui.WebUI._start_club(self.ui, "huddle:room-1"))
        self.assertEqual(self.queued, [])
        self.assertEqual(session.vibe, "club")
