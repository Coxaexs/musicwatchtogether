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


def tone(t, hz, harmonics=1):
    """A note with `harmonics` overtones falling away (1: a sine)."""
    return sum(np.sin(2 * np.pi * hz * h * t) / h for h in range(1, harmonics + 1))


def notes_song():
    """8 s with known notes. Bass: A1 then D2. The rest of the music: a C major
    chord, then A minor. Voice: quiet, then E4 (with vibrato), then G4."""
    n = SR * SECONDS
    t = np.arange(n) / SR
    first = t < SECONDS / 2
    bass = 0.25 * np.where(first, tone(t, 55.0, 5), tone(t, 73.42, 5))
    major = tone(t, 261.63, 3) + tone(t, 329.63, 3) + tone(t, 392.0, 3)
    minor = tone(t, 220.0, 3) + tone(t, 261.63, 3) + tone(t, 329.63, 3)
    other = 0.06 * np.where(first, major, minor)
    wobble = 2 ** (0.2 / 12 * np.sin(2 * np.pi * 5 * t))          # vibrato, a fifth of a semitone
    voice = np.where(t < 5, np.sin(2 * np.pi * np.cumsum(329.63 * wobble) / SR), np.sin(2 * np.pi * 392.0 * t))
    voice = voice + 0.5 * np.sin(2 * np.pi * 2 * np.where(t < 5, 329.63, 392.0) * t)
    vocals = 0.2 * np.where(t >= 2, voice, 0.0)
    drums = synthetic_song()[1][0][:, 0] / (0.8 * 32767)      # the same kicks
    to16 = lambda x: (np.stack([x, x], axis=1) * 0.4 * 32767).astype(np.int16)   # all together stays under full scale
    return to16(drums + bass + vocals + other), np.stack([to16(drums), to16(bass), to16(vocals)])


