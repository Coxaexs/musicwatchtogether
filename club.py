"""The Hoffle Living Room's club: its light show, and its DJ's remix moves.

The club is a venue of the Living Room with a dance floor, beams, lasers and
an LED wall that play the music the room hears. With stems on a deck the bot
knows what is sounding part by part, so that is what it sends: the kick, the
snare and hats, the bass, the vocal and the rest of the music, mixed the way
the room hears them (faders, crossfader, EQ kills, stem mutes), plus the beat.
A song without stems (still separating, or the room's ordinary player) sends
the same channels read off the mix's frequency bands, with no vocal.

Light reaches the room by the hub socket, in batches, not with the sound. So
each 20 ms frame is worked out LOOKAHEAD seconds before its sound leaves (from
envelopes made when a deck loads, read where the deck will be by then) and
stamped with the time it belongs to; every listener holds it until their own
sound gets there.

The moves (perform) are what the person at the club's decks presses: a build
and drop, an acapella, a beat break, a stutter, an echo throw. They play the
booth's own controls on the deck that leads, on its beat, the way Real DJ's
flourishes do.
"""

import base64
import json
import math
import random
import time

import numpy as np
from scipy import signal

SR = 48_000
BLOCK = 960                        # 20 ms: one audio block, one light frame
FRAME_MS = 1000 * BLOCK / SR
CHANNELS = ("kick", "snare", "bass", "vocals", "melody", "level")
#: Seconds ahead of the sound that each light frame is worked out.
LOOKAHEAD = 0.25
#: How often the publisher sends what has gathered.
SEND_EVERY = 0.08
#: A track's envelope is computed at 12 kHz, in pieces of this many samples (48 kHz).
CHUNK = SR * 60
KILL_DB = -26.0                    # dj.KILL_DB: a knob fully left kills its band

_RATE = SR // 4
_HOP = BLOCK // 4
_LOW = signal.butter(4, 150, "lowpass", fs=_RATE, output="sos")
_HIGH = signal.butter(2, 1800, "highpass", fs=_RATE, output="sos")
_MID = signal.butter(2, [300, 3000], "bandpass", fs=_RATE, output="sos")


def _mono(source, start, end):
    """Samples start..end of an int16 (n, 2) array as float mono at 12 kHz."""
    x = source[start:end].astype(np.float32).mean(axis=1) / 32768.0
    return signal.resample_poly(x, 1, 4).astype(np.float32)


def _rms(x):
    n = len(x) // _HOP
    if n <= 0:
        return np.zeros(0, np.float32)
    return np.sqrt(np.mean(np.square(x[: n * _HOP].reshape(n, _HOP)), axis=1))


def _smooth(x, frames):
    if frames <= 1 or not len(x):
        return x
    kernel = np.ones(frames, np.float32) / frames
    return np.convolve(x, kernel, mode="same")


def _scaled(x, ref):
    return np.clip(x / max(ref, 1e-6), 0.0, 1.0)


def envelope(audio, parts=None):
    """A decoded track's light envelope: uint8 (frames, 6), 50 frames a second.

    parts: int16 (3, n, 2) drums / bass / vocals from stems.py, or None for a
    song without stems, whose channels are read off the mix's bands instead
    (and whose vocal is 0). Guitar and piano are part of the melody either way.
    """
    total = len(audio) // BLOCK
    rows = {name: [] for name in CHANNELS}
    zi = {}

    def filtered(name, sos, x):
        y, zi[name] = signal.sosfilt(sos, x, zi=zi.get(name, np.zeros((sos.shape[0], 2))))
        return y

    for start in range(0, total * BLOCK, CHUNK):
        end = min(start + CHUNK, total * BLOCK)
        mix = _mono(audio, start, end)
        rows["level"].append(_rms(mix))
        if parts is not None:
            drums, bass, vocals = (_mono(parts[i], start, end) for i in range(3))
            rows["kick"].append(_rms(filtered("kick", _LOW, drums)))
            rows["snare"].append(_rms(filtered("snare", _HIGH, drums)))
            rows["bass"].append(_rms(bass))
            rows["vocals"].append(_rms(vocals))
            rows["melody"].append(_rms(mix - drums - bass - vocals))
        else:
            low = _rms(filtered("low", _LOW, mix))
            rows["kick"].append(low)
            rows["bass"].append(low)
            rows["snare"].append(_rms(filtered("high", _HIGH, mix)))
            rows["melody"].append(_rms(filtered("mid", _MID, mix)))
    got = {name: (np.concatenate(parts_) if parts_ else np.zeros(0, np.float32))[:total]
           for name, parts_ in rows.items()}
    level = got["level"]
    sounding = level[level > 1e-4]
    ref = float(np.percentile(sounding, 95)) if len(sounding) else 1e-3
    if parts is not None:
        # A drum hit against the song's own drums; the parts by their share of the mix.
        kick = _scaled(got["kick"], max(float(np.percentile(got["kick"], 98)), 0.05 * ref)) ** 1.5
        snare = _scaled(got["snare"], max(float(np.percentile(got["snare"], 98)), 0.03 * ref))
        bass = np.clip(np.sqrt(_scaled(got["bass"], ref)) * 1.1, 0, 1)
        vocals = np.clip(np.sqrt(_scaled(got["vocals"], ref)) * 1.2, 0, 1)
        melody = np.clip(np.sqrt(_scaled(got["melody"], ref)) * 1.1, 0, 1)
    else:
        # No stems: the kick is the low end's punch, the bass its body.
        low = got["kick"]
        body = _smooth(low, 12)
        punch = np.maximum(0.0, low - 0.8 * body)
        kick = _scaled(punch, max(float(np.percentile(punch, 98)), 0.02 * ref)) ** 1.3
        bass = _scaled(body, max(float(np.percentile(body, 95)), 0.05 * ref))
        snare = _scaled(got["snare"], max(float(np.percentile(got["snare"], 98)), 0.03 * ref))
        vocals = np.zeros_like(level)
        melody = np.clip(np.sqrt(_scaled(got["melody"], ref)) * 1.1, 0, 1)
    out = np.stack([kick, snare, bass, vocals, melody, _scaled(level, ref)], axis=1)
    return np.round(np.nan_to_num(out) * 255).astype(np.uint8)


def _db_gain(value):
    return 0.0 if value <= KILL_DB + 0.5 else 10 ** (min(value, 6.0) / 20)