def note_at(notes, seconds, part):
    """The pitch class (0 = C) a part is on at `seconds`, or None."""
    value = int(frame_at(notes, seconds)[club.NOTE_PARTS.index(part)])
    return (value & 15) - 1 if value else None


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
        env = club.light(audio, parts)
        self.assertEqual(env.shape, (len(audio) // club.BLOCK, len(club.CHANNELS) + len(club.NOTE_PARTS)))
        self.assertLess(time.perf_counter() - started, 15.0)


A, C, D, E, G = 9, 0, 2, 4, 7


class NoteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.audio, cls.parts = notes_song()

    def test_with_stems_each_part_is_on_its_note(self):
        notes = club.notes(self.audio, self.parts)
        self.assertEqual(notes.dtype, np.uint8)
        self.assertEqual(notes.shape, (len(self.audio) // club.BLOCK, 3))
        self.assertEqual([note_at(notes, s, "bass") for s in (1.0, 2.25, 3.5)], [A, A, A])
        self.assertEqual([note_at(notes, s, "bass") for s in (4.6, 6.25, 7.5)], [D, D, D])
        self.assertEqual([note_at(notes, s, "chord") for s in (1.0, 3.0)], [C, C])
        self.assertEqual([note_at(notes, s, "chord") for s in (5.0, 7.0)], [A, A])
        self.assertIsNone(note_at(notes, 1.0, "vocal"))               # not singing yet
        self.assertEqual([note_at(notes, s, "vocal") for s in (2.5, 3.5, 4.5)], [E, E, E])
        self.assertEqual([note_at(notes, s, "vocal") for s in (5.5, 7.0)], [G, G])
        # A clear, held note is a strong one.
        self.assertGreaterEqual(int(frame_at(notes, 3.0)[0]) >> 4, 8)

    def test_without_stems_the_bass_comes_off_the_mix_and_there_is_no_voice(self):
        notes = club.notes(self.audio)
        self.assertEqual(note_at(notes, 2.25, "bass"), A)
        self.assertEqual(note_at(notes, 6.25, "bass"), D)
        self.assertIsNotNone(note_at(notes, 2.25, "chord"))
        self.assertEqual(int(notes[:, club.NOTE_PARTS.index("vocal")].max()), 0)

    def test_silence_has_no_notes(self):
        quiet = np.zeros((SR * 2, 2), np.int16)
        self.assertEqual(int(club.notes(quiet, np.zeros((3, SR * 2, 2), np.int16)).max()), 0)
        self.assertEqual(club.notes(quiet[:100]).shape, (0, 3))

    def test_the_player_reads_the_bass_and_the_chord_as_it_plays(self):
        meter = club.PlayerMeter()
        t = np.arange(SR * 2) / SR
        mix = 0.3 * np.sin(2 * np.pi * 55.0 * t) + 0.05 * (tone(t, 261.63, 3) + tone(t, 329.63, 3) + tone(t, 392.0, 3))
        pcm = (np.stack([mix, mix], axis=1) * 32767).astype("<i2")
        for i in range(0, len(pcm) - club.BLOCK + 1, club.BLOCK):
            meter.frame(pcm[i:i + club.BLOCK].tobytes())
        bass, chord, vocal = meter.notes
        self.assertEqual((bass & 15) - 1, A)
        self.assertEqual((chord & 15) - 1, C)
        self.assertEqual(vocal, 0)
        meter.reset()
        self.assertEqual(meter.notes, bytes(3))


def lit_engine():
    audio, parts = synthetic_song()
    engine = dj.DJEngine()
    deck = engine.decks["A"]
    deck.audio, deck.stems = audio, parts
    deck.meta = {"bpm": BPM, "bpm_confidence": 1.0, "first_beat": 0.0}
    deck.light = club.light(audio, parts)
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
        values, notes, beat, bpm, lit_by_stems = club.frame(engine, ahead=0.05)   # lands on the kick at 6.0 s
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

    def test_each_part_has_the_note_of_the_deck_it_sounds_loudest_on(self):
        engine, a = lit_engine()
        audio, parts = notes_song()
        a.audio, a.stems, a.light = audio, parts, club.light(audio, parts)
        b = engine.decks["B"]
        b.audio, b.stems, b.light = audio, parts, club.light(audio, parts)
        b.meta = dict(a.meta)
        a.seek(2.25)
        a.rate_now = 1.0
        _, notes, *_ = club.frame(engine, ahead=0.0)
        self.assertEqual([(n & 15) - 1 for n in notes], [A, C, E])
        a.stem_now = dict(a.stem_now, bass=0.0)          # the bass muted: no bass note
        _, notes, *_ = club.frame(engine, ahead=0.0)
        self.assertEqual(notes[0], 0)
        self.assertEqual((notes[1] & 15) - 1, C)
        # Deck B in the second half comes up over the crossfader: its notes take over.
        b.seek(6.25)
        b.rate_now, b.playing, b.fader = 1.0, True, 1.0
        engine.xfader = 1.0
        _, notes, *_ = club.frame(engine, ahead=0.0)
        self.assertEqual([(n & 15) - 1 for n in notes], [D, A, G])
        # A light from before notes (6 channels) lights with no notes.
        b.light = club.envelope(audio, parts)
        values, notes, *_ = club.frame(engine, ahead=0.0)
        self.assertGreater(max(values), 0.1)
        self.assertEqual(notes, bytes(3))

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

    def test_notes_ride_beside_their_frames(self):
        feed = club.Feed()
        feed.add(1000, [0.5] * 6, notes=bytes([0x9A, 0, 0x45]))
        feed.add(1020, [0.5] * 6)                          # a frame without notes: zeros
        feed.add(1040, [0.5] * 6, notes=bytes([0x9A, 0x31, 0]))
        [message] = feed.take(now=1.0)
        self.assertEqual(base64.b64decode(message["n"]), bytes([0x9A, 0, 0x45, 0, 0, 0, 0x9A, 0x31, 0]))
        feed.add(1060, [0.5] * 6)
        self.assertNotIn("n", feed.take(now=1.1)[0])      # no notes at all: none sent

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


class CrowdTests(unittest.TestCase):
    def test_a_tap_moves_the_vibe_a_step_and_fades(self):
        crowd = club.Crowd()
        crowd.hear("more", "a", people=1, now=0)
        self.assertEqual(crowd.mood(0), 1.0)
        self.assertEqual(crowd.follow(0), 1)
        self.assertEqual(crowd.info(1), {"more": 1, "chill": 0, "said": "up"})
        self.assertEqual(crowd.follow(60), 1)             # half faded: still above CROWD_SETTLE
        self.assertEqual(crowd.info(60), {"more": 1, "chill": 0, "said": None})
        self.assertEqual(crowd.follow(100), 0)            # faded below it
        self.assertIsNone(crowd.info(club.CROWD_VOTE_S + 1))
        self.assertEqual(club.crowd_vibe("club", 1), "hype")
        self.assertEqual(club.crowd_vibe("hype", 1), "hype")
        self.assertEqual(club.crowd_vibe("club", -1), "chill")
        self.assertEqual(club.crowd_vibe("chill", -1), "chill")

    def test_the_room_has_to_lean_one_way(self):
        crowd = club.Crowd()
        crowd.hear("more", "a", people=4, now=0)
        self.assertEqual(crowd.follow(0), 0)              # one of four
        crowd.hear("more", "b", people=4, now=0)
        self.assertEqual(crowd.follow(0), 1)              # half the room
        crowd.hear("chill", "b", people=4, now=1)         # changes their mind: their last tap counts
        self.assertEqual(crowd.counts(1), (1, 1))
        self.assertEqual(crowd.follow(1), 0)
        crowd.hear("chill", "a", people=4, now=2)
        self.assertEqual(crowd.follow(2), -1)
        self.assertEqual(crowd.info(2)["said"], "down")
        crowd.hear("nonsense", "c", people=True, now=3)   # ignored, and so is a bad head count
        self.assertEqual((crowd.counts(3), crowd.people), ((0, 2), 4))

    def test_cheering_together_calls_a_drop_now_and_then(self):
        crowd = club.Crowd()
        for at in (0, 1):
            crowd.hear("cheer", "a", people=1, now=at)
        self.assertFalse(crowd.cheered(1))
        crowd.hear("cheer", "a", people=1, now=2)
        self.assertTrue(crowd.cheered(2))                 # alone: three cheers in a few seconds
        crowd.dropping(2)
        for at in (3, 4, 5):
            crowd.hear("cheer", "a", now=at)
        self.assertFalse(crowd.cheered(5))                # the last drop was a moment ago
        self.assertEqual(crowd.info(5)["said"], "drop")
        later = 2 + club.CROWD_DROP_EVERY_S
        for at in range(5):
            crowd.hear("cheer", "a", people=4, now=later + at)
        self.assertFalse(crowd.cheered(later + 4))        # one of four cheering, however much
        crowd.hear("cheer", "b", now=later + 4)
        self.assertTrue(crowd.cheered(later + 4))
        crowd.cheers.clear()
        for at in (0, 1, 2, 9):
            crowd.hear("cheer", "c", people=1, now=later + 20 + at)
        self.assertFalse(crowd.cheered(later + 29.5))     # the first ones are too old


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

    def test_auto_dj_starts_the_night_too(self):
        session = asyncio.run(self.webui.WebUI._start_club(self.ui, "huddle:room-1", "chill", "auto"))
        self.assertEqual((session.mode, session.auto, session.vibe), ("auto", True, "chill"))
        self.assertEqual(len(self.queued), 1)


class AutoDJTests(unittest.TestCase):
    """Auto DJ (the booth mixing the queue by itself) under the club's lights."""

    def test_the_lights_follow_an_auto_dj_mix_from_one_song_into_the_next(self):
        audio, parts = synthetic_song()
        session = dj.DJSession("huddle:auto", "huddle", crate=SimpleNamespace(items=lambda: [{"title": "Third"}]), auto=True)
        session.save_state = lambda: None
        session.live = True
        session.auto_style, session.auto_beats = "fade", 4
        engine = session.engine
        engine.light = club.Feed()
        for name, title in (("A", "One"), ("B", "Two")):
            deck = engine.decks[name]
            deck.audio, deck.stems = audio, parts
            deck.meta = {"bpm": BPM, "bpm_confidence": 1.0, "first_beat": 0.0, "camelot": "8A"}
            deck.light = club.envelope(audio, parts)
            deck.info = {"title": title}
        a, b = engine.decks["A"], engine.decks["B"]
        a.playing, a.fader = True, 1.0
        engine.master_name, engine.xfader = "A", -1.0
        a.seek(3.0)
        asyncio.run(session._tick())
        self.assertEqual(engine.light.info["mode"], "auto")
        self.assertEqual(engine.light.info["next"], "Two")
        mixes, beats, renders = [], [], 0
        while not (b.playing and engine.transition is None) and renders < 400:
            engine.render()
            renders += 1
            values, notes, beat, bpm, lit = club.frame(engine, ahead=0.0)
            beats.append(beat)
            if renders % 12 == 0:                         # the session ticks every quarter second
                asyncio.run(session._tick())
                if engine.light.info.get("mix"):
                    mixes.append(engine.light.info["mix"]["progress"])
        self.assertTrue(b.playing, "Auto DJ never mixed into the next song")
        self.assertIsNone(engine.transition)
        self.assertTrue(mixes, "the club never showed the mix")
        self.assertEqual(mixes, sorted(mixes))
        # A frame for every block rendered, all the way through the mix, and always a beat.
        self.assertEqual(sum(1 for f in engine.light.frames if f), min(200, renders))
        self.assertNotIn(None, beats)
        asyncio.run(session._tick())
        self.assertEqual(engine.light.info["next"], "Third")


class FakeFFmpeg:
    """A decoder whose output is a tone, block after block."""

    def __init__(self, seconds):
        t = np.arange(int(seconds * SR)) / SR
        self.data = (np.stack([0.3 * np.sin(2 * np.pi * 220 * t)] * 2, axis=1) * 32767).astype("<i2").tobytes()
        self.returncode = None
        self.stdout = self
        self.pid = 0

    async def readexactly(self, n):
        if len(self.data) < n:
            rest, self.data = self.data, b""
            raise asyncio.IncompleteReadError(rest, n)
        chunk, self.data = self.data[:n], self.data[n:]
        return chunk

    def terminate(self):
        self.returncode = 0

    async def communicate(self):
        return b"", b""


class AutoMixLightTests(unittest.TestCase):
    """The room's ordinary player with AutoMix: a rendered transition spliced between two songs."""

    def test_the_beat_holds_up_to_the_blend_and_picks_up_the_next_song_after_it(self):
        import huddle_voice

        async def run():
            track = huddle_voice.RoomAudioTrack()
            track.light = club.Feed()
            track.analysis = {"bpm": BPM, "bpm_confidence": 1.0, "first_beat": 0.0}
            track._paused = False
            track._url = "old"
            track._process = FakeFFmpeg(4.0)
            blend = (np.zeros((SR, 2)) + 0.1 * 32767).astype("<i2").tobytes()     # one second
            started = []
            track.set_mix({"url": "old", "out_at": 2.0, "segment": blend, "next_url": "new",
                           "next_resume": 1.0, "next_track_id": "t2", "on_start": lambda: started.append(True)})

            async def spawn(url, position, filters):
                return FakeFFmpeg(3.0)
            track._spawn = spawn
            seen = []
            add = track.light.add

            def spy(t_ms, values, beat=None, bpm=None, stems=False, notes=None):
                seen.append((beat, (track.light.info or {}).get("mix")))
                self.assertEqual(len(notes), 3)
                add(t_ms, values, beat, bpm, stems, notes)
            track.light.add = spy
            pump = asyncio.create_task(track._pump_frames())
            await asyncio.sleep(0.05)
            for _ in range(240):
                track._next_frame_at = -1e9                 # no pacing in a test ...
                await track.recv()
                await asyncio.sleep(0)                      # ... but the decoder keeps up, as it does live
            pump.cancel()
            await asyncio.gather(pump, return_exceptions=True)
            return seen, started

        seen, started = asyncio.run(run())
        self.assertEqual(started, [True])
        beats = [beat for beat, _ in seen]
        first_gap = beats.index(None)
        gap = len(beats) - first_gap - next(i for i, b in enumerate(reversed(beats)) if b is None)
        before, after = beats[:first_gap], beats[first_gap + gap:]
        # The old song, on its grid up to its mix point (2 s = beat 4) ...
        self.assertAlmostEqual(before[-1], 4.0, delta=0.1)
        self.assertTrue(all(b2 >= b1 for b1, b2 in zip(before, before[1:])))     # (a block's jitter at most)
        # ... no grid through the blend (one second, 50 blocks; the mix marker in the queue hides one) ...
        self.assertAlmostEqual(gap, 50, delta=1)
        progress = [mix["progress"] for _, mix in seen[first_gap:first_gap + gap]]
        self.assertEqual(progress, sorted(progress))
        self.assertGreater(progress[-1], 0.9)
        # ... then the new song's grid from where the blend leaves it (1 s = beat 2).
        self.assertAlmostEqual(after[0], 2.0, delta=0.1)
        self.assertTrue(all(b2 >= b1 for b1, b2 in zip(after, after[1:])))
        self.assertAlmostEqual(after[-1] - after[0], (len(after) - 1) * 0.04, delta=0.1)
        self.assertIsNone(seen[-1][1])


if __name__ == "__main__":
    unittest.main()