def deck_frame(engine, deck, ahead):
    """What one deck adds to the light `ahead` seconds from now (6 floats), or None."""
    env = getattr(deck, "light", None)
    if env is None or not deck.loaded or deck.rate_now == 0.0:
        return None
    gain = engine._channel_gain(deck) * engine.master_volume
    if gain < 0.01:
        return None
    i = int((deck.pos + deck.rate_now * ahead * SR) // BLOCK)
    if not 0 <= i < len(env):
        return None
    kick, snare, bass, vocals, melody, level = env[i] / 255.0
    g = deck.stem_now if deck.stems is not None and deck.use_stems else {}
    drums_g, bass_g, vocals_g, other_g = (g.get(name, 1.0) for name in ("drums", "bass", "vocals", "other"))
    lows, mids, highs = (_db_gain(deck.eq.get(band, 0.0)) for band in ("low", "mid", "hi"))
    if deck.color_fx == "filter" and abs(deck.color) >= 0.03:
        if deck.color > 0:          # high-pass: the bottom goes first
            lows *= (1 - deck.color) ** 2
            mids *= 1 - 0.4 * deck.color
        else:                       # low-pass: the top goes first
            highs *= (1 + deck.color) ** 2
            mids *= 1 + 0.6 * deck.color
    parts = max(drums_g, bass_g, vocals_g, other_g)
    return (kick * drums_g * lows * gain, snare * drums_g * highs * gain, bass * bass_g * lows * gain,
            vocals * vocals_g * mids * gain, melody * other_g * mids * gain,
            level * parts * (lows + mids + highs) / 3 * gain)


def frame(engine, ahead=LOOKAHEAD):
    """The room's light `ahead` seconds from now: (6 floats 0..1, beat, bpm, stems)."""
    values = [0.0] * 6
    stems_on = False
    for deck in engine.decks.values():
        got = deck_frame(engine, deck, ahead)
        if got is None:
            continue
        stems_on = stems_on or deck.stems is not None
        for i, v in enumerate(got):
            if v > values[i]:
                values[i] = v
    beat = bpm = None
    m = engine.master()
    if m is not None and m.loaded and m.playing and m.bpm:
        beat = m.beat_at(m.seconds + m.rate_now * ahead)
        bpm = m.live_bpm()
    return values, beat, bpm, stems_on


class Feed:
    """Light frames on their way to the room, and the moments in them.

    The audio side adds a frame per block (add) and notes moments (event); the
    publisher collects them every SEND_EVERY seconds (take) as wire messages.
    """

    #: How far a frame's wall-clock time may stray from the audio's own clock
    #: (one block after the last) before it is taken as a stall or a jump.
    RESYNC_MS = 60

    def __init__(self):
        self.frames = []           # [t ms, bytes(6)], None between runs
        self.clock = None          # t ms of the last frame added
        self.beat = None           # (beat, bpm) at the first frame waiting
        self.stems = False
        self.events = []           # {"k", "at", ...}
        self.info = None           # what the booth is doing, sent now and then
        self.info_sent = (None, 0.0)
        # Somebody has the club open (the publisher hears so from the hub): until
        # then the audio side skips the light work altogether.
        self.wanted = True

    def add(self, t_ms, values, beat=None, bpm=None, stems=False):
        # Frames keep the audio's clock, one block apart, the way a listener
        # plays the blocks; the sender's own jitter is not the music's.
        expected = None if self.clock is None else self.clock + FRAME_MS
        if expected is not None and abs(t_ms - expected) <= self.RESYNC_MS:
            t_ms = expected
        elif self.frames and self.frames[-1] is not None:
            # The loop stalled (or the source jumped): the frames so far go on
            # their own, and a new run starts at this one.
            self.frames.append(None)
        self.clock = t_ms
        if not self.frames or self.frames[-1] is None:
            self.beat = None
        if self.beat is None and beat is not None and bpm:
            self.beat = (beat - len(self._run()) * bpm / 60 * FRAME_MS / 1000, bpm)
        self.stems = self.stems or stems
        self.frames.append([t_ms, bytes(int(max(0.0, min(1.0, v)) * 255 + 0.5) for v in values)])
        del self.frames[:-200]     # nobody took them for 4 s: keep only the last

    def _run(self):
        run = []
        for item in reversed(self.frames):
            if item is None:
                break
            run.append(item)
        return run

    def event(self, kind, at_ms, **extra):
        self.events.append({"k": kind, "at": int(at_ms), **extra})
        del self.events[:-8]

    def take(self, now=None):
        """The wire messages for what gathered: a list of dicts (often one)."""
        now = time.time() if now is None else now
        out = []
        run = []
        for item in self.frames + [None]:
            if item is not None:
                run.append(item)
                continue
            if run:
                out.append({"t": "club", "at": int(run[0][0]),
                            "f": base64.b64encode(b"".join(f for _, f in run)).decode()})
            run = []
        if out:
            beat = self.beat
            last = out[-1]
            if beat is not None:
                last["b"], last["bpm"] = round(beat[0], 3), round(beat[1], 2)
            last["stems"] = self.stems
        if self.events:
            if not out:
                out.append({"t": "club", "at": int(now * 1000), "f": ""})
            out[-1]["ev"] = self.events
        info, sent_at = self.info_sent
        if self.info is not None and (self.info != info or now - sent_at > 1.0):
            if not out:
                out.append({"t": "club", "at": int(now * 1000), "f": ""})
            out[-1]["info"] = self.info
            self.info_sent = (self.info, now)
        self.frames, self.beat, self.stems, self.events = [], None, False, []
        return out


def wire(message):
    return json.dumps(message, separators=(",", ":"))


class PlayerMeter:
    """Light for the room's ordinary player, which has no stems: the mix's bands,
    measured on the decoded block itself, each against its own recent peak."""

    PEAK_DECAY = 0.9993            # per frame: a peak halves in about 20 s

    def __init__(self):
        self.window = np.hanning(BLOCK).astype(np.float32)
        self.reset()

    def reset(self):
        self.peaks = np.full(6, 1e-3)
        self.body = 0.0

    def frame(self, pcm):
        x = np.frombuffer(pcm, "<i2").reshape(-1, 2).astype(np.float32).mean(axis=1) / 32768.0
        if len(x) != BLOCK:
            return [0.0] * 6
        power = np.abs(np.fft.rfft(x * self.window)) ** 2   # bins 50 Hz wide
        low = math.sqrt(power[1:4].sum())
        mid = math.sqrt(power[6:61].sum())
        high = math.sqrt(power[36:161].sum())
        level = math.sqrt(float(np.mean(x * x)))
        self.body = self.body * 0.9 + low * 0.1
        punch = max(0.0, low - 0.8 * self.body)
        raw = np.array([punch, high, self.body, 0.0, mid, level])
        self.peaks = np.maximum(raw, self.peaks * self.PEAK_DECAY)
        values = raw / np.maximum(self.peaks, 1e-4)
        values[0] **= 1.3
        return [float(v) for v in values]


def player_beat(analysis, position):
    """(beat, bpm) of the ordinary player at `position` seconds, from AutoMix's analysis."""
    if not analysis:
        return None, None
    bpm = analysis.get("bpm")
    if not bpm or (analysis.get("bpm_confidence") or 0) < 0.15:
        return None, None
    bpm = float(bpm)
    return (position - float(analysis.get("first_beat") or 0.0)) / (60.0 / bpm), bpm


# --------------------------------------------------------------------------
# The DJ's moves
# --------------------------------------------------------------------------

MOVES = {
    "drop": "Build and drop",
    "acapella": "Acapella",
    "beatbreak": "Beat break",
    "stutter": "Stutter",
    "echo": "Echo throw",
}


def _beat_ms(m, beat, now_beat):
    """Wall-clock ms when deck m reaches `beat`."""
    seconds = (beat - now_beat) * m.beat_seconds / max(0.05, abs(m.base_rate()))
    return time.time() * 1000 + seconds * 1000


def perform(session, move):
    """Play one of MOVES on the deck that leads, from its next bar. Returns a notice."""
    engine = session.engine
    if move not in MOVES:
        raise ValueError("Unknown move.")
    with engine.lock:
        tr = engine.transition
        if tr is not None and tr.started and not getattr(tr, "done", False):
            raise ValueError("A mix is under way: try that once it's done.")
        m = engine.master()
        if m is None or not m.playing or not m.bpm:
            raise ValueError("Nothing with a beat is playing.")
        now_beat = m.beat_at(m.seconds)
        busy = getattr(session, "club_busy_until", None)
        if busy and busy[0] == (m.name, m.wave_id) and now_beat < busy[1]:
            raise ValueError("Let the last move finish first.")
        bar = math.ceil(now_beat / 4 + 1e-6) * 4
        if bar - now_beat < 0.75:
            bar += 4                       # too close to land on: the bar after
        saved = dict(m.stem_gain)
        feed = getattr(engine, "light", None)

        def gains(values):
            def apply():
                m.stem_gain.update(values)
            return apply

        def restore():
            m.stem_gain.update(saved)

        def fx(kind, beat_index, level):
            def on():
                engine.fx.set_on(False, engine.clock()[0])
                engine.fx.type, engine.fx.beat, engine.fx.level, engine.fx.target = kind, beat_index, level, m.name
                engine.fx.set_on(True, engine.clock()[0])

            def off():
                engine.fx.set_on(False, engine.clock()[0])
            return on, off

        def sample(name):
            return lambda: engine.play_sample(name)

        def nothing():
            return None

        stems_ok = m.stems is not None and m.use_stems
        if move in ("acapella", "beatbreak") and not stems_ok:
            raise ValueError("That one needs stems on this song; they aren't ready yet.")
        if move == "drop":
            build = 8
            start, drop = bar, bar + build
            # The bass leaves for the build, the top end opens up, the last beats
            # roll, one beat of silence, and everything slams back with an 808.
            if stems_ok:
                engine.schedule(m.name, start, drop - 1, gains({"bass": 0.0}), nothing)
                engine.schedule(m.name, drop - 1, drop, gains({name: 0.0 for name in ("drums", "bass", "vocals", "other")}), restore)
            for step in range(build - 1):
                amount = 0.12 + 0.6 * (step + 1) / (build - 1)

                def sweep(amount=amount):
                    m.color_fx = "filter"
                    m.color = amount
                engine.schedule(m.name, start + step, start + step + 1, sweep, nothing)
            engine.schedule(m.name, drop - 1, drop, nothing, lambda: setattr(m, "color", 0.0))
            on, off = fx("roll", 2, 0.85)          # half-beat roll ...
            engine.schedule(m.name, drop - 3, drop - 2, on, off)
            on, off = fx("roll", 1, 0.95)          # ... then quarter beats
            engine.schedule(m.name, drop - 2, drop - 1, on, off)
            riser_beats = max(1.0, min(float(build), 4.0 / (m.beat_seconds / max(0.05, m.base_rate()))))
            engine.schedule(m.name, drop - riser_beats, drop - riser_beats + 0.5, sample("riser"), nothing)
            engine.schedule(m.name, drop, drop + 0.5, sample("boom"), nothing)
            if not stems_ok:
                # Without stems, the silence is the fader's.
                fader = m.fader
                engine.schedule(m.name, drop - 0.5, drop, lambda: setattr(m, "fader", 0.0),
                                lambda: setattr(m, "fader", fader))
            end = drop + 1
            if feed is not None:
                feed.event("build", _beat_ms(m, start, now_beat), until=int(_beat_ms(m, drop, now_beat)))
                feed.event("drop", _beat_ms(m, drop, now_beat))
        elif move == "acapella":
            length = 8
            engine.schedule(m.name, bar, bar + length, gains({"drums": 0.0, "bass": 0.0, "other": 0.0}), restore)
            engine.schedule(m.name, bar + length, bar + length + 0.5, sample("boom"), nothing)
            end = bar + length
            if feed is not None:
                feed.event("breakdown", _beat_ms(m, bar, now_beat), until=int(_beat_ms(m, end, now_beat)))
                feed.event("drop", _beat_ms(m, end, now_beat))
        elif move == "beatbreak":
            length = 8
            engine.schedule(m.name, bar, bar + length, gains({"bass": 0.0, "vocals": 0.0, "other": 0.0}), restore)
            end = bar + length
            if feed is not None:
                feed.event("beatbreak", _beat_ms(m, bar, now_beat), until=int(_beat_ms(m, end, now_beat)))
                feed.event("drop", _beat_ms(m, end, now_beat))
        elif move == "stutter":
            # On the next beat, not the next bar: a stutter is a quick one.
            beat = math.ceil(now_beat + 0.25)
            on, off = fx("roll", 1, 1.0)
            engine.schedule(m.name, beat, beat + 2, on, off)
            end = beat + 2
            if feed is not None:
                feed.event("stutter", _beat_ms(m, beat, now_beat), until=int(_beat_ms(m, end, now_beat)))
        else:                              # echo
            beat = math.ceil(now_beat + 0.25)
            on, off = fx("echo", 3, 0.75)          # 3/4-beat echo, thrown off the last word
            engine.schedule(m.name, beat, beat + 1, on, off)
            end = beat + 1
            if feed is not None:
                feed.event("echo", _beat_ms(m, beat, now_beat))
        session.club_busy_until = ((m.name, m.wave_id), end)
    return f"{MOVES[move]} on the next {'bar' if move in ('drop', 'acapella', 'beatbreak') else 'beat'}"


def night_entries(entries, playing=(), count=30, rng=None):
    """The stemmed songs to queue for a night in the club: a shuffled handful,
    leaving out what the room has queued already."""
    rng = rng or random.Random()
    taken = {str(item).lower() for item in playing if item}
    pool = [entry for entry in entries
            if entry.get("url") and entry["url"].lower() not in taken and (entry.get("title") or "").lower() not in taken]
    rng.shuffle(pool)
    return pool[:count]
