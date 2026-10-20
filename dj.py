"""Live two-deck DJ booth for Discord voice and Huddle rooms.

AutoMix renders a finished transition ahead of time. The DJ booth is
different: two decks play at once and every control changes the audio live,
the way a DJ controller does. The engine is pull-based. Whatever carries the
room's audio (discord.py's player thread, or Huddle's WebRTC track) asks for
one 20 ms block at a time, and the engine renders it from:

    deck A ─ trim ─ 3-band EQ ─ color FX ─ fader ┐
                                                  ├─ crossfader ─ beat FX ─ master ─ sampler ─ limiter
    deck B ─ trim ─ 3-band EQ ─ color FX ─ fader ┘

Each deck holds its whole track decoded in memory (s16 stereo, 48 kHz), so
cues, loops, beat jumps, reverse and scratching are just moves of a playhead.
Tempo changes are varispeed (pitch moves with tempo, like vinyl).

Controls arrive from the web booth (webui.py) and the /dj slash command as
small ops, e.g. {"op": "set", "deck": "A", "param": "eq_low", "value": -26}.

Auto DJ is the "a bit automatic" part. It pulls the next song from the room's
queue into the idle deck, picks a mix-out point on a phrase boundary, then
performs a transition (blend, bass swap, echo out, filter, ...) by moving the
same faders and knobs a person would, which the booth shows as it happens.
"""

import asyncio
import atexit
import base64
import bisect
import json
import logging
import math
import os
import random
import subprocess
import sys
import threading
import time

import numpy as np
from scipy import signal

import mixer
import realdj
import samples
import stems

logger = logging.getLogger("MusicBot.DJ")

SR = 48_000
BLOCK = 960                      # 20 ms, one Discord/Opus frame
FRAME_BYTES = BLOCK * 2 * 2      # s16le stereo
MAX_TRACK_SECONDS = 15 * 60
WAVE_RATE = 20                   # waveform bins per second of audio
KILL_DB = -26.0                  # EQ knob fully left = band killed
DECKS = ("A", "B")
# Real DJ's third deck (3-songs mode): a song layered over the other two for a
# phrase. Not one of DECKS: the booth, Auto DJ and loading never use it.
LAYER = "C"

FX_TYPES = ("echo", "delay", "reverb", "flanger", "roll", "trans")
FX_BEATS = (1 / 8, 1 / 4, 1 / 2, 3 / 4, 1, 2, 4, 8)
COLOR_FX = ("filter", "space", "dub_echo", "crush", "noise")
LOOP_BEATS = (1 / 4, 1 / 2, 1, 2, 4, 8, 16, 32)
STYLES = {
    # style: (label, default length in beats)
    "auto": ("Auto", 32),
    "blend": ("Blend + bass swap", 32),
    "fade": ("Smooth fade", 16),
    "filter": ("Filter sweep", 16),
    "echo": ("Echo out", 8),
    "cut": ("Cut on the one", 1),
    "brake": ("Brake stop", 4),
    "spinback": ("Spinback", 4),
    "stem_swap": ("Stem swap", 32),
    "acapella": ("Acapella over", 32),
    "drum_bridge": ("Drum bridge", 32),
    "mashup": ("Mashup", 64),
    "loop_roll": ("Loop roll drop", 8),
    "guitar_ride": ("Guitar ride", 32),
}
STEM_STYLES = ("stem_swap", "acapella", "drum_bridge", "mashup", "guitar_ride")
# Styles where both songs play over each other for a while; Auto DJ tries to
# place these where the outgoing song has no vocal.
OVERLAP_STYLES = ("blend", "fade", "filter", "stem_swap", "drum_bridge", "guitar_ride")
# A routine is a transition scripted as keyframes (Real DJ's back and forth,
# hook teases): it isn't in STYLES because only a plan can supply the script.
ROUTINE = "routine"
SCRIPT_CONTROLS = ("xf",) + tuple(f"{side}.{stem}" for side in ("out", "in") for stem in stems.CONTROLS)
SCRIPT_START = dict({"xf": 0.0}, **{f"out.{stem}": 1.0 for stem in stems.CONTROLS},
                    **{f"in.{stem}": 0.0 for stem in stems.CONTROLS})
DATA_DIR = os.getenv("DATA_DIR") or os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(DATA_DIR, "dj_settings.json")
# Running booths, so a restart can bring them back (see DJSession.save_state).
STATE_FILE = os.path.join(DATA_DIR, "dj_sessions.json")
RESTORE_MAX_AGE = 15 * 60          # seconds: an older snapshot is a set nobody's waiting for
SAMPLES = ("horn", "siren", "rewind", "laser", "boom", "clap", "riser", "scratch")


def _db_gain(value):
    return 0.0 if value <= KILL_DB + 0.5 else 10 ** (value / 20)


def _clamp(value, low, high):
    return max(low, min(high, value))


def _smooth(x):
    x = _clamp(x, 0.0, 1.0)
    return x * x * (3 - 2 * x)


def _lr4(freq, kind):
    """Linkwitz-Riley 4th order = two identical 2nd-order Butterworths."""
    sos = signal.butter(2, freq, kind, fs=SR, output="sos")
    return np.vstack([sos, sos])


SOS_LOW = _lr4(250, "lowpass")
SOS_HIGH = _lr4(2500, "highpass")


# --------------------------------------------------------------------------
# DSP building blocks. Every delay is at least one block long, so a whole
# block can be processed at once with numpy even with feedback.
# --------------------------------------------------------------------------

class DelayLine:
    def __init__(self, seconds):
        self.size = int(SR * seconds) + BLOCK * 2
        self.buf = np.zeros((self.size, 2), np.float32)
        self.write_at = 0

    def read(self, delay, n, spread=0):
        base = self.write_at + np.arange(n)
        left = self.buf[(base - int(delay)) % self.size, 0]
        right = self.buf[(base - int(delay) - spread) % self.size, 1]
        return np.stack([left, right], axis=1)

    def write(self, x):
        index = (self.write_at + np.arange(len(x))) % self.size
        self.buf[index] = x
        self.write_at = (self.write_at + len(x)) % self.size


class Echo:
    MAX_SECONDS = 4.0

    def __init__(self):
        self.line = DelayLine(self.MAX_SECONDS)

    def process(self, x, delay_seconds, feedback, feeding):
        delay = _clamp(int(delay_seconds * SR), BLOCK, int(self.MAX_SECONDS * SR))
        wet = self.line.read(delay, len(x))
        self.line.write((x if feeding else 0.0) + wet * feedback)
        return wet


class Reverb:
    """Freeverb-style: parallel damped combs into two allpasses."""
    COMBS = (1687, 1601, 1867, 1777)
    ALLPASS = (1051, 1327)

    def __init__(self):
        self.combs = [DelayLine(0.1) for _ in self.COMBS]
        self.damp = [np.zeros((1, 2)) for _ in self.COMBS]
        self.ap_in = [DelayLine(0.1) for _ in self.ALLPASS]
        self.ap_out = [DelayLine(0.1) for _ in self.ALLPASS]

    def process(self, x, size, feeding, damping=0.35):
        feed = x * 0.12 if feeding else np.zeros_like(x)
        feedback = 0.72 + 0.26 * _clamp(size, 0, 1)
        acc = np.zeros_like(x)
        for index, (line, delay) in enumerate(zip(self.combs, self.COMBS)):
            out = line.read(delay, len(x), spread=23)
            damped, self.damp[index] = signal.lfilter(
                [1 - damping], [1, -damping], out, axis=0, zi=self.damp[index])
            line.write(feed + damped * feedback)
            acc += out
        gain = 0.5
        for line_in, line_out, delay in zip(self.ap_in, self.ap_out, self.ALLPASS):
            out = -gain * acc + line_in.read(delay, len(x)) + gain * line_out.read(delay, len(x))
            line_in.write(acc)
            line_out.write(out)
            acc = out
        return acc.astype(np.float32)


class Flanger:
    HISTORY = 2048

    def __init__(self):
        self.history = np.zeros((self.HISTORY, 2), np.float32)
        self.phase = 0.0

    def process(self, x, period_seconds):
        n = len(x)
        buf = np.concatenate([self.history, x])
        step = 1.0 / max(1.0, period_seconds * SR)
        t = self.phase + np.arange(n) * step
        delay = (0.0012 + 0.0040 * (0.5 - 0.5 * np.cos(2 * np.pi * t))) * SR
        position = self.HISTORY + np.arange(n) - delay
        i0 = np.floor(position).astype(int)
        frac = (position - i0)[:, None]
        delayed = buf[i0] * (1 - frac) + buf[i0 + 1] * frac
        self.history = buf[-self.HISTORY:]
        self.phase = (self.phase + n * step) % 1.0
        return delayed.astype(np.float32)


class Roll:
    """Captures a slice when engaged and repeats it; the track runs on beneath."""

    def __init__(self):
        self.buf = None
        self.count = 0

    def engage(self, seconds):
        length = max(BLOCK // 4, int(seconds * SR))
        self.buf = np.zeros((length, 2), np.float32)
        self.count = 0

    def process(self, x):
        if self.buf is None:
            return x
        length = len(self.buf)
        g = self.count + np.arange(len(x))
        capture = g < length
        if capture.any():
            self.buf[g[capture]] = x[capture]
        self.count += len(x)
        return np.where(capture[:, None], x, self.buf[g % length])


class BeatFX:
    """One Beat FX unit, like the XDJ's: type, beat, level/depth, on, target."""

    def __init__(self):
        self.type = "echo"
        self.beat = 2               # index into FX_BEATS (1/2 beat)
        self.level = 0.5
        self.on = False
        self.target = "master"      # master | A | B
        self.echo = Echo()
        self.reverb = Reverb()
        self.flanger = Flanger()
        self.roll = Roll()
        self.gate_zi = np.zeros(1)
        self.tail = 0               # blocks left of echo/reverb spill-over

    def set_on(self, on, beat_seconds):
        if on and not self.on and self.type == "roll":
            self.roll.engage(FX_BEATS[self.beat] * beat_seconds)
        if not on and self.on:
            self.tail = 250 if self.type in ("echo", "delay", "reverb") else 0
            self.roll.buf = None
        self.on = on

    def active(self):
        return self.on or self.tail > 0

    def process(self, x, beat_seconds, beat_pos, beats_per_sample):
        if not self.active():
            return x
        if not self.on:
            self.tail -= 1
        frac = FX_BEATS[self.beat]
        kind = self.type
        if kind in ("echo", "delay"):
            feedback = 0.62 if kind == "echo" else 0.12
            wet = self.echo.process(x, frac * beat_seconds, feedback, self.on)
            return x + wet * self.level
        if kind == "reverb":
            wet = self.reverb.process(x, 0.4 + 0.6 * self.level, self.on)
            return x + wet * (0.4 + 1.6 * self.level)
        if not self.on:
            return x
        if kind == "flanger":
            wet = self.flanger.process(x, frac * 4 * beat_seconds)
            return x * (1 - self.level / 2) + wet * (self.level / 2)
        if kind == "roll":
            rolled = self.roll.process(x)
            return x * (1 - self.level) + rolled * self.level
        if kind == "trans":
            beats = beat_pos + np.arange(len(x)) * beats_per_sample
            open_ = ((beats / frac) % 1.0) < 0.5
            raw = np.where(open_, 1.0, 1.0 - self.level)
            pole = math.exp(-1.0 / (0.003 * SR))
            gain, self.gate_zi = signal.lfilter([1 - pole], [1, -pole], raw, zi=self.gate_zi)
            return x * gain[:, None].astype(np.float32)
        return x


# --------------------------------------------------------------------------
# Sampler: one-shots synthesised once at import, so there are no files to ship
# --------------------------------------------------------------------------

def _synth_samples():
    rng = np.random.default_rng(7)

    def env(n, attack=0.005, release=0.05):
        e = np.ones(n)
        a = max(1, int(attack * SR))
        r = max(1, int(release * SR))
        e[:a] = np.linspace(0, 1, a)
        e[-r:] *= np.linspace(1, 0, r)
        return e

    def saw(freq, n, phase=0.0):
        t = np.arange(n) / SR
        return 2 * ((t * freq + phase) % 1.0) - 1

    def stereo(mono, gain=0.6):
        mono = np.tanh(mono * 1.2) * gain
        return np.stack([mono, mono], axis=1).astype(np.float32)

    out = {}
    # Air horn: stacked detuned saws, "bap bap baaaap".
    parts = []
    for length in (0.13, 0.13, 0.13, 0.7):
        n = int(length * SR)
        tone = sum(saw(f, n, i * 0.13) for i, f in enumerate((440, 443, 554, 557, 659)))
        parts.append(tone / 5 * env(n, 0.004, 0.03))
        parts.append(np.zeros(int(0.04 * SR)))
    horn = np.concatenate(parts)
    horn = signal.sosfilt(signal.butter(2, [300, 4500], "bandpass", fs=SR, output="sos"), horn)
    out["horn"] = stereo(horn * 2.2)
    # Dub siren: square wave wobbling between two pitches.
    n = int(1.8 * SR)
    t = np.arange(n) / SR
    freq = np.where(np.sin(2 * np.pi * 3.2 * t) >= 0, 1150, 700)
    siren = np.sign(np.sin(2 * np.pi * np.cumsum(freq) / SR)) * 0.5
    out["siren"] = stereo(siren * env(n, 0.01, 0.3) * 0.7)
    # Rewind: noisy saw sweeping down fast.
    n = int(0.9 * SR)
    sweep = np.geomspace(2400, 90, n)
    rewind = 2 * ((np.cumsum(sweep) / SR) % 1.0) - 1
    rewind = rewind * 0.6 + rng.normal(0, 0.15, n)
    out["rewind"] = stereo(rewind * env(n, 0.01, 0.2) * 0.8)
    # Laser: three quick falling zaps.
    zaps = []
    for _ in range(3):
        n = int(0.16 * SR)
        f = np.geomspace(3200, 180, n)
        zaps.append(np.sin(2 * np.pi * np.cumsum(f) / SR) * env(n, 0.002, 0.04))
    out["laser"] = stereo(np.concatenate(zaps) * 0.9)
    # 808 boom: pitch-dropping sine with a long tail.
    n = int(1.6 * SR)
    t = np.arange(n) / SR
    f = 45 + 110 * np.exp(-t / 0.04)
    boom = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t / 0.6)
    out["boom"] = stereo(boom * 1.4, 0.8)
    # Clap: three filtered noise bursts and a tail.
    n = int(0.45 * SR)
    t = np.arange(n) / SR
    noise = rng.normal(0, 1, n)
    shape = np.zeros(n)
    for offset in (0.0, 0.011, 0.022):
        s = int(offset * SR)
        shape[s:] += np.exp(-(t[: n - s]) / (0.006 if offset < 0.02 else 0.12))
    clap = signal.sosfilt(signal.butter(2, [900, 3200], "bandpass", fs=SR, output="sos"), noise * shape)
    out["clap"] = stereo(clap * 1.6)
    # Riser: white noise opening up over four seconds, plus a rising tone.
    n = int(4.0 * SR)
    t = np.arange(n) / SR
    noise = rng.normal(0, 0.4, n)
    riser = np.zeros(n)
    zi = None
    chunk = SR // 20
    for start in range(0, n, chunk):
        cutoff = 300 * (40 ** (start / n))
        sos = signal.butter(2, min(cutoff, 20000), "lowpass", fs=SR, output="sos")
        if zi is None:
            zi = np.zeros((sos.shape[0], 2))
        riser[start:start + chunk], zi = signal.sosfilt(sos, noise[start:start + chunk], zi=zi)
    tone = np.sin(2 * np.pi * np.cumsum(np.geomspace(220, 1760, n)) / SR) * 0.15
    out["riser"] = stereo((riser + tone) * (t / t[-1]) ** 1.5)
    # Scratch: saw with a fast back-and-forth pitch, "wicka-wicka".
    n = int(0.6 * SR)
    t = np.arange(n) / SR
    speed = np.sin(2 * np.pi * 6.5 * t)
    f = 180 + 520 * np.abs(speed)
    scratch = 2 * ((np.cumsum(f) / SR) % 1.0) - 1
    scratch = scratch * np.abs(speed) + rng.normal(0, 0.08, n) * np.abs(speed)
    out["scratch"] = stereo(scratch * env(n, 0.005, 0.05) * 0.8)
    return out


_SAMPLE_BANK = None
_SAMPLE_THREAD = None
_SAMPLE_LOCK = threading.Lock()


def _build_sample_bank():
    global _SAMPLE_BANK
    try:
        bank = samples.build()
    except Exception:
        logger.warning("Could not build the sampler sounds; using the simple ones", exc_info=True)
        bank = _synth_samples()
    _SAMPLE_BANK = bank


def warm_samples():
    """Start building the sampler sounds in the background (once; ~2 s)."""
    global _SAMPLE_THREAD
    with _SAMPLE_LOCK:
        if _SAMPLE_BANK is None and _SAMPLE_THREAD is None:
            _SAMPLE_THREAD = threading.Thread(target=_build_sample_bank, name="dj-samples", daemon=True)
            _SAMPLE_THREAD.start()


def sample_bank(wait=False):
    """The sampler sounds (samples.py). While they are still being built this
    is {} so a hit is skipped rather than stalling the audio; wait=True blocks
    until they're ready (not for the audio thread)."""
    if _SAMPLE_BANK is None:
        warm_samples()
        if wait and _SAMPLE_THREAD is not None:
            _SAMPLE_THREAD.join()
    return _SAMPLE_BANK or {}


# --------------------------------------------------------------------------
# Loading: decode a whole track and analyse it
# --------------------------------------------------------------------------

def decode_track(source):
    """Whole track as int16 (n, 2) at 48 kHz."""
    command = ["ffmpeg", "-nostdin", "-loglevel", "error"]
    if str(source).startswith(("http://", "https://")):
        # A stalled stream fails after 20 s instead of hanging to the timeout.
        command += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "3",
                    "-rw_timeout", "20000000"]
    command += ["-i", str(source), "-vn", "-t", str(MAX_TRACK_SECONDS),
                "-f", "s16le", "-ac", "2", "-ar", str(SR), "pipe:1"]
    result = subprocess.run(command, capture_output=True, timeout=240)
    audio = np.frombuffer(result.stdout, dtype=np.int16)
    audio = audio[: len(audio) // 2 * 2].reshape(-1, 2)
    if len(audio) < SR * 3:
        detail = result.stderr.decode(errors="replace").strip()[-200:]
        raise RuntimeError(detail or "The track has no audio.")
    return audio


def _band_rms(x, hop, count):
    x = x[: hop * count].reshape(count, hop)
    return np.sqrt((x.astype(np.float64) ** 2).mean(axis=1))


def analyze_buffer(audio, keys):
    """(analysis, waveform) for a decoded track, reusing the mixer's cache."""
    mono = audio.astype(np.float32).mean(axis=1) / 32768.0
    small = signal.resample_poly(mono, 147, 640).astype(np.float32)  # 48k -> 11025
    meta = mixer.cache.get(*keys) if keys else None
    if not meta:
        meta = mixer.analyze_samples(small)
        if keys:
            mixer.cache.put(meta, *keys)
    # Three-band waveform like rekordbox: low blue, mid amber, high white.
    rate = mixer.ANALYSIS_SR
    hop = rate // WAVE_RATE
    count = max(1, len(small) // hop)
    low = signal.sosfilt(signal.butter(2, 200, "lowpass", fs=rate, output="sos"), small)
    high = signal.sosfilt(signal.butter(2, 2200, "highpass", fs=rate, output="sos"), small)
    mid = small - low - high
    bands = [_band_rms(band, hop, count) for band in (low, mid, high)]
    peak = max(1e-6, max(float(np.percentile(b, 99.5)) for b in bands))
    wave = {}
    for name, band, boost in zip(("low", "mid", "high"), bands, (1.0, 1.3, 2.2)):
        scaled = np.clip(np.sqrt(band / peak) * 255 * boost / 1.25, 0, 255).astype(np.uint8)
        wave[name] = base64.b64encode(scaled.tobytes()).decode()
    wave["rate"] = WAVE_RATE
    return meta, wave


# --------------------------------------------------------------------------
# Deck
# --------------------------------------------------------------------------

class Deck:
    def __init__(self, name):
        self.name = name
        self.xf = name              # crossfader assign: A | THRU | B
        self.audio = None
        self.info = {}
        self.meta = {}
        self.wave = None
        self.wave_id = 0
        self.loading = None
        self.error = None
        self.load_token = 0
        # Stems: int16 (3, n, 2) drums/bass/vocals from stems.py, or None.
        self.stems = None
        # Guitar and piano, int16 (2, n, 2), for songs that have them (stems.extras); else None
        # and they are part of "other".
        self.extra = None
        self.stems_status = None    # None | separating | ready | failed
        self.stem_gain = stems.StemGains()
        self.stem_now = dict(self.stem_gain)
        self.use_stems = True       # the room's STEMS switch
        self.karaoke = False        # the room's karaoke mode: vocals stay out
        self.reset_track_state()
        # Mixer channel
        self.trim = 0.0
        self.eq = {"hi": 0.0, "mid": 0.0, "low": 0.0}
        self.color = 0.0
        self.color_fx = "filter"
        self.fader = 1.0
        self.level = 0.0
        self.eq_low_zi = np.zeros((SOS_LOW.shape[0], 2, 2))
        self.eq_high_zi = np.zeros((SOS_HIGH.shape[0], 2, 2))
        self.filter_key = None
        self.filter_sos = None
        self.filter_zi = None
        self.color_echo = None
        self.color_reverb = None
        self.crush_hold = np.zeros(2, np.float32)
        self.rng = np.random.default_rng()
        # Performance settings survive loading a new track
        self.range = 8
        self.quantize = True
        self.vinyl = True
        self.slip = False
        self.sync = False
        self.ai = False             # set by the session's experimental AI switch

    def reset_track_state(self):
        self.pos = 0.0              # playhead, in samples (float)
        self.shadow = 0.0           # slip-mode playhead
        self.playing = False
        self.rate_now = 0.0         # rate at the end of the last block
        self.pitch = 0.0            # percent
        self.cue = 0.0
        self.hotcues = [None] * 8
        self.loop_in = None
        self.loop_out = None
        self.loop_on = False
        self.reverse = False
        self.bend = 0.0
        self.scratch = None
        self.scratch_at = 0.0
        self.brake = None
        self.cue_preview = False
        self.ended = False
        self.gesture = False        # a scripted hand on the record (Gesture)
        self.phase_snap = False
        self.nudge = 0.0            # sync's phase-hold, a tiny rate offset

    # ---- analysis helpers (track time, seconds) ----

    @property
    def loaded(self):
        return self.audio is not None

    @property
    def duration(self):
        return len(self.audio) / SR if self.loaded else 0.0

    @property
    def seconds(self):
        return self.pos / SR

    @property
    def bpm(self):
        bpm = self.meta.get("bpm")
        if not bpm or (self.meta.get("bpm_confidence") or 0) < 0.15:
            return None
        return float(bpm) * self.meta.get("bpm_scale", 1.0)

    @property
    def beat_seconds(self):
        return 60.0 / self.bpm if self.bpm else 0.5

    @property
    def first_beat(self):
        return float(self.meta.get("first_beat") or 0.0)

    def base_rate(self):
        return 1.0 + self.pitch / 100.0

    def live_bpm(self):
        return self.bpm * self.base_rate() if self.bpm else None

    def beat_at(self, seconds):
        return (seconds - self.first_beat) / self.beat_seconds

    def time_of_beat(self, beat):
        return self.first_beat + beat * self.beat_seconds

    def snap(self, seconds, force=False):
        if not (self.quantize or force) or not self.bpm:
            return seconds
        return self.time_of_beat(round(self.beat_at(seconds)))

    def music_end(self):
        return float(self.meta.get("music_end") or self.duration) if self.loaded else 0.0

    # ---- transport ----

    def seek(self, seconds):
        seconds = _clamp(seconds, 0.0, max(0.0, self.duration - 0.05))
        self.pos = self.shadow = seconds * SR
        self.ended = False

    def target_rate(self, now):
        if self.scratch is not None:
            # A scratch with no fresh movement is a held record.
            return self.scratch if now - self.scratch_at < 0.12 else 0.0
        if self.brake:
            kind, start_rate, length, elapsed = self.brake
            progress = elapsed / max(0.05, length)
            if progress >= 1:
                self.brake = None
                self.playing = False
                return 0.0
            if kind == "spinback":
                return -3.2 * (1 - progress) ** 2
            return start_rate * (1 - progress) ** 1.4
        if not self.playing:
            return 0.0
        rate = self.base_rate() + self.bend * 0.08 + self.nudge
        return -rate if self.reverse else rate

    def slipping(self):
        return self.slip and (self.loop_on or self.reverse or self.scratch is not None
                              or self.brake is not None)

    def read(self, n, now):
        """Next block of raw deck audio (float32, pre-mixer), or None if silent."""
        if not self.loaded:
            return None
        start, end = self.rate_now, self.target_rate(now)
        if self.scratch is not None and not self.gesture:
            end = start + (end - start) * 0.45  # a hand never stops instantly
        if self.brake:
            kind, rate0, length, elapsed = self.brake
            self.brake = (kind, rate0, length, elapsed + n / SR)
        self.rate_now = end
        if start == 0.0 and end == 0.0:
            return None
        rates = start + (end - start) * (np.arange(1, n + 1) / n)
        offsets = np.concatenate([[0.0], np.cumsum(rates[:-1])])
        positions = self.pos + offsets
        new_pos = self.pos + float(rates.sum())
        if self.loop_on and self.loop_out is not None and self.loop_in is not None and end > 0:
            loop_in, loop_out = self.loop_in * SR, self.loop_out * SR
            length = loop_out - loop_in
            if length > 64:
                wrap = positions >= loop_out
                positions[wrap] = loop_in + (positions[wrap] - loop_in) % length
                if new_pos >= loop_out:
                    new_pos = loop_in + (new_pos - loop_in) % length
        total = len(self.audio)
        valid = (positions >= 0) & (positions < total - 1)
        index = np.clip(positions.astype(np.int64), 0, total - 2)
        frac = (positions - index)[:, None].astype(np.float32)
        if max(abs(start), abs(end)) > FAST_RATE:
            # A scratch or spinback sweeps the record faster than it plays: each output
            # sample is the average of the stretch of record the needle crossed (like a
            # real stylus), instead of one point of it, which aliased into a metallic edge.
            edges = np.concatenate([[positions[0] - rates[0]], positions])
            out = self._apply_stems(_box_read(self.audio, edges), index, frac, n,
                                    fetch=lambda source, row: _box_read(source[row], edges))
        else:
            a = self.audio[index].astype(np.float32)
            b = self.audio[index + 1].astype(np.float32)
            out = self._apply_stems(a + (b - a) * frac, index, frac, n)
        out = out * (valid[:, None] / 32768.0)
        # Slip: the shadow playhead keeps the track's normal time.
        if self.slipping():
            if self.playing:
                self.shadow += self.base_rate() * n
        else:
            self.shadow = new_pos
        if new_pos >= total - 1:
            new_pos = total - 1
            self.playing = False
            self.rate_now = 0.0
            self.ended = True
        elif new_pos < 0:
            new_pos = 0.0
            if self.reverse:
                self.reverse = False
            if self.scratch is None:
                self.playing = False
                self.rate_now = 0.0
        self.pos = new_pos
        return out.astype(np.float32)

    @property
    def has_stems(self):
        return self.stems is not None and self.use_stems

    def _apply_stems(self, x, index, frac, n, fetch=None):
        """Mix x (the whole track) with per-stem gains, ramped over the block.

        Only drums, bass and vocals are stored (plus guitar and piano for some
        songs); other = mix - the stored ones. So
        out = mix * g_other + sum((g_stem - g_other) * stem), which is exactly
        the original track when every gain is equal.
        """
        if not self.use_stems:
            # Switched off: ramp every part back to full, then let go.
            self.stem_gain = stems.StemGains()
            if self.stems is not None and all(v == 1.0 for v in self.stem_now.values()):
                self.stems = None
                self.extra = None
                self.stems_status = None
        # A missing guitar / piano (a plain dict from older code) follows "other".
        start = {k: self.stem_now.get(k, self.stem_now.get("other", 1.0)) for k in stems.ALL_CONTROLS}
        end = {k: self.stem_gain.get(k, self.stem_gain.get("other", 1.0)) for k in stems.ALL_CONTROLS}
        if self.karaoke and end["vocals"] > stems.KARAOKE_GUIDE:
            end = dict(end, vocals=stems.KARAOKE_GUIDE)
        self.stem_now = dict(end)
        if self.stems is None or all(start[k] == 1.0 and end[k] == 1.0 for k in stems.ALL_CONTROLS):
            return x
        ramp = (np.arange(1, n + 1, dtype=np.float32) / n)[:, None]
        g_other = start["other"] + (end["other"] - start["other"]) * ramp
        out = x * g_other
        parts = [(self.stems, i, name) for i, name in enumerate(stems.NAMES)]
        if self.extra is not None:
            parts += [(self.extra, i, name) for i, name in enumerate(stems.EXTRA_NAMES)]
        for source, i, name in parts:
            g = start[name] + (end[name] - start[name]) * ramp - g_other
            if not g.any():
                continue
            if fetch is not None:
                out += fetch(source, i) * g
                continue
            a = source[i, index].astype(np.float32)
            b = source[i, index + 1].astype(np.float32)
            out += (a + (b - a) * frac) * g
        return out

    def stem_presence(self, name, t0, t1):
        """How much of the mix stem `name` makes up between t0 and t1 seconds (0..1)."""
        if self.stems is None:
            return None
        if name in stems.EXTRA_NAMES:
            if self.extra is None:
                return None
            source, row = self.extra, stems.EXTRA_NAMES.index(name)
        else:
            source, row = self.stems, stems.NAMES.index(name)
        i0, i1 = int(max(0, t0) * SR), int(max(0, t1) * SR)
        stem = source[row, i0:i1:16].astype(np.float32)
        mix = self.audio[i0:i1:16].astype(np.float32)
        if not len(mix):
            return None
        return float(np.sqrt((stem ** 2).mean()) / (np.sqrt((mix ** 2).mean()) + 1e-6))

    def end_slip(self):
        if self.slip and self.loaded:
            self.pos = self.shadow

    # ---- channel strip ----

    def strip(self, x, beat_seconds):
        gain = 10 ** (self.trim / 20)
        if gain != 1.0:
            x = x * gain
        low, self.eq_low_zi = signal.sosfilt(SOS_LOW, x, axis=0, zi=self.eq_low_zi)
        high, self.eq_high_zi = signal.sosfilt(SOS_HIGH, x, axis=0, zi=self.eq_high_zi)
        g_low, g_mid, g_hi = (_db_gain(self.eq["low"]), _db_gain(self.eq["mid"]), _db_gain(self.eq["hi"]))
        if g_low != 1 or g_mid != 1 or g_hi != 1:
            x = low * g_low + (x - low - high) * g_mid + high * g_hi
        x = self.color_process(x.astype(np.float32), beat_seconds)
        self.level = max(self.level * 0.82, float(np.abs(x).max()) if len(x) else 0.0)
        return x

    def _filter(self, x, amount):
        if abs(amount) < 0.03:
            self.filter_key = None
            return x
        if amount < 0:
            key = ("lowpass", round(20000 * (150 / 20000) ** min(1.0, -amount), 1))
        else:
            key = ("highpass", round(25 * (7000 / 25) ** min(1.0, amount), 1))
        if key != self.filter_key:
            kind_changed = not self.filter_key or self.filter_key[0] != key[0]
            self.filter_sos = signal.butter(2, key[1], key[0], fs=SR, output="sos")
            if kind_changed or self.filter_zi is None:
                self.filter_zi = np.zeros((self.filter_sos.shape[0], 2, 2))
            self.filter_key = key
        y, self.filter_zi = signal.sosfilt(self.filter_sos, x, axis=0, zi=self.filter_zi)
        return y.astype(np.float32)

    def color_process(self, x, beat_seconds):
        amount = self.color
        kind = self.color_fx
        if kind == "filter":
            return self._filter(x, amount)
        depth = abs(amount)
        if kind == "space":
            if self.color_reverb is None:
                self.color_reverb = Reverb()
            wet = self.color_reverb.process(self._filter(x, 0.35 if amount > 0 else -0.35),
                                            0.85, depth > 0.03)
            return x + wet * depth * 2.2
        if kind == "dub_echo":
            if self.color_echo is None:
                self.color_echo = Echo()
            wet = self.color_echo.process(x, beat_seconds * 0.75, 0.66, depth > 0.03)
            return x + wet * depth
        if kind == "crush" and depth > 0.03:
            steps = 2 ** (16 - int(13 * depth))
            hold = 1 + int(depth * 14)
            crushed = np.round(x * steps) / steps
            if hold > 1:
                crushed = np.repeat(crushed[::hold], hold, axis=0)[: len(x)]
            return (x * (1 - depth) + crushed * depth).astype(np.float32)
        if kind == "noise" and depth > 0.03:
            noise = self.rng.normal(0, 0.08 * depth, x.shape).astype(np.float32)
            return self._filter(x + noise, amount * 0.8)
        return x

    def state(self, full=False):
        info = self.info
        loop = None
        if self.loop_in is not None:
            loop = {"in": self.loop_in, "out": self.loop_out, "on": self.loop_on}
        result = {
            "name": self.name,
            "loaded": self.loaded,
            "loading": self.loading,
            "error": self.error,
            "title": info.get("title"),
            "artist": info.get("artist"),
            "thumbnail": info.get("thumbnail"),
            "duration": round(self.duration, 2),
            "bpm": round(self.bpm, 2) if self.bpm else None,
            "bpm_scale": self.meta.get("bpm_scale", 1.0),
            "first_beat": self.first_beat,
            "key": self.meta.get("key"),
            "camelot": self.meta.get("camelot"),
            "energy": self.meta.get("energy"),
            "music_end": self.music_end(),
            "pos": round(self.seconds, 4),
            "playing": self.playing,
            "rate": round(self.rate_now, 5),
            "pitch": round(self.pitch, 2),
            "range": self.range,
            "sync": self.sync,
            "cue": self.cue,
            "hotcues": self.hotcues,
            "loop": loop,
            "slip": self.slip,
            "reverse": self.reverse,
            "quantize": self.quantize,
            "vinyl": self.vinyl,
            "trim": self.trim,
            "eq": dict(self.eq),
            "color": self.color,
            "color_fx": self.color_fx,
            "fader": self.fader,
            "xf": self.xf,
            "level": round(min(1.5, self.level), 3),
            "wave_id": self.wave_id,
            "karaoke": self.karaoke,
            "stems": {"status": self.stems_status, "extra": self.extra is not None,
                      "gain": {k: round(v, 3) for k, v in self.stem_gain.items()}},
        }
        if full:
            result["wave"] = self.wave
        return result


# --------------------------------------------------------------------------
# Auto transitions
# --------------------------------------------------------------------------

class Gesture:
    """A hand on the record: the deck's speed follows rate(t) (1 = normal,
    negative = backwards) for `seconds` of audio time.

    With slip the song keeps its own time underneath and carries on exactly
    where it would have been, on the beat. jump_to (seconds) first drops the
    needle on a word to scratch; stems overrides the deck's stem levels for
    the duration (a scratch on the vocal alone); after() runs at the end.
    """

    def __init__(self, deck, kind, rate, seconds, slip=True, jump_to=None, stems=None, after=None):
        self.deck = deck
        self.kind = kind
        self.rate = rate
        self.seconds = seconds
        self.slip = slip
        self.jump_to = jump_to
        self.stems = stems
        self.after = after
        self.start = None
        self.saved = {}


def scratch_gesture(deck, beats=2.0, at=None, strokes_per_beat=2, vocals_only=False, rng=None):
    """Baby scratches on the record itself: the song pushed over a word and
    pulled back, `strokes_per_beat` times a beat, landing back on time (slip).

    Played like a hand, not a machine: every stroke a little deeper or
    shallower and a touch early or late, the push quicker than the pull, every
    other stroke a short one. With vocals_only the scratch is on the vocal
    stem, but only where someone sings: on an instrumental bit the whole mix
    is scratched (a vocal stem there is just a hiss).
    """
    rng = rng or random.Random()
    rate0 = max(0.05, abs(deck.base_rate()))
    period = deck.beat_seconds / strokes_per_beat / rate0       # real seconds per stroke
    depth = min(0.22, deck.beat_seconds * 0.42)                 # seconds of record per stroke
    seconds = beats * deck.beat_seconds / rate0
    strokes, starts = [], [0.0]
    while starts[-1] < seconds + period:
        d = depth * (1.0 if len(strokes) % 2 == 0 else 0.55) * rng.uniform(0.82, 1.12)
        strokes.append((d, period * rng.uniform(0.93, 1.06)))
        starts.append(starts[-1] + strokes[-1][1])
    push = 0.42                                                 # share of a stroke spent pushing forward

    def rate(t):
        i = min(max(bisect.bisect_right(starts, t) - 1, 0), len(strokes) - 1)
        d, p = strokes[i]
        u = min(1.0, (t - starts[i]) / p)
        # Position: up to d during the push, back to 0 during the pull (half a cosine each); rate = its slope.
        if u < push:
            return d * math.pi / (2 * push * p) * math.sin(math.pi * u / push)
        v = (u - push) / (1 - push)
        return -d * math.pi / (2 * (1 - push) * p) * math.sin(math.pi * v)
    stems_override = None
    if vocals_only and deck.has_stems:
        where = at if at is not None else deck.seconds
        sung = deck.stem_presence("vocals", where, where + 2 * depth)
        if sung is not None and sung >= 0.15:
            stems_override = {"drums": 0.0, "bass": 0.0, "other": 0.0, "vocals": 1.0}
    return Gesture(deck, "scratch", rate, seconds, slip=True, jump_to=at, stems=stems_override)


def rewind_gesture(deck, to_seconds, engine=None, on_restart=None):
    """A rewind: the record spun back hard, slowing, then the song played
    again from to_seconds (a "pull up": the crowd wants that bit again)."""
    seconds = 1.4

    def rate(t):
        if t < 0.1:
            return 1.0 - 4.5 * t / 0.1
        return -3.5 + 2.3 * (t - 0.1) / (seconds - 0.1)

    def after():
        deck.seek(to_seconds)
        deck.rate_now = deck.base_rate()
        deck.playing = True
        if on_restart:
            on_restart()
    return Gesture(deck, "rewind", rate, seconds, slip=False, after=after)


class Transition:
    """A scripted mix from one deck to the other, driven by the real controls."""

    def __init__(self, out_deck, in_deck, style, beats, start_beat):
        self.out = out_deck
        self.inn = in_deck
        self.style = style
        self.beats = max(1, beats)
        self.start_beat = start_beat   # on the outgoing deck; None = now
        self.started = False
        self.elapsed = 0.0             # beats since the start
        self.done = False
        self.label = STYLES.get(style, (style,))[0]
        self.samples = {}              # sampler hits: {"start": name, "drop": name}
        self.script = None             # keyframes, for a routine
        # Brake/spinback stop the old track before the new one drops.
        self.in_delay = (2 if style in ("brake", "spinback")
                         else self.beats if style == "loop_roll" else 0)

    @property
    def progress(self):
        return _clamp(self.elapsed / self.beats, 0.0, 1.0)

    def json(self):
        return {"from": self.out.name, "to": self.inn.name, "style": self.style,
                "label": self.label, "beats": self.beats, "started": self.started,
                "progress": round(self.progress, 3),
                "starts_in_beats": (None if self.started or self.start_beat is None
                                    else round(self.start_beat - self.out.beat_at(self.out.seconds), 2))}


def script_at(script, beat):
    """Every routine control at `beat` beats into a script.

    A script is a list of keyframes {"at": beat, "ramp": beats, "set": {control:
    value}} in time order. Controls are "xf" (0 = all outgoing, 1 = all
    incoming) and "out.<stem>" / "in.<stem>" stem levels; they start at
    SCRIPT_START. Each keyframe glides its controls from wherever they were to
    the new value over `ramp` beats (an S-curve), starting at `at`.

    "out.guitar" / "in.guitar" (and piano) are optional: until a keyframe sets
    one it isn't in the result and follows that side's "other" (StemGains).
    """
    values = dict(SCRIPT_START)
    for frame in script:
        if beat < frame["at"]:
            break
        ramp = frame.get("ramp") or 0
        q = 1.0 if ramp <= 0 else _smooth((beat - frame["at"]) / ramp)
        for control, target in frame["set"].items():
            if control not in values:            # guitar / piano: start from that side's "other"
                values[control] = values[control.split(".", 1)[0] + ".other"]
            values[control] = values[control] + (target - values[control]) * q
    return values


def _xfader_toward(side_in, q):
    """Crossfader position q of the way from the outgoing side to the incoming."""
    s = 1.0 if side_in == "B" else -1.0
    return -s + 2 * s * _clamp(q, 0.0, 1.0)


def choose_style(out_deck, in_deck):
    """A DJ's pick from tempo, key and energy compatibility."""
    ratio = mixer.tempo_ratio(out_deck.live_bpm(), in_deck.bpm)
    if not ratio or abs(ratio - 1) > 0.08:
        return "echo"
    keys_ok = mixer.key_distance(out_deck.meta.get("camelot"), in_deck.meta.get("camelot")) <= 1
    energy_out = out_deck.meta.get("energy") or 0.5
    energy_in = in_deck.meta.get("energy") or 0.5
    if out_deck.has_stems and in_deck.has_stems:
        # With stems the parts can be traded one at a time.
        if not keys_ok:
            # Clashing keys: only the drums (which have no key) overlap.
            return "drum_bridge"
        if out_deck.bpm:
            # A vocal on the outgoing song can ride over the new instrumental.
            end = out_deck.music_end()
            vocal = out_deck.stem_presence("vocals", end - 32 * out_deck.beat_seconds, end)
            if vocal is not None and vocal > 0.3:
                return "acapella"
            # No vocal but a guitar part: the guitar carries over the new drums and bass.
            guitar = out_deck.stem_presence("guitar", end - 32 * out_deck.beat_seconds, end)
            if guitar is not None and guitar > GUITAR_RIDE_MIN:
                return "guitar_ride"
        return "stem_swap"
    if keys_ok and min(energy_out, energy_in) > 0.45:
        return "blend"
    if energy_in - energy_out > 0.25 or not keys_ok:
        return "filter"
    return "fade"


# Faster than this (scratches, spinbacks) the deck reads the record anti-aliased (_box_read).
FAST_RATE = 1.15


def _box_read(audio, edges):
    """(n, 2) float32: the average of `audio` (linearly interpolated) between each
    pair of neighbouring positions in `edges` (n + 1 of them, in samples, any
    direction). Where two positions are less than a sample apart it is the plain
    interpolated value at the second one."""
    total = len(audio)
    lo = max(0, int(np.floor(edges.min())) - 1)
    hi = min(total - 1, int(np.ceil(edges.max())) + 2)
    seg = audio[lo:hi + 1].astype(np.float64)
    if len(seg) < 2:
        return np.zeros((len(edges) - 1, audio.shape[1]), np.float32)
    integral = np.concatenate([np.zeros((1, seg.shape[1])), np.cumsum((seg[:-1] + seg[1:]) / 2, axis=0)])
    x = np.clip(edges - lo, 0, len(seg) - 1.000001)
    j = x.astype(np.int64)
    f = (x - j)[:, None]
    at = seg[j] + (seg[j + 1] - seg[j]) * f
    area = integral[j] + seg[j] * f + (seg[j + 1] - seg[j]) * f * f / 2
    dx = np.diff(x)[:, None]
    narrow = np.abs(dx) < 1.0
    out = np.where(narrow, at[1:], np.diff(area, axis=0) / np.where(narrow, 1.0, dx))
    return out.astype(np.float32)


# A skip mixes over at most this many beats (about 8 s at 120 bpm).
SKIP_BEATS = 16

# How much of the outgoing mix its guitar has to be for a guitar ride.
GUITAR_RIDE_MIN = 0.3


def auto_beats(style, out_deck, in_deck):
    """Transition length: a key clash without stems is kept short."""
    beats = STYLES[style][1]
    clash = mixer.key_distance(out_deck.meta.get("camelot"), in_deck.meta.get("camelot")) > 1
    if clash and style in ("fade", "filter", "blend"):
        return min(beats, 8)
    return beats


def mix_out_beat(out_deck, style, beats, end_beat, current):
    """Where (in beats) Auto DJ starts the mix: on a phrase, quietest vocals.

    Without stems: the last 8-beat phrase that leaves room for the
    transition before the music ends (what it always did). With stems, the
    candidate phrases in the last ~64 beats are scored by how much the
    outgoing vocal sings over the transition, so two singers don't overlap.
    """
    tail = beats if style not in ("echo", "cut", "brake", "spinback") else max(2, beats // 2)
    latest = math.floor((end_beat - tail) / 8) * 8
    if style not in OVERLAP_STYLES or not out_deck.has_stems or not out_deck.bpm:
        return latest
    best, best_score = latest, None
    for back in range(0, 72, 8):
        start = latest - back
        if start <= current + 4:
            break
        t0 = out_deck.time_of_beat(start)
        vocal = out_deck.stem_presence("vocals", t0, t0 + beats * out_deck.beat_seconds)
        if vocal is None:
            continue
        # Each phrase earlier costs a little: don't cut a song short for nothing.
        score = vocal + back * 0.004
        if best_score is None or score < best_score - 1e-6:
            best, best_score = start, score
        if vocal < 0.12 and back == 0:
            break
    return best


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------

class DJEngine:
    def __init__(self):
        self.lock = threading.RLock()
        self.decks = {name: Deck(name) for name in DECKS}
        self.decks[LAYER] = Deck(LAYER)
        self.decks[LAYER].xf = "THRU"     # the crossfader doesn't touch the layer
        self.xfader = 0.0
        self.xf_curve = "smooth"     # smooth | sharp
        self.master_volume = 0.85
        self.master_name = None
        self.master_level = [0.0, 0.0]
        self.fx = BeatFX()
        self.sampler_volume = 0.7
        self.voices = []             # [sample array, offset]
        self.transition = None
        self.tempo_release = None    # (deck, pitch at start, beats, elapsed)
        self.events = []             # ("ended", deck) / ("transition_done", tr) for Auto DJ
        # Beat-timed touches (Real DJ): [deck name, start beat, end beat,
        # on(), off(), state] run on that deck's own beat clock.
        self.automations = []
        self.timers = []             # [audio time due, fn]: schedule_after()
        self.gestures = []           # Gesture: hands on the records
        self.paused = False          # the whole set paused: its clock stands still
        self.frames = 0
        self.last_render = 0.0

    # ---- musical clock ----

    def master(self):
        deck = self.decks.get(self.master_name) if self.master_name else None
        if deck and deck.loaded and deck.playing and deck.bpm:
            return deck
        # Otherwise the loudest playing deck with a beat grid leads.
        best = None
        for candidate in self.decks.values():
            if candidate.loaded and candidate.playing and candidate.bpm:
                if best is None or self._channel_gain(candidate) > self._channel_gain(best):
                    best = candidate
        return best or (deck if deck and deck.loaded and deck.bpm else None)

    def clock(self):
        """(beat seconds of real time, current beat position) for synced FX."""
        m = self.master()
        if not m:
            return 0.5, self.frames * BLOCK / SR / 0.5
        rate = max(0.05, abs(m.base_rate()))
        return m.beat_seconds / rate, m.beat_at(m.seconds)

    # ---- mixing ----

    def _xf_gain(self, deck):
        if deck.xf == "THRU":
            return 1.0
        t = (self.xfader + 1) / 2
        if deck.xf == "A":
            t = 1 - t
        if self.xf_curve == "sharp":
            return _clamp(t / 0.06, 0.0, 1.0)
        return math.sin(t * math.pi / 2)

    def _channel_gain(self, deck):
        return (deck.fader ** 1.6) * self._xf_gain(deck)

    def _sync_tempo(self):
        m = self.master()
        for deck in self.decks.values():
            if not deck.sync or deck is m or not deck.loaded or not deck.bpm or not m:
                deck.phase_snap = False
                deck.nudge = 0.0
                continue
            master_bpm = m.live_bpm()
            factor = min((0.5, 1.0, 2.0), key=lambda f: abs(math.log(master_bpm / (deck.bpm * f))))
            deck.pitch = _clamp((master_bpm / (deck.bpm * factor) - 1) * 100, -50, 50)
            deck.nudge = 0.0
            if not (deck.playing and m.playing) or deck.scratch is not None or deck.bend:
                continue
            master_phase = m.beat_at(m.seconds)
            own_phase = deck.beat_at(deck.seconds) * factor
            diff = (master_phase - own_phase + 0.5) % 1.0 - 0.5
            if deck.phase_snap:
                # Engaging sync lands on the beat at once, like a CDJ.
                deck.phase_snap = False
                deck.pos += diff * deck.beat_seconds / factor * SR
                deck.shadow = deck.pos
            elif abs(diff) > 0.01:
                # After that, drift is pulled back with a slight speed-up or
                # slow-down, which is inaudible, instead of jumping.
                deck.nudge = _clamp(diff * 0.25, -0.02, 0.02)

    def _run_tempo_release(self, beat_step):
        release = self.tempo_release
        if not release:
            return
        deck, start_pitch, beats, elapsed = release
        elapsed += beat_step
        if not deck.playing or deck.sync and self.master() is not deck:
            self.tempo_release = None
            return
        deck.pitch = start_pitch * (1 - _clamp(elapsed / beats, 0, 1))
        self.tempo_release = None if elapsed >= beats else (deck, start_pitch, beats, elapsed)

    def _run_transition(self, beat_step):
        tr = self.transition
        if not tr or tr.done:
            return
        out, inn = tr.out, tr.inn
        if not tr.started:
            current = out.beat_at(out.seconds) if out.bpm else None
            if (tr.start_beat is None or not out.playing or current is None
                    or current >= tr.start_beat or out.ended):
                self._start_transition(tr)
            return
        tr.elapsed += beat_step
        p = tr.progress
        side = inn.xf if inn.xf in ("A", "B") else ("B" if inn.name == "B" else "A")
        style = tr.style
        if style == "blend":
            if p < 0.45:
                self.xfader = _xfader_toward(side, 0.5 * _smooth(p / 0.45))
            elif p < 0.55:
                self.xfader = _xfader_toward(side, 0.5)
            else:
                self.xfader = _xfader_toward(side, 0.5 + 0.5 * _smooth((p - 0.55) / 0.45))
            swap = _smooth((p - 0.47) / 0.06)
            inn.eq["low"] = KILL_DB + (0 - KILL_DB) * swap
            out.eq["low"] = 0 + (KILL_DB - 0) * swap
        elif style == "fade":
            self.xfader = _xfader_toward(side, _smooth(p))
            out.eq["low"] = -14 * _smooth(p)
            inn.eq["low"] = -14 * (1 - _smooth(p))
        elif style == "filter":
            self.xfader = _xfader_toward(side, _smooth(p))
            out.color_fx = inn.color_fx = "filter"
            out.color = 0.85 * _smooth(p)
            inn.color = -0.8 * (1 - _smooth(p))
        elif style == "echo":
            # The echo catches the last beat, then the channel is cut and
            # the repeats ring out over the new track's first bars.
            if tr.elapsed >= 0.75 and out.fader:
                out.fader = 0.0
                if tr.samples.get("drop"):
                    self.play_sample(tr.samples["drop"])
                if self.fx.on and self.fx.target == out.name:
                    self.fx.set_on(False, self.clock()[0])
            if p > 0.5:
                self.xfader = _xfader_toward(side, 0.5 + 0.5 * _smooth((p - 0.5) / 0.5))
        elif style == "stem_swap":
            # Bring the new song in part by part: drums, then the music, the
            # bass on the halfway downbeat, its vocal last. The old song
            # hands each part over in the same order.
            self.xfader = _xfader_toward(side, 0.5 * _smooth(p / 0.06) + 0.5 * _smooth((p - 0.9) / 0.1))
            swap = _smooth((p - 0.48) / 0.04)
            inn.stem_gain.update(drums=_smooth(p / 0.2), other=_smooth((p - 0.25) / 0.2),
                                 bass=swap, vocals=_smooth((p - 0.75) / 0.15))
            out.stem_gain.update(vocals=1 - _smooth((p - 0.2) / 0.25), bass=1 - swap,
                                 other=1 - _smooth((p - 0.55) / 0.25),
                                 drums=1 - _smooth((p - 0.7) / 0.2))
        elif style == "drum_bridge":
            # Keys clash, so the melodies never overlap: the new drums come in
            # under the old song, the old music and vocal leave, the bass
            # swaps on the halfway downbeat, then the new music and vocal.
            self.xfader = _xfader_toward(side, 0.5 * _smooth(p / 0.06) + 0.5 * _smooth((p - 0.9) / 0.1))
            swap = _smooth((p - 0.48) / 0.04)
            inn.stem_gain.update(drums=_smooth(p / 0.2), bass=swap,
                                 other=_smooth((p - 0.55) / 0.2), vocals=_smooth((p - 0.8) / 0.15))
            out.stem_gain.update(other=1 - _smooth((p - 0.2) / 0.28), vocals=1 - _smooth((p - 0.15) / 0.3),
                                 bass=1 - swap, drums=1 - _smooth((p - 0.65) / 0.25))
        elif style == "acapella":
            # The old song's vocal rides on over the new song's instrumental,
            # then the new vocal takes over at the end.
            self.xfader = _xfader_toward(side, 0.5 * _smooth(p / 0.3) + 0.5 * _smooth((p - 0.9) / 0.1))
            swap = _smooth((p - 0.3) / 0.04)
            inn.stem_gain.update(drums=1.0, other=1.0, bass=swap, vocals=_smooth((p - 0.85) / 0.12))
            out.stem_gain.update(bass=1 - swap, drums=1 - _smooth((p - 0.3) / 0.12),
                                 other=1 - _smooth((p - 0.3) / 0.2),
                                 vocals=1 - _smooth((p - 0.72) / 0.16))
        elif style == "guitar_ride":
            # Like an acapella-over, with the guitar: the old song's vocal and
            # the rest of its music leave first, its guitar riff carries on
            # alone over the new drums and bass (the bass swaps early), then
            # hands over to the new song's music, guitar and vocal. The two
            # guitars never play at once.
            self.xfader = _xfader_toward(side, 0.5 * _smooth(p / 0.1) + 0.5 * _smooth((p - 0.9) / 0.1))
            swap = _smooth((p - 0.4) / 0.04)
            inn.stem_gain.update(drums=_smooth(p / 0.2), bass=swap, other=_smooth((p - 0.55) / 0.2),
                                 guitar=_smooth((p - 0.84) / 0.08), vocals=_smooth((p - 0.85) / 0.12))
            out.stem_gain.update(vocals=1 - _smooth(p / 0.15), other=1 - _smooth((p - 0.1) / 0.2),
                                 drums=1 - _smooth((p - 0.25) / 0.15), bass=1 - swap,
                                 guitar=1 - _smooth((p - 0.68) / 0.15))
        elif style == "mashup":
            # A long acapella-over: the new instrumental comes up under the
            # old vocal and they ride together, then the new vocal takes over.
            self.xfader = _xfader_toward(side, 0.5 * _smooth(p / 0.2) + 0.5 * _smooth((p - 0.9) / 0.1))
            swap = _smooth((p - 0.25) / 0.03)
            inn.stem_gain.update(drums=_smooth(p / 0.12), other=_smooth((p - 0.06) / 0.15), bass=swap,
                                 vocals=_smooth((p - 0.86) / 0.1))
            out.stem_gain.update(bass=1 - swap, drums=1 - _smooth((p - 0.22) / 0.1),
                                 other=1 - _smooth((p - 0.18) / 0.15),
                                 vocals=1 - _smooth((p - 0.8) / 0.1))
        elif style == "loop_roll":
            # The old song is caught in a loop that halves every couple of
            # beats while a high-pass opens up; the new song drops on the one.
            if out.loop_in is not None and out.playing:
                left = tr.beats - tr.elapsed
                size = 4 if left > 6 else 2 if left > 4 else 1 if left > 2 else 0.5 if left > 1 else 0.25
                out.loop_out = out.loop_in + size * out.beat_seconds
                out.loop_on = True
            out.color_fx = "filter"
            out.color = 0.9 * _smooth(p)
            if tr.elapsed >= tr.beats - 0.5 and not self.fx.on:
                self.fx.type, self.fx.beat, self.fx.level, self.fx.target = "echo", 2, 0.6, out.name
                self.fx.set_on(True, self.clock()[0])
            if not inn.playing and tr.elapsed >= tr.in_delay:
                out.playing = False
                self.fx.set_on(False, self.clock()[0])
                self._drop_in(inn)
                self.xfader = _xfader_toward(side, 1.0)
                if tr.samples.get("drop"):
                    self.play_sample(tr.samples["drop"])
        elif style in ("brake", "spinback"):
            if not inn.playing and tr.elapsed >= tr.in_delay:
                self._drop_in(inn)
                self.xfader = _xfader_toward(side, 1.0)
        elif style == ROUTINE:
            self._apply_script(tr, side, tr.elapsed)
        # "cut" happens entirely in _start_transition
        if p >= 1:
            self._finish_transition(tr)

    def _apply_script(self, tr, side, beat):
        values = script_at(tr.script or [], beat)
        self.xfader = _xfader_toward(side, values["xf"])
        for deck, prefix in ((tr.out, "out."), (tr.inn, "in.")):
            # Guitar / piano only when the script set them; otherwise they follow "other".
            deck.stem_gain.update({stem: _clamp(values[prefix + stem], 0.0, 1.0)
                                   for stem in stems.ALL_CONTROLS if prefix + stem in values})

    def schedule(self, deck_name, start_beat, end_beat, on, off):
        """Run on() when deck_name reaches start_beat and off() at end_beat."""
        self.automations.append([deck_name, start_beat, end_beat, on, off, "waiting"])

    def schedule_after(self, seconds, fn):
        """Run fn after this many seconds of rendered audio."""
        self.timers.append([self.frames * BLOCK / SR + seconds, fn])

    def _run_timers(self):
        if not self.timers:
            return
        now = self.frames * BLOCK / SR
        due = [t for t in self.timers if t[0] <= now]
        self.timers = [t for t in self.timers if t[0] > now]
        for _at, fn in due:
            fn()

    def start_gesture(self, g):
        """Put a hand on a deck now (see Gesture). One per deck at a time."""
        deck = g.deck
        if not deck.loaded or any(other.deck is deck for other in self.gestures):
            return False
        g.start = self.frames * BLOCK / SR
        g.saved = {"slip": deck.slip, "stems": dict(deck.stem_gain)}
        if g.slip:
            deck.slip = True
            deck.shadow = deck.pos
        if g.jump_to is not None:
            deck.pos = max(0.0, g.jump_to) * SR
        if g.stems:
            deck.stem_gain.update(g.stems)
        deck.gesture = True
        self.gestures.append(g)
        return True

    def _run_gestures(self):
        if not self.gestures:
            return
        now = self.frames * BLOCK / SR
        keep = []
        for g in self.gestures:
            deck = g.deck
            t = now - g.start
            if t >= g.seconds or not deck.loaded:
                deck.scratch = None
                deck.gesture = False
                if g.slip:
                    deck.end_slip()
                    deck.slip = g.saved["slip"]
                if g.stems:
                    names = set(g.stems) | (set(stems.EXTRA_NAMES) if "other" in g.stems else set())
                    deck.stem_gain.update({k: g.saved["stems"][k] for k in names})
                if g.after:
                    g.after()
                continue
            # The speed at the end of this block; the deck ramps to it.
            deck.scratch = g.rate(t + BLOCK / SR)
            deck.scratch_at = time.monotonic()
            keep.append(g)
        self.gestures = keep

    def unload(self, name):
        """Empty a deck (Real DJ's layer deck when its phrase is over)."""
        deck = self.decks[name]
        deck.playing = False
        deck.audio = deck.stems = deck.extra = deck.wave = None
        deck.info, deck.meta = {}, {}
        deck.sync = False
        deck.stem_gain = stems.StemGains()
        deck.stem_now = dict(deck.stem_gain)
        deck.reset_track_state()

    def pad(self, name):
        """A sampler pad. Scratch and rewind move the record that's on air (a
        real scratch, a real rewind); with nothing on air, the sampled ones."""
        deck = self.master()
        free = deck and deck.playing and deck.bpm and self.transition is None and not deck.gesture
        if name == "scratch":
            # Always the record that's on air (mid-mix too: the louder deck), so it is
            # that song being scratched; the sampled scratch only when nothing plays.
            on_air = sorted((d for d in self.decks.values() if d.playing and d.loaded and not d.gesture),
                            key=self._channel_gain, reverse=True)
            if on_air:
                self.start_gesture(scratch_gesture(on_air[0], beats=2))
                return
            if any(d.playing and d.gesture for d in self.decks.values()):
                return                      # a hand is already on the record

        if name == "rewind" and free:
            # Back to the start of the previous 8 bars, and play it again.
            beat = deck.beat_at(deck.seconds)
            target = max(0.0, deck.time_of_beat(math.floor(beat / 32) * 32 - 32))
            self.start_gesture(rewind_gesture(deck, target))
            return
        self.play_sample(name)

    def play_sample(self, name):
        bank = sample_bank()
        if name in bank:
            self.voices.append([bank[name], 0])
            self.voices = self.voices[-6:]

    def _run_automations(self):
        if not self.automations:
            return
        keep = []
        for item in self.automations:
            name, start, end, on, off, state = item
            deck = self.decks[name]
            if not deck.loaded or not deck.playing:
                if state == "on":
                    off()
                continue                # its song left the air: drop it
            beat = deck.beat_at(deck.seconds)
            if state == "waiting" and beat >= start:
                if beat < end:
                    on()
                    item[5] = state = "on"
                else:
                    continue            # missed it (a seek): skip
            if state == "on" and beat >= end:
                off()
                continue
            keep.append(item)
        self.automations = keep

    def _start_transition(self, tr):
        out, inn = tr.out, tr.inn
        tr.started = True
        if tr.samples.get("start"):
            self.play_sample(tr.samples["start"])
        if tr.samples.get("drop") and tr.style == "cut":
            self.play_sample(tr.samples["drop"])
        if (tr.style in STEM_STYLES or tr.style == ROUTINE) and not (out.has_stems and inn.has_stems):
            if tr.style == ROUTINE:
                tr.beats = 32
            tr.style = "blend"         # stems not ready (yet): the EQ version
            tr.label = STYLES["blend"][0]
        if tr.style == "guitar_ride" and out.extra is None:
            tr.style = "stem_swap"     # the old song has no separate guitar
            tr.label = STYLES["stem_swap"][0]
        side = inn.xf if inn.xf in ("A", "B") else ("B" if inn.name == "B" else "A")
        inn.fader = 1.0
        inn.eq.update({"hi": 0.0, "mid": 0.0})
        if tr.style == "blend":
            inn.eq["low"] = KILL_DB
        if tr.style == "fade":
            inn.eq["low"] = -14
        if tr.style == "filter":
            inn.color_fx = "filter"
            inn.color = -0.8
        if tr.style in ("stem_swap", "drum_bridge", "guitar_ride"):
            inn.stem_gain.update(drums=0.0, bass=0.0, other=0.0, vocals=0.0)
        if tr.style == "acapella":
            inn.stem_gain.update(drums=1.0, bass=0.0, other=1.0, vocals=0.0)
        if tr.style == "mashup":
            inn.stem_gain.update(drums=0.0, bass=0.0, other=0.0, vocals=0.0)
        if tr.style == "loop_roll":
            out.loop_in = out.snap(out.seconds, force=True)
            out.loop_out = out.loop_in + 4 * out.beat_seconds
            out.loop_on = True
        if tr.style == ROUTINE:
            self._apply_script(tr, side, 0.0)
        if not inn.playing and not tr.in_delay:
            self._drop_in(inn)
        if tr.style == ROUTINE:
            pass
        elif tr.style == "echo":
            self.fx.set_on(False, self.clock()[0])
            self.fx.tail = 0
            self.fx.type = "echo"
            self.fx.beat = 3
            self.fx.level = 0.75
            self.fx.target = out.name
            self.fx.set_on(True, self.clock()[0])
            self.xfader = _xfader_toward(side, 0.5)
        elif tr.style == "cut":
            self.xfader = _xfader_toward(side, 1.0)
            out.playing = False
        elif tr.style in ("brake", "spinback"):
            out.brake = (tr.style, out.base_rate(),
                         out.beat_seconds * tr.in_delay / max(0.05, out.base_rate()), 0.0)
            self.xfader = _xfader_toward(side, 0.0)
        else:
            self.xfader = _xfader_toward(side, 0.0)

    @staticmethod
    def _drop_in(deck):
        deck.seek(deck.cue)
        deck.playing = True
        deck.phase_snap = True

    def _finish_transition(self, tr):
        out, inn = tr.out, tr.inn
        tr.done = True
        if not inn.playing:
            self._drop_in(inn)
        out.playing = False
        out.brake = None
        out.ended = True             # spent: Auto DJ loads the next song here
        out.loop_on = False
        out.eq.update({"hi": 0.0, "mid": 0.0, "low": 0.0})
        out.color = 0.0
        out.fader = 1.0
        inn.eq.update({"hi": 0.0, "mid": 0.0, "low": 0.0})
        inn.color = 0.0
        for deck in (out, inn):
            deck.stem_gain = stems.StemGains()
        side = inn.xf if inn.xf in ("A", "B") else ("B" if inn.name == "B" else "A")
        self.xfader = _xfader_toward(side, 1.0)
        self.master_name = inn.name
        if inn.sync and abs(inn.pitch) > 0.3:
            # Ease the new track back to its own tempo over 16 bars.
            self.tempo_release = (inn, inn.pitch, 64, 0.0)
        self.transition = None
        self.events.append(("transition_done", tr))

    def render(self):
        """One 20 ms block of s16le stereo."""
        with self.lock:
            now = time.monotonic()
            self.last_render = now
            if not self.paused:
                self.frames += 1
            self._sync_tempo()
            beat_seconds, beat_pos = self.clock()
            beat_step = BLOCK / SR / beat_seconds
            if not self.paused:
                # Paused, a mix, a hook loop or a scratch waits exactly where it was.
                self._run_transition(beat_step)
                self._run_tempo_release(beat_step)
                self._run_automations()
                self._run_timers()
                self._run_gestures()
            mix = np.zeros((BLOCK, 2), np.float32)
            fx_target = self.fx.target
            for deck in self.decks.values():
                was_ended = deck.ended
                raw = deck.read(BLOCK, now)
                if deck.ended and not was_ended:
                    self.events.append(("ended", deck.name))
                if raw is None:
                    if deck.level:
                        deck.level *= 0.8
                    if fx_target == deck.name and self.fx.active():
                        mix += self.fx.process(np.zeros((BLOCK, 2), np.float32), beat_seconds,
                                               beat_pos, 1 / (beat_seconds * SR)) * self._xf_gain(deck)
                    continue
                channel = deck.strip(raw, beat_seconds) * (deck.fader ** 1.6)
                if fx_target == deck.name:
                    channel = self.fx.process(channel, beat_seconds, beat_pos, 1 / (beat_seconds * SR))
                mix += channel * self._xf_gain(deck)
            if fx_target == "master":
                mix = self.fx.process(mix, beat_seconds, beat_pos, 1 / (beat_seconds * SR))
            mix *= self.master_volume
            if self.voices:
                alive = []
                for voice in self.voices:
                    sample, offset = voice
                    chunk = sample[offset:offset + BLOCK]
                    mix[: len(chunk)] += chunk * self.sampler_volume
                    if offset + BLOCK < len(sample):
                        alive.append([sample, offset + BLOCK])
                self.voices = alive
            # Soft limiter: transparent below 0.8, rounds peaks above it.
            over = np.abs(mix) > 0.8
            if over.any():
                sign = np.sign(mix[over])
                mix[over] = sign * (0.8 + 0.2 * np.tanh((np.abs(mix[over]) - 0.8) / 0.2))
            peak = np.abs(mix).max(axis=0)
            self.master_level = [max(self.master_level[i] * 0.82, float(peak[i])) for i in (0, 1)]
            return (np.clip(mix, -1, 1) * 32767).astype("<i2").tobytes()

    # ---- state ----

    def state(self, full=False):
        with self.lock:
            m = self.master()
            return {
                "decks": {name: deck.state(full) for name, deck in self.decks.items()},
                "mixer": {"xfader": round(self.xfader, 3), "xf_curve": self.xf_curve,
                          "master": self.master_volume, "sampler": self.sampler_volume,
                          "master_deck": m.name if m else None,
                          "level": [round(v, 3) for v in self.master_level]},
                "fx": {"type": self.fx.type, "beat": self.fx.beat, "level": self.fx.level,
                       "on": self.fx.on, "target": self.fx.target},
                "transition": self.transition.json() if self.transition else None,
                "rendering": time.monotonic() - self.last_render < 0.5,
            }


# --------------------------------------------------------------------------
# Sessions: one booth per Discord guild / Huddle room
# --------------------------------------------------------------------------

#: guild key (str(guild.id) or "huddle:<channel>") -> DJSession
sessions = {}
# Background stem work waits while any booth is on air.
stems.pause_background = lambda: any(s.live and s.active for s in list(sessions.values()))

#: async (query) -> {"audio_url", "page_url", "title"?, ...}; set by webui
resolver = None
#: same, but straight from the bot's yt-dlp; used to retry a failed link
fallback_resolver = None


class DiscordCrate:
    """The guild's normal queue is the crate: /play keeps feeding the DJ."""

    def __init__(self, player, resolve_file):
        self.player = player
        self.resolve_file = resolve_file

    def items(self):
        return [self._item(song) for song in list(self.player.queue)[:100]]

    def _item(self, song):
        url = song.url or ""
        query = url[len("spotify:search:"):] if url.startswith("spotify:search:") else url
        return {"title": song.title, "artist": getattr(song, "artist", None),
                "thumbnail": song.thumbnail, "duration": song.duration,
                "query": query if song.source_type != "local" else None,
                "file": self.resolve_file(song),
                "keys": [mixer.track_key(url), mixer.track_key(query)]}

    async def take(self, index=0):
        queue = self.player.queue
        if not 0 <= index < len(queue):
            return None
        song = queue[index]
        del queue[index]
        self.player.history.appendleft(song)  # Smart Autoplay seeds from here
        if hasattr(self.player, "long_history"):
            self.player.long_history.appendleft(song)
        return {**self._item(song), "song": song}

    async def add(self, query, requester=None):
        cog = self.player.bot.get_cog("MusicCog")
        song = await cog.process_youtube(query, requester or self.player.guild.me) if cog else None
        if not song:
            raise RuntimeError("Nothing found for that search.")
        self.player.queue.append(song)
        return song.title


class HuddleCrate:
    """A Huddle room's hub queue, as last seen by the voice publisher."""

    def __init__(self, channel_id):
        self.channel_id = channel_id
        self.used = set()

    def _player(self):
        import huddle_voice
        manager = huddle_voice.MANAGER
        return (manager.players.get(self.channel_id) if manager else None) or {}

    def _tracks(self):
        player = self._player()
        tracks = []
        track = player.get("track")
        if track and track.get("id") not in self.used:
            tracks.append(("current", track))
        tracks += [("queue", item) for item in player.get("queue") or []]
        return tracks

    @staticmethod
    def _item(track):
        return {"title": track.get("title"), "artist": track.get("artist"),
                "thumbnail": track.get("thumbnail"),
                "duration": track.get("duration"),
                "audio_url": track.get("audioUrl"),
                "query": track.get("pageUrl") or track.get("query") or track.get("title"),
                "keys": [mixer.track_key(track.get("pageUrl")), mixer.track_key(track.get("query"))],
                # A planned playlist transition out of this song (cut points, booth style).
                "mix": track.get("mix") if isinstance(track.get("mix"), dict) else None}

    def items(self):
        return [self._item(track) for _, track in self._tracks()[:100]]

    async def take(self, index=0):
        import huddle
        tracks = self._tracks()
        if not 0 <= index < len(tracks):
            return None
        where, track = tracks[index]
        if where == "current":
            self.used.add(track.get("id"))
        else:
            queue_index = index - (1 if tracks and tracks[0][0] == "current" else 0)
            await huddle._request("POST", "/api/bot/player", {
                "channelId": self.channel_id,
                "action": {"name": "remove", "index": queue_index}})
            player = self._player()
            if player.get("queue"):
                player["queue"] = [t for t in player["queue"] if t.get("id") != track.get("id")]
        return self._item(track)

    async def add(self, query, requester=None):
        import huddle
        await huddle.play(huddle.PREFIX + self.channel_id, query, requested_by="DJ booth")
        return query


class DJSession:
    def __init__(self, key, kind, crate, auto=False):
        self.key = key
        self.kind = kind                 # discord | huddle
        self.crate = crate
        self.engine = DJEngine()
        self.auto = auto
        self.mode = "auto"               # auto (smooth queue) | real (Real DJ set)
        self.real = None                 # realdj.RealDJ while mode == "real"
        self.vibe = "club"               # Real DJ: chill | club | hype
        self.three = False               # Real DJ: 3 songs (layer a third song's vocal)
        self.ai = False                  # experimental: Real DJ uses the offline AI analysis (djbrain)
        self.paused = False              # the whole set paused (pause / resume)
        self.paused_decks = []           # what was playing when it was paused
        self.saved_at = 0.0              # monotonic time of the last snapshot
        self.restoring = False           # bringing a set back: the tick waits
        self.auto_style = "auto"
        self.auto_beats = None           # None = the style's default
        self.history = []
        self.live = False                # engine is the room's output
        self.active = True
        self.started_at = time.time()
        self.notice = None
        self.load_tasks = {}
        self.stem_tasks = {}
        self.stems_on = load_settings().get(key, {}).get("stems", True)
        for deck in self.engine.decks.values():
            deck.use_stems = self.stems_on
        self.tick_task = None
        self.on_stop = None              # platform clean-up, set by start_*()
        self.report_on_air = None        # async (info | None), set by start_*()
        self.karaoke_setting = None      # () -> bool, the room's karaoke mode
        self.reported = None             # (info, monotonic time) last sent
        warm_samples()

    # ---- loading ----

    async def load(self, deck_name, item, position=None, play=False, started=None):
        deck = self.engine.decks[deck_name]
        with self.engine.lock:
            if deck.playing and self._audible(deck):
                raise RuntimeError(f"Deck {deck_name} is on air. Pause it or pull its fader first.")
            deck.load_token += 1
            token = deck.load_token
            deck.loading = item.get("title") or item.get("query") or "Loading"
            deck.error = None
        started = started or time.monotonic()
        cached_stems = cached_extra = None
        try:
            source = item.get("file") or item.get("audio_url")
            keys = [k for k in (item.get("keys") or []) if k]
            loop = asyncio.get_running_loop()
            local = None
            # A song with stems is rebuilt from the cache even when there is a
            # downloaded file: the file decodes to different samples than the
            # stream the stems were made from, so it would be split all over again.
            if keys and stems.enabled() and self.stems_on:
                local = await loop.run_in_executor(None, stems.local_audio, keys)
            if local:
                # Prepared song: rebuilt from the stems cache, no download.
                audio, cached_stems = local
                cached_extra = await loop.run_in_executor(
                    None, lambda: stems.extras(keys=keys, frames=cached_stems.shape[1]))
                source = None
                logger.info("DJ %s: %r rebuilt from the stems cache", self.key, item.get("title"))
            elif not source:
                if not resolver:
                    raise RuntimeError("Song lookup is not available.")
                resolved = await resolver(item.get("query") or item.get("title"))
                source = resolved["audio_url"]
                keys.append(mixer.track_key(resolved.get("page_url")))
                item = {**item, "title": item.get("title") or resolved.get("title") or item.get("query"),
                        "artist": item.get("artist") or resolved.get("artist"),
                        "thumbnail": item.get("thumbnail") or resolved.get("thumbnail")}
            try:
                if source is not None:
                    audio = await loop.run_in_executor(None, decode_track, source)
            except Exception as error:
                # Stream links expire (YouTube answers 403) or get throttled:
                # look the song up again and try once more.
                lookup = item.get("query") or item.get("title")
                if item.get("file") or not resolver or not lookup:
                    raise
                logger.info("DJ %s: stream for %r failed (%s), resolving it again",
                            self.key, item.get("title"), str(error)[-80:])
                resolved = await (fallback_resolver or resolver)(lookup)
                source = resolved["audio_url"]
                keys.append(mixer.track_key(resolved.get("page_url")))
                audio = await loop.run_in_executor(None, decode_track, source)
            meta, wave = await loop.run_in_executor(None, analyze_buffer, audio, keys)
        except Exception as error:
            with self.engine.lock:
                if deck.load_token == token:
                    deck.loading = None
                    deck.error = str(error)[:200]
            raise
        with self.engine.lock:
            if deck.load_token != token:
                return
            deck.audio = audio
            deck.stems = cached_stems
            deck.extra = cached_extra
            deck.stems_status = ("ready" if cached_stems is not None
                                 else "separating" if self._stems_wanted() else None)
            deck.meta = dict(meta)
            deck.wave = wave
            deck.wave_id += 1
            deck.info = {"title": item.get("title"), "artist": item.get("artist"),
                         "thumbnail": item.get("thumbnail"),
                         "song": item.get("song"), "query": item.get("query"),
                         "keys": keys, "mix": item.get("mix")}
            deck.loading = None
            deck.error = None
            deck.reset_track_state()
            # Auto gain to a common loudness, cue on the first downbeat.
            loud = meta.get("loudness_db")
            deck.trim = round(_clamp(-11.0 - loud, -9, 6), 1) if loud is not None else 0.0
            start = float(meta.get("music_start") or 0.0)
            cue = start
            if deck.bpm:
                # The first beat at (or just before) the music, never before 0 s.
                beats = math.ceil(deck.beat_at(start) - 0.1)
                cue = deck.time_of_beat(beats)
                while cue < 0:
                    cue += deck.beat_seconds
            deck.cue = cue
            if position is not None:
                deck.seek(position + (time.monotonic() - started if play else 0.0))
            else:
                deck.seek(deck.cue)
            deck.playing = play
            self.history.insert(0, {"title": deck.info["title"], "artist": deck.info["artist"],
                                    "thumbnail": deck.info["thumbnail"], "at": time.time()})
            del self.history[30:]
        logger.info("DJ %s: deck %s loaded %r (%s bpm, %s)", self.key, deck_name,
                    item.get("title"), meta.get("bpm"), meta.get("camelot"))
        if self._stems_wanted() and cached_stems is None:
            self.stem_tasks[deck_name] = asyncio.create_task(self._separate(deck, token, audio))

    def _stems_wanted(self):
        return self.stems_on and stems.enabled()

    def set_stems(self, on):
        """The room's STEMS switch: split songs (and offer stem mixes) or not."""
        self.stems_on = bool(on)
        settings = load_settings()
        settings.setdefault(self.key, {})["stems"] = self.stems_on
        save_settings(settings)
        with self.engine.lock:
            for deck in self.engine.decks.values():
                deck.use_stems = self.stems_on
                if self.stems_on and deck.loaded and deck.stems is None and deck.stems_status != "separating":
                    if stems.enabled():
                        deck.stems_status = "separating"
                        self.stem_tasks[deck.name] = asyncio.create_task(
                            self._separate(deck, deck.load_token, deck.audio))
                elif not self.stems_on and deck.stems_status in ("separating", "failed"):
                    deck.stems_status = None
            tr = self.engine.transition
            if not self.stems_on and tr and tr.style in STEM_STYLES:
                if tr.started:
                    self.engine.transition = None
                else:
                    tr.style, tr.label = "blend", STYLES["blend"][0]

    def set_ai(self, on):
        """The experimental AI switch: Real DJ uses the offline analysis (sound similarity, Gemini exits, entries,
        hooks and effect moves) while it is on, and behaves exactly as before while it is off."""
        self.ai = bool(on)
        for deck in self.engine.decks.values():
            deck.ai = self.ai

    async def _separate(self, deck, token, audio):
        """Split the deck's song into stems in the background; it plays meanwhile."""
        try:
            # Shielded: a booth closing mid-split still finishes into the cache.
            result = await asyncio.shield(
                stems.SEPARATOR.separate(audio, wanted=lambda: deck.load_token == token,
                                         title=deck.info.get("title")))
            if result is not None and result.shape != (len(stems.NAMES),) + audio.shape:
                raise RuntimeError(f"stems have shape {result.shape}, expected {audio.shape}")
            extra = None
            if result is not None:
                keys, title = deck.info.get("keys"), deck.info.get("title")
                extra = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: _keep_split(audio, result, keys, title))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning("DJ %s: stem separation failed on %s: %s", self.key, deck.name, error)
            with self.engine.lock:
                if deck.load_token == token:
                    deck.stems_status = "failed"
            return
        with self.engine.lock:
            if deck.load_token != token or not deck.use_stems:
                return
            if result is None:
                deck.stems_status = None
                return
            deck.stems = result
            deck.extra = extra
            deck.stems_status = "ready"
            # Fade any gains already set in from the full mix, no jump.
            deck.stem_now = dict(stems.StemGains())

    def _audible(self, deck):
        return self.engine._channel_gain(deck) * self.engine.master_volume > 0.02

    def load_in_background(self, deck_name, item, **kwargs):
        async def run():
            try:
                await self.load(deck_name, item, **kwargs)
            except Exception as error:
                self.notice = f"Deck {deck_name}: {error}"
                logger.warning("DJ %s: load failed on %s: %s", self.key, deck_name, error)
        task = asyncio.create_task(run())
        self.load_tasks[deck_name] = task
        return task

    # ---- auto DJ ----

    def start_ticking(self):
        if not self.tick_task:
            self.tick_task = asyncio.create_task(self._tick_loop())

    async def _tick_loop(self):
        while self.active:
            try:
                await self._tick()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("DJ %s: auto tick failed", self.key)
            await asyncio.sleep(0.25)

    def _loading(self, deck_name):
        task = self.load_tasks.get(deck_name)
        return bool(task and not task.done())

    async def _next_item(self):
        item = await self.crate.take(0)
        if item or self.kind != "discord":
            return item
        # Empty crate on Discord: fall back to Smart Autoplay's pick.
        player = self.crate.player
        cog = player.bot.get_cog("MusicCog")
        if not cog:
            return None
        try:
            song = await cog.pick_autoplay_recommendation(player)
        except Exception as error:
            logger.debug("DJ autoplay pick failed: %s", error)
            return None
        return {**self.crate._item(song), "song": song} if song else None

    def on_air(self):
        """The song the room hears now: the master deck, if it is audible."""
        with self.engine.lock:
            m = self.engine.master()
            decks = [m] if m else []
            decks += sorted((d for d in self.engine.decks.values() if d is not m),
                            key=self.engine._channel_gain, reverse=True)
            deck = next((d for d in decks if d.loaded and d.playing and self._audible(d)), None)
            if not deck:
                return None            # _report keeps the last song, stopped
            return {"id": f"dj:{deck.name}:{deck.wave_id}", "title": deck.info.get("title") or "",
                    "artist": deck.info.get("artist") or "", "thumbnail": deck.info.get("thumbnail"),
                    "duration": round(deck.duration, 2), "positionMs": round(deck.seconds * 1000),
                    "paused": not deck.playing}

    async def _report(self):
        """Tell the room what is on air, when it changes or its clock drifts."""
        if not self.report_on_air or not self.live:
            return
        info = self.on_air()
        now = time.monotonic()
        last = self.reported
        if last:
            sent, at = last
            expected = sent and sent["positionMs"] + (0 if sent["paused"] else (now - at) * 1000)
            if not info and sent:
                # Nothing audible: keep showing the last song, stopped, rather
                # than the room's own (paused) track.
                info = dict(sent, paused=True, positionMs=round(expected))
            same = info and sent and sent["id"] == info["id"] and sent["paused"] == info["paused"]
            # A heartbeat every 20 s tells the room the booth is still alive.
            if same and now - at < 20 and (abs(expected - info["positionMs"]) < 800 or now - at < 1.0):
                return
            if not info and not sent:
                return
        self.reported = (info, now)
        try:
            await self.report_on_air(info)
        except Exception as error:
            logger.debug("DJ %s: on-air report failed: %s", self.key, error)

    def _follow_karaoke(self):
        """Mirror the room's karaoke setting onto the decks (checked every 2 s)."""
        now = time.monotonic()
        if now - getattr(self, "_karaoke_at", 0) < 2 or not self.karaoke_setting:
            return
        self._karaoke_at = now
        try:
            on = bool(self.karaoke_setting())
        except Exception:
            return
        with self.engine.lock:
            for deck in self.engine.decks.values():
                deck.karaoke = on

    async def _tick(self):
        engine = self.engine
        self._follow_karaoke()
        if self.restoring:
            return
        await self._report()
        self.save_state()
        with engine.lock:
            events, engine.events = engine.events, []
        if self.paused:
            return
        if not self.auto or not self.live:
            return
        if self.mode == "real":
            if self.real is None:
                self.real = realdj.RealDJ(self, vibe=self.vibe)
            await self.real.tick()
            return
        with engine.lock:
            decks = engine.decks
            playing = [d for d in decks.values() if d.playing]
            tr = engine.transition
        if not playing:
            # Nothing on air: start whatever is loaded, else pull a track in.
            ready = next((d for d in decks.values() if d.name in DECKS and d.loaded and not d.ended), None)
            if ready:
                with engine.lock:
                    ready.playing = True
                    ready.fader = 1.0
                    engine.xfader = _xfader_toward(ready.xf if ready.xf in ("A", "B") else ready.name, 1.0)
                    engine.master_name = ready.name
                return
            target = next((d for d in DECKS if not self._loading(d)), None)
            if target and not any(self._loading(d) for d in DECKS):
                item = await self._next_item()
                if item:
                    self.load_in_background(target, item, play=True)
            return
        with engine.lock:
            m = engine.master() or playing[0]
            other = decks["B" if m.name == "A" else "A"]
        if other.playing or tr:
            return
        if (not other.loaded or other.ended) and not self._loading(other.name):
            if m.seconds > 4:
                item = await self._next_item()
                if item:
                    self.load_in_background(other.name, item)
            return
        if not other.loaded or self._loading(other.name):
            return
        with engine.lock:
            plan = planned_mix(m, other)
            style = self.auto_style
            if style == "auto":
                style = plan.get("style") or choose_style(m, other)
            beats = self.auto_beats or plan.get("beats") or auto_beats(style, m, other)
            if other.bpm and m.bpm:
                ratio = mixer.tempo_ratio(m.live_bpm(), other.bpm)
                other.sync = bool(ratio and abs(ratio - 1) <= 0.08)
            if plan.get("in_at") is not None and not other.info.get("planned_cue") and not other.playing:
                other.cue = (max(0.0, other.time_of_beat(round(other.beat_at(plan["in_at"]) / 4) * 4))
                             if other.bpm else plan["in_at"])
                other.seek(other.cue)
                other.info["planned_cue"] = True
            if m.bpm:
                end_beat = m.beat_at(m.music_end())
                current = m.beat_at(m.seconds)
                start_beat = plan.get("start_beat") or mix_out_beat(m, style, beats, end_beat, current)
                if m.loop_on:
                    return  # the DJ is holding a loop; wait for them
                if start_beat <= current:
                    start_beat = math.ceil(current / 4) * 4 + 4
                seconds_left = (start_beat - current) * m.beat_seconds / max(0.05, m.base_rate())
            else:
                start_beat = None
                seconds_left = (m.music_end() - 12 - m.seconds) / max(0.05, m.base_rate())
                style = "fade" if style not in ("cut", "echo") else style
            if seconds_left < 1.2 or (start_beat is None and seconds_left <= 0):
                engine.transition = Transition(m, other, style, beats, start_beat)
                logger.info("DJ %s: %s from %s to %s over %s beats", self.key, style, m.name,
                            other.name, beats)

    # ---- controls ----

    async def handle(self, body, requester=None):
        op = str(body.get("op") or "")
        engine = self.engine
        deck_name = str(body.get("deck") or "").upper()
        deck = engine.decks.get(deck_name)
        if op == "pause":
            self.pause()
            return
        if op == "resume":
            self.resume()
            return
        if op == "skip":
            await self.skip()
            return
        if self.paused and op in ("play", "cue", "mix", "scratch", "brake"):
            self.resume()                  # a hand on the decks picks the set back up
        if op in ("load", "load_next"):
            if not deck:
                raise ValueError("Choose deck A or B.")
            if op == "load_next" or body.get("index") is not None:
                item = await self.crate.take(int(body.get("index") or 0))
                if not item:
                    raise ValueError("The queue is empty. Search for a song or /play one.")
            else:
                query = str(body.get("query") or "").strip()[:300]
                if not query:
                    raise ValueError("Type a song name or paste a link.")
                item = {"query": query}
            with engine.lock:
                if deck.playing and self._audible(deck):
                    raise ValueError(f"Deck {deck_name} is on air. Pause it or pull its fader first.")
            self.load_in_background(deck.name, item)
            return
        if op == "queue":
            query = str(body.get("query") or "").strip()[:300]
            if not query:
                raise ValueError("Type a song name or paste a link.")
            title = await self.crate.add(query, requester)
            self.notice = f"Queued {title}"
            return
        if op == "stems":
            if not stems.enabled() and body.get("on"):
                raise ValueError("Stem separation is not installed on this bot.")
            self.set_stems(body.get("on", not self.stems_on))
            return
        if op == "auto":
            if body.get("mode") in ("auto", "real") and body["mode"] != self.mode:
                self.mode = body["mode"]
                self.real = realdj.RealDJ(self, vibe=self.vibe) if self.mode == "real" else None
                if self.mode == "real":
                    self.auto = True
                with engine.lock:
                    if engine.transition and not engine.transition.started:
                        engine.transition = None   # replanned by the new mode
            if body.get("vibe") in realdj.VIBES:
                self.vibe = body["vibe"]
                if self.real:
                    self.real.vibe = self.vibe
            if "three" in body:
                self.three = bool(body["three"])
                if self.real:
                    self.real.three = self.three
            if "ai" in body:
                self.set_ai(bool(body["ai"]))
            if "on" in body:
                self.auto = bool(body["on"])
            if body.get("style") in STYLES:
                self.auto_style = body["style"]
            if "beats" in body:
                beats = body.get("beats")
                self.auto_beats = int(_clamp(int(beats), 1, 64)) if beats else None
            if not self.auto:
                with engine.lock:
                    if engine.transition and not engine.transition.started:
                        engine.transition = None
            return
        with engine.lock:
            self._handle_locked(op, body, deck)

    def _handle_locked(self, op, body, deck):
        engine = self.engine
        value = body.get("value")
        if deck is None and op not in ("set_mixer", "fx", "sample", "mix", "cancel_mix"):
            raise ValueError("Choose deck A or B.")
        if deck is not None and not deck.loaded and op not in ("set", "eject", "flag"):
            raise ValueError(f"Deck {deck.name} is empty. Load a song first.")
        now = time.monotonic()
        if op == "play":
            if deck.cue_preview:
                deck.cue_preview = False
            want = (not deck.playing) if value is None else bool(value)
            if want and deck.ended:
                deck.seek(deck.cue)
            deck.playing = want
            deck.brake = None
            if want and deck.sync:
                deck.phase_snap = True
            if not want:
                deck.end_slip()
        elif op == "cue":
            # Pioneer CUE: playing -> back to cue and stop; stopped at cue ->
            # preview while held; stopped elsewhere -> set the cue here.
            down = body.get("down", True)
            if down:
                if deck.playing and not deck.cue_preview:
                    deck.playing = False
                    deck.seek(deck.cue)
                elif abs(deck.seconds - deck.cue) < 0.03:
                    deck.cue_preview = True
                    deck.playing = True
                else:
                    deck.cue = deck.snap(deck.seconds)
                    deck.seek(deck.cue)
            elif deck.cue_preview:
                deck.cue_preview = False
                deck.playing = False
                deck.seek(deck.cue)
        elif op == "hotcue":
            index = int(body.get("index") or 0) % 8
            if body.get("clear"):
                deck.hotcues[index] = None
            elif deck.hotcues[index] is None:
                deck.hotcues[index] = round(deck.snap(deck.seconds), 4)
            else:
                deck.seek(deck.hotcues[index])
                deck.loop_on = False
                if not deck.playing:
                    deck.playing = True
                if deck.sync:
                    deck.phase_snap = True
        elif op == "seek":
            deck.seek(float(value or 0))
            if deck.sync:
                deck.phase_snap = True
        elif op == "beat_jump":
            beats = float(body.get("beats") or 0)
            deck.seek(deck.seconds + beats * deck.beat_seconds)
            if deck.loop_on and deck.loop_in is not None:
                shift = beats * deck.beat_seconds
                deck.loop_in += shift
                deck.loop_out += shift
        elif op == "loop_in":
            deck.loop_in = deck.snap(deck.seconds)
            deck.loop_out = None
            deck.loop_on = False
        elif op == "loop_out":
            if deck.loop_in is None or deck.seconds <= deck.loop_in + 0.05:
                raise ValueError("Set LOOP IN first, a little earlier in the track.")
            deck.loop_out = deck.snap(deck.seconds)
            if deck.loop_out <= deck.loop_in:
                deck.loop_out = deck.loop_in + deck.beat_seconds
            deck.loop_on = True
        elif op == "reloop":
            if deck.loop_in is None or deck.loop_out is None:
                raise ValueError("No loop set yet.")
            if deck.loop_on:
                deck.loop_on = False
                deck.end_slip()
            else:
                deck.loop_on = True
                deck.seek(deck.loop_in)
        elif op == "loop":
            beats = float(body.get("beats") or 4)
            if deck.loop_on and deck.loop_in is not None and abs(
                    (deck.loop_out - deck.loop_in) - beats * deck.beat_seconds) < 0.01:
                deck.loop_on = False  # pressing the active size again exits
                deck.end_slip()
            else:
                start = deck.snap(deck.seconds)
                if start > deck.seconds:
                    start -= deck.beat_seconds if deck.bpm and deck.quantize else 0
                deck.loop_in = max(0.0, start)
                deck.loop_out = deck.loop_in + beats * deck.beat_seconds
                deck.loop_on = True
        elif op in ("loop_half", "loop_double"):
            if deck.loop_in is None or deck.loop_out is None:
                raise ValueError("No loop set yet.")
            length = deck.loop_out - deck.loop_in
            length = length / 2 if op == "loop_half" else length * 2
            deck.loop_out = deck.loop_in + max(0.01, length)
            if deck.loop_on and deck.seconds >= deck.loop_out:
                deck.seek(deck.loop_in + (deck.seconds - deck.loop_in) % length)
        elif op == "loop_exit":
            deck.loop_on = False
            deck.end_slip()
        elif op == "sync":
            deck.sync = (not deck.sync) if value is None else bool(value)
            if deck.sync:
                deck.phase_snap = True
            if engine.tempo_release and engine.tempo_release[0] is deck:
                engine.tempo_release = None
        elif op == "master":
            engine.master_name = deck.name
        elif op == "bend":
            deck.bend = _clamp(float(value or 0), -1, 1)
        elif op == "scratch":
            if value is None:
                deck.scratch = None
                deck.end_slip()
            else:
                deck.scratch = _clamp(float(value), -8, 8)
                deck.scratch_at = now
        elif op == "brake":
            if deck.playing:
                kind = "spinback" if body.get("spinback") else "brake"
                deck.brake = (kind, deck.base_rate(), float(body.get("seconds") or 1.2), 0.0)
        elif op == "bpm_scale":
            factor = float(value or 1)
            if factor in (0.5, 2.0):
                scale = deck.meta.get("bpm_scale", 1.0) * factor
                deck.meta["bpm_scale"] = _clamp(scale, 0.25, 4)
        elif op == "eject":
            if deck.playing and self._audible(deck):
                raise ValueError(f"Deck {deck.name} is on air.")
            deck.audio = None
            deck.stems = None
            deck.extra = None
            deck.stems_status = None
            deck.load_token += 1
            deck.info = {}
            deck.meta = {}
            deck.wave = None
            deck.wave_id += 1
            deck.reset_track_state()
        elif op == "flag":
            name = body.get("name")
            if name in ("slip", "quantize", "vinyl", "reverse"):
                current = getattr(deck, name)
                new = (not current) if value is None else bool(value)
                setattr(deck, name, new)
                if name in ("slip", "reverse") and not new:
                    deck.end_slip()
        elif op == "set":
            self._set_deck(deck, str(body.get("param") or ""), value)
        elif op == "set_mixer":
            self._set_mixer(str(body.get("param") or ""), value)
        elif op == "fx":
            fx = engine.fx
            if body.get("type") in FX_TYPES and body["type"] != fx.type:
                fx.set_on(False, engine.clock()[0])
                fx.tail = 0
                fx.type = body["type"]
            if "beat" in body:
                fx.beat = int(_clamp(int(body["beat"]), 0, len(FX_BEATS) - 1))
            if "level" in body:
                fx.level = _clamp(float(body["level"]), 0, 1)
            if body.get("target") in ("master", "A", "B"):
                fx.target = body["target"]
            if "on" in body:
                fx.set_on(bool(body["on"]), engine.clock()[0])
        elif op == "sample":
            name = body.get("name")
            if name in SAMPLES:
                engine.pad(name)
        elif op == "mix":
            self._manual_mix(body)
        elif op == "cancel_mix":
            engine.transition = None
        else:
            raise ValueError(f"Unknown DJ control {op!r}.")

    def _set_deck(self, deck, param, value):
        engine = self.engine
        tr = engine.transition
        if tr and tr.started and deck in (tr.out, tr.inn) and (param in (
                "eq_low", "color", "fader") or param.startswith("stem_")):
            engine.transition = None  # a human took over
        if param.startswith("stem_") and param[5:] in stems.ALL_CONTROLS:
            if not deck.use_stems:
                raise ValueError("Stems are switched off for this room.")
            # One part only: muting "other" leaves a separate guitar / piano playing.
            deck.stem_gain.set_one(param[5:], _clamp(float(value), 0, 1))
        elif param in ("eq_hi", "eq_mid", "eq_low"):
            deck.eq[param[3:]] = _clamp(float(value), KILL_DB, 6.0)
        elif param == "color":
            deck.color = _clamp(float(value), -1, 1)
        elif param == "color_fx" and value in COLOR_FX:
            deck.color_fx = value
        elif param == "trim":
            deck.trim = _clamp(float(value), -12, 12)
        elif param == "fader":
            deck.fader = _clamp(float(value), 0, 1)
        elif param == "pitch":
            deck.pitch = _clamp(float(value), -deck.range, deck.range)
            if deck.sync and engine.master() is not deck:
                deck.sync = False
            if engine.tempo_release and engine.tempo_release[0] is deck:
                engine.tempo_release = None
        elif param == "range" and int(value) in (6, 8, 16, 50):
            deck.range = int(value)
            deck.pitch = _clamp(deck.pitch, -deck.range, deck.range)
        elif param == "xf" and value in ("A", "THRU", "B"):
            deck.xf = value
        else:
            raise ValueError(f"Unknown deck setting {param!r}.")

    def _set_mixer(self, param, value):
        engine = self.engine
        if param == "xfader":
            if engine.transition and engine.transition.started:
                engine.transition = None
            engine.xfader = _clamp(float(value), -1, 1)
        elif param == "xf_curve" and value in ("smooth", "sharp"):
            engine.xf_curve = value
        elif param == "master":
            engine.master_volume = _clamp(float(value), 0, 1.2)
        elif param == "sampler":
            engine.sampler_volume = _clamp(float(value), 0, 1)
        else:
            raise ValueError(f"Unknown mixer setting {param!r}.")

    def _manual_mix(self, body):
        """MIX button: blend from the deck on air into the other one."""
        engine = self.engine
        playing = [d for d in engine.decks.values() if d.playing]
        if not playing:
            raise ValueError("Start a deck first, then mix into the other one.")
        out = engine.master() if engine.master() in playing else playing[0]
        target = body.get("to")
        inn = engine.decks.get(target) if target in DECKS else engine.decks["B" if out.name == "A" else "A"]
        if inn is out:
            raise ValueError("That deck is already playing.")
        if not inn.loaded:
            raise ValueError(f"Load a song on deck {inn.name} first.")
        style = body.get("style") if body.get("style") in STYLES else self.auto_style
        if style == "auto":
            style = choose_style(out, inn)
        beats = int(body.get("beats") or self.auto_beats or auto_beats(style, out, inn))
        if inn.bpm and out.bpm and not inn.playing:
            ratio = mixer.tempo_ratio(out.live_bpm(), inn.bpm)
            inn.sync = bool(ratio and abs(ratio - 1) <= 0.08)
        start_beat = None
        if out.bpm and not body.get("now"):
            start_beat = math.ceil(out.beat_at(out.seconds) / 4 + 1e-6) * 4  # next bar
        engine.transition = Transition(out, inn, style, beats, start_beat)

    def state(self, full=False):
        state = self.engine.state(full)
        state.update({
            "active": True,
            "kind": self.kind,
            "live": self.live,
            "auto": {"on": self.auto, "style": self.auto_style, "beats": self.auto_beats,
                     "mode": self.mode, "vibe": self.vibe, "three": self.three, "ai": self.ai,
                     "real": self.real.state() if self.real else None},
            "stems": {"on": self.stems_on, "available": stems.enabled()},
            "paused": self.paused,
            "crate": self.crate.items()[:60],
            "history": self.history[:20],
            "notice": self.notice,
            "server_time": time.time(),
        })
        self.notice = None
        return state

    # ---- skip (the room's skip button) ----

    async def skip(self):
        """Mix into the next song now: a short mix (at most SKIP_BEATS) from the next bar.

        A mix already under way finishes at once. With nothing ready on the
        other deck the next song is loaded first (Real DJ's pick in Real DJ
        mode) while the current one keeps playing.
        """
        if self.paused:
            self.resume()
        engine = self.engine
        with engine.lock:
            tr = engine.transition
            if tr and tr.started and not tr.done:
                engine._finish_transition(tr)
                return
            playing = [d for d in engine.decks.values() if d.playing and d.name in DECKS]
            if not playing:
                raise ValueError("Nothing is playing to skip.")
            out = engine.master() if engine.master() in playing else playing[0]
            inn = engine.decks["B" if out.name == "A" else "A"]
            engine.transition = None          # a mix planned for later is replaced by this one
            ready = inn.loaded and not inn.ended and not inn.playing
        if not ready or self._loading(inn.name):
            if not self._loading(inn.name):
                if self.mode == "real" and self.real is not None:
                    await self.real._load_next(inn, out)
                else:
                    item = await self._next_item()
                    if not item:
                        raise ValueError("Nothing is queued to mix into.")
                    self.load_in_background(inn.name, item)
            task = self.load_tasks.get(inn.name)
            if task is None:
                raise ValueError("Nothing is queued to mix into.")
            self.notice = "Skipping: loading the next song..."
            try:
                await asyncio.wait_for(asyncio.shield(task), 90)
            except asyncio.TimeoutError:
                raise ValueError("The next song is taking too long to load.") from None
        with engine.lock:
            if not out.playing or not inn.loaded or inn.playing:
                return                         # the set moved on by itself meanwhile
            if self.real is not None:
                self.real.plan = None          # Real DJ plans again after this mix
            style = choose_style(out, inn)
            beats = min(STYLES[style][1], SKIP_BEATS)
            self._manual_mix({"to": inn.name, "style": style, "beats": beats})
        self.notice = f"Skipping: {STYLES[style][0].lower()} into {inn.info.get('title') or 'the next song'}"
        logger.info("DJ %s: skip, %s into %r over %s beats", self.key, style, inn.info.get("title"), beats)

    # ---- pause / resume ----

    def pause(self):
        """Freeze the whole set: both decks, the mix in progress, hook loops,
        scratches. resume() carries on exactly from there."""
        engine = self.engine
        with engine.lock:
            if self.paused:
                return
            self.paused_decks = [d.name for d in engine.decks.values() if d.playing]
            for d in engine.decks.values():
                d.playing = False
            engine.paused = True
            self.paused = True
        self.notice = "Paused. Resume picks the set up right here."
        self.save_state(force=True)

    def resume(self):
        engine = self.engine
        with engine.lock:
            if not self.paused:
                return
            for name in self.paused_decks:
                deck = engine.decks.get(name)
                if deck and deck.loaded and not deck.ended:
                    deck.playing = True
            self.paused_decks = []
            engine.paused = False
            self.paused = False
        self.save_state(force=True)

    # ---- surviving a restart ----

    def snapshot(self):
        """What it takes to bring this booth back: the room, the settings, each
        deck's song and where it was, and which one had the room."""
        engine = self.engine
        with engine.lock:
            on_air = engine.master_name
            tr = engine.transition
            if tr and tr.started and not tr.done:
                # Mid-mix: come back on whichever song had the room more.
                on_air = tr.inn.name if tr.progress >= 0.5 else tr.out.name
            decks = {}
            for name in DECKS:
                d = engine.decks[name]
                if not d.loaded or not d.info.get("title") or d.ended:
                    continue
                playing = d.playing or name in self.paused_decks
                decks[name] = {"item": {k: d.info.get(k) for k in ("title", "artist", "thumbnail", "query", "keys")},
                               "position": round(d.seconds, 2),
                               "playing": bool(playing and name == on_air)}
        snap = {"kind": self.kind, "auto": self.auto, "mode": self.mode, "vibe": self.vibe,
                "three": self.three, "ai": self.ai, "stems": self.stems_on, "paused": self.paused,
                "decks": decks, "on_air": on_air if on_air in decks else None, "saved_at": time.time()}
        if self.kind == "huddle":
            import huddle
            snap["channel_id"] = huddle.channel_id_from(self.key)
            snap["used"] = sorted(str(u) for u in self.crate.used if u is not None)
        return snap

    def save_state(self, force=False):
        """Write this booth's snapshot (every few seconds while it runs)."""
        if self.kind not in ("huddle", "discord") or not self.active or self.restoring:
            return
        now = time.monotonic()
        if not force and now - self.saved_at < 5:
            return
        self.saved_at = now
        try:
            snap = self.snapshot()
        except Exception:
            logger.debug("DJ %s: snapshot failed", self.key, exc_info=True)
            return
        queue_state(self.key, snap)

    async def restore(self, snap):
        """Pick a set back up from a snapshot (after a restart)."""
        self.restoring = True
        try:
            await self._restore(snap)
        finally:
            self.restoring = False

    async def _restore(self, snap):
        if self.kind == "huddle":
            self.crate.used.update(snap.get("used") or [])
        if snap.get("mode") in ("auto", "real"):
            self.mode = snap["mode"]
        if snap.get("vibe") in realdj.VIBES:
            self.vibe = snap["vibe"]
        self.three = bool(snap.get("three"))
        self.set_ai(bool(snap.get("ai")))
        self.auto = bool(snap.get("auto", True))
        if snap.get("stems") is not None and bool(snap["stems"]) != self.stems_on:
            self.set_stems(bool(snap["stems"]))
        self.real = realdj.RealDJ(self, vibe=self.vibe) if self.mode == "real" else None
        decks = snap.get("decks") or {}
        on_air = snap.get("on_air")
        paused = bool(snap.get("paused"))
        # The song that had the room first, so it's back on air soonest.
        for name in sorted(decks, key=lambda n: n != on_air):
            if name not in DECKS:
                continue
            d = decks[name]
            play = bool(d.get("playing")) and not paused
            try:
                # Loaded paused at the saved spot, then started: it carries on from
                # exactly there (loading time isn't skipped over).
                await self.load(name, d["item"], position=d.get("position") if name == on_air else None)
                if play:
                    with self.engine.lock:
                        self.engine.decks[name].playing = True
            except Exception as error:
                logger.warning("DJ %s: could not bring deck %s back (%r): %s", self.key, name,
                               (d.get("item") or {}).get("title"), error)
        with self.engine.lock:
            if on_air in DECKS and self.engine.decks[on_air].loaded:
                self.engine.master_name = on_air
                self.engine.xfader = _xfader_toward(on_air, 1.0)
            if paused and on_air in DECKS:
                self.paused_decks = [on_air]
                self.paused = True
                self.engine.paused = True
        self.notice = "Back after a restart: the set carries on where it was."
        logger.info("DJ %s: restored (%s mode, %s)", self.key, self.mode,
                    ", ".join(f"{n}: {(d.get('item') or {}).get('title')}" for n, d in decks.items()))

    def forget_state(self):
        queue_state(self.key, None)

    async def stop(self, forget=True):
        """Turn the booth off. forget=False keeps its snapshot (the bot is going
        down and will bring it back); an explicit stop forgets it."""
        self.active = False
        if forget:
            self.forget_state()
        if self.tick_task:
            self.tick_task.cancel()
        for task in (*self.load_tasks.values(), *self.stem_tasks.values()):
            task.cancel()
        sessions.pop(self.key, None)
        if self.on_stop:
            try:
                await self.on_stop()
            except Exception:
                logger.exception("DJ %s: clean-up failed", self.key)


def planned_mix(out_deck, in_deck):
    """The transition planned for this pair in a playlist (an AI-planned FLAC list), checked
    against the decks: {"style", "beats", "start_beat", "in_at"}, any of them missing.

    The plan rides on the outgoing song (deck.info["mix"], from the room queue). Its exit
    becomes a bar of the outgoing song while it is still ahead and the mix still fits before
    the song ends; its entry cues the incoming song on a bar."""
    spec = out_deck.info.get("mix") or {}
    if not isinstance(spec, dict):
        return {}
    plan = {}
    style = spec.get("booth")
    if style in STYLES and style != "auto":
        plan["style"] = style
        try:
            beats = int(spec.get("booth_beats") or 0)
        except (TypeError, ValueError):
            beats = 0
        if beats in (1, 2, 4, 8, 16, 32, 64):
            plan["beats"] = beats
    try:
        out_at = float(spec["out_at"]) if spec.get("out_at") is not None else None
        in_at = float(spec["in_at"]) if spec.get("in_at") is not None else None
    except (TypeError, ValueError):
        out_at = in_at = None
    if out_at is not None and out_deck.bpm:
        current = out_deck.beat_at(out_deck.seconds)
        beat = round(out_deck.beat_at(out_at) / 4) * 4
        length = plan.get("beats") or STYLES.get(plan.get("style") or "blend", ("", 32))[1]
        if beat > current + 0.5 and out_deck.time_of_beat(beat + length) <= out_deck.duration:
            plan["start_beat"] = beat
    if in_at is not None and 0 <= in_at < in_deck.duration - 10:
        plan["in_at"] = in_at
    return plan


def _keep_split(audio, result, keys, title):
    """After a booth split (in a worker thread): make the song reusable.

    Saves the karaoke instrumental beside the stems and indexes the song's
    keys. With both, the next load rebuilds the exact same audio from the cache
    (stems.local_audio) instead of downloading and splitting it again, and the
    Oracle upgrade (huddle-ops stems-sync) can redo it in high quality.
    Returns the song's guitar / piano stems if it already has them.
    """
    digest = stems.track_hash(audio)
    try:
        if not os.path.exists(stems.instrumental_path(digest)):
            stems.write_instrumental(audio, result, digest)
    except Exception as error:
        logger.warning("DJ: could not save the instrumental for %r: %s", title, error)
    if keys:
        stems.remember(keys, digest, title)
    return stems.extras(audio=audio)


def huddle_karaoke(channel_id):
    import huddle
    settings = huddle.settings_for(huddle.PREFIX + channel_id)
    return bool(settings.get("karaoke_mode") or settings.get("audio_filter") == "karaoke")


def load_settings():
    """Per-room booth settings, e.g. {"<guild key>": {"stems": false}}."""
    try:
        with open(SETTINGS_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(settings):
    tmp = SETTINGS_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(settings, f, indent=2)
        os.replace(tmp, SETTINGS_FILE)
    except OSError as error:
        logger.warning("Could not save DJ settings: %s", error)


# Snapshots are written by a background thread: the disk is slow at times
# (the watchdog caught this file stalling the audio loop for seconds), and
# the loop that feeds the room's audio must never wait for it.
_state_lock = threading.Lock()        # guards _state_pending
_state_io = threading.Lock()          # one reader-modify-writer of STATE_FILE at a time
_state_pending = {}                   # session key -> snapshot, or None to drop it
_state_wake = threading.Event()
_state_thread = None


def queue_state(key, snap):
    """Save (or, with None, drop) one booth's snapshot without waiting for the disk."""
    global _state_thread
    with _state_lock:
        _state_pending[key] = snap
        if _state_thread is None or not _state_thread.is_alive():
            _state_thread = threading.Thread(target=_state_writer, name="dj-state", daemon=True)
            _state_thread.start()
    _state_wake.set()


def _state_writer():
    while True:
        _state_wake.wait()
        _state_wake.clear()
        flush_state()


def flush_state():
    """Write every queued snapshot now (also at exit)."""
    with _state_io:
        with _state_lock:
            pending = dict(_state_pending)
            _state_pending.clear()
        if not pending:
            return
        data = _read_state_file()
        for key, snap in pending.items():
            if snap is None:
                data.pop(key, None)
            else:
                data[key] = snap
        write_state(data)


atexit.register(flush_state)


def read_state():
    """{session key: snapshot} of running booths (STATE_FILE), queued saves included."""
    flush_state()
    return _read_state_file()


def _read_state_file():
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(data):
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, STATE_FILE)
    except OSError as error:
        logger.warning("Could not save the DJ booth state: %s", error)


async def restore_sessions(cog, wait_seconds=90):
    """After the bot starts: bring back every booth that was running (Real DJ
    or Auto DJ, its settings, its decks), once its room is reachable again."""
    data = read_state()
    for key, snap in list(data.items()):
        age = time.time() - float(snap.get("saved_at") or 0)
        session = None
        if age <= RESTORE_MAX_AGE and key not in sessions:
            try:
                session = await _restore_one(cog, key, snap, wait_seconds)
            except Exception:
                logger.warning("DJ %s: could not restore the booth", key, exc_info=True)
        if session is None:
            queue_state(key, None)


async def _restore_one(cog, key, snap, wait_seconds):
    if snap.get("kind") == "huddle":
        import huddle_voice
        channel_id = snap.get("channel_id")
        for _ in range(int(wait_seconds)):
            manager = huddle_voice.MANAGER
            if manager and channel_id in (manager.players or {}):
                break
            await asyncio.sleep(1)
        else:
            logger.info("DJ %s: room not back within %ss, not restoring", key, wait_seconds)
            return None
        session = await start_huddle(channel_id, auto=bool(snap.get("auto", True)), restore=True)
    elif snap.get("kind") == "discord" and cog is not None:
        player = None
        for _ in range(int(wait_seconds)):
            guild = cog.bot.get_guild(int(key)) if cog.bot.is_ready() else None
            vc = guild.voice_client if guild else None
            if vc and vc.is_connected():
                player = cog.get_player(guild)
                break
            await asyncio.sleep(1)
        if player is None:
            logger.info("DJ %s: not back in voice within %ss, not restoring", key, wait_seconds)
            return None
        session = await start_discord(player, auto=bool(snap.get("auto", True)), restore=True)
    else:
        return None
    await session.restore(snap)
    return session


def catalog():
    """Static choices the booth UI draws."""
    return {
        "styles": [{"id": key, "label": label, "beats": beats} for key, (label, beats) in STYLES.items()],
        "fx_types": list(FX_TYPES),
        "fx_beats": ["1/8", "1/4", "1/2", "3/4", "1", "2", "4", "8"],
        "color_fx": list(COLOR_FX),
        "loop_beats": [1 / 4, 1 / 2, 1, 2, 4, 8, 16, 32],
        "samples": list(SAMPLES),
    }


def inactive_state():
    return {"active": False, "catalog": catalog()}


# --------------------------------------------------------------------------
# Discord output
# --------------------------------------------------------------------------

def _discord_source(engine):
    import discord

    class DJAudioSource(discord.AudioSource):
        def read(self):
            return engine.render()

        def is_opus(self):
            return False

    return DJAudioSource()


async def start_discord(player, auto=True, restore=False):
    """Put a guild's voice connection on the booth, taking over the current song."""
    key = str(player.guild.id)
    if key in sessions:
        session = sessions[key]
        session.auto = auto or session.auto
        return session
    vc = player.guild.voice_client
    if not vc or not vc.is_connected():
        raise RuntimeError("The bot is not in a voice channel. Use /join or /play in Discord first.")
    session = DJSession(key, "discord", DiscordCrate(player, player._resolve_local_file), auto=auto)
    session.restoring = restore
    sessions[key] = session
    song = player.current
    carry = None
    if song and (vc.is_playing() or vc.is_paused()) and not restore:
        # The song on air keeps playing from deck A.
        carry = {"item": {**session.crate._item(song), "song": song},
                 "position": player.get_playback_position_seconds(),
                 "play": vc.is_playing()}

    async def go_live():
        player.cancel_automix()
        player.cancel_autoplay_prefetch()
        player.clear_preloads()
        player._suppress_after = True
        try:
            if vc.is_playing() or vc.is_paused():
                vc.stop()
            if player.current:
                player.history.appendleft(player.current)
            player.current = None
            player.reset_playback_clock()
            vc.play(_discord_source(session.engine), after=after)
            session.live = True
            await asyncio.sleep(0.5)
        finally:
            player._suppress_after = False

    def after(error):
        # Something else (/skip, /stop, a disconnect) ended the booth's stream.
        if session.active:
            session.on_stop = None
            player.current = None      # the booth's song already went to history
            player.reset_playback_clock()
            asyncio.run_coroutine_threadsafe(session.stop(), player.bot.loop)
            asyncio.run_coroutine_threadsafe(player.play_next(), player.bot.loop)

    async def report_on_air(info):
        """Keep the guild player's "current song" on the booth's on-air deck, so
        /nowplaying, /lyrics, /lyricsnow, karaoke and the dashboard follow it."""
        if not info:
            return
        deck = session.engine.decks[info["id"].split(":")[1]]
        song = deck.info.get("song")
        if song is None:
            # Loaded from the booth's search: no queue entry to show, make one.
            song_type = sys.modules[type(player).__module__].Song
            seconds = int(info["duration"] or 0)
            song = song_type(title=info["title"] or "Unknown", url=deck.info.get("query") or "",
                             duration=f"{seconds // 60}:{seconds % 60:02d}",
                             requester=player.guild.me, source_type="youtube",
                             thumbnail=info["thumbnail"], artist=info["artist"] or None)
            deck.info["song"] = song
        changed = player.current_song_key != info["id"]
        player.current = song
        player.current_song_key = info["id"]
        now = time.monotonic()
        player.song_started_at = now - info["positionMs"] / 1000
        player.total_paused_seconds = 0.0
        player.paused_started_at = now if info["paused"] else None
        if changed and player.last_message_channel:
            cog = player.bot.get_cog("MusicCog")
            if cog:
                asyncio.create_task(cog._send_now_playing_update(player))

    async def on_stop():
        player.current = None          # the booth's song already went to history
        player.reset_playback_clock()
        player._suppress_after = True
        try:
            if vc.is_connected() and (vc.is_playing() or vc.is_paused()):
                vc.stop()
            await asyncio.sleep(0.3)
        finally:
            player._suppress_after = False
        await player.play_next()

    session.on_stop = on_stop
    session.report_on_air = report_on_air
    session.karaoke_setting = lambda: player.karaoke_mode
    if carry:
        started = time.monotonic()

        async def takeover():
            try:
                await session.load("A", carry["item"], position=carry["position"],
                                   play=carry["play"], started=started)
            except Exception as error:
                session.notice = f"Couldn't carry the current song over: {error}"
            if session.active:
                with session.engine.lock:
                    session.engine.xfader = -1.0
                    session.engine.master_name = "A"
                await go_live()
        session.load_tasks["A"] = asyncio.create_task(takeover())
    else:
        await go_live()
    session.start_ticking()
    return session


# --------------------------------------------------------------------------
# Huddle output (huddle_voice.RoomAudioTrack pulls engine.render())
# --------------------------------------------------------------------------

async def start_huddle(channel_id, auto=True, restore=False):
    import huddle
    import huddle_voice
    key = huddle.PREFIX + channel_id
    if key in sessions:
        session = sessions[key]
        session.auto = auto or session.auto
        return session
    session = DJSession(key, "huddle", HuddleCrate(channel_id), auto=auto)
    session.restoring = restore            # held until restore() has put the decks back
    sessions[key] = session
    manager = huddle_voice.MANAGER
    player = (manager.players.get(channel_id) if manager else None) or {}
    track = player.get("track")

    async def pause_hub():
        try:
            await huddle._request("POST", "/api/bot/player", {
                "channelId": channel_id, "action": {"name": "pause"}})
        except Exception as error:
            logger.debug("DJ could not pause the Huddle player: %s", error)

    async def report_on_air(info):
        await huddle._request("POST", "/api/bot/player", {
            "channelId": channel_id, "action": {"name": "live", "live": info}})

    async def on_stop():
        # Hand the room back to its normal player, past the song DJ took.
        try:
            await report_on_air(None)
        except Exception as error:
            logger.debug("DJ could not clear the on-air track: %s", error)
        try:
            current = (manager.players.get(channel_id) if manager else None) or {}
            if current.get("track") and current["track"].get("id") in session.crate.used:
                await huddle._request("POST", "/api/bot/player", {
                    "channelId": channel_id, "action": {"name": "skip"}})
            if current.get("track"):
                await huddle._request("POST", "/api/bot/player", {
                    "channelId": channel_id, "action": {"name": "resume"}})
        except Exception as error:
            logger.debug("DJ hand-back failed: %s", error)

    session.on_stop = on_stop
    session.pause_hub = pause_hub
    session.report_on_air = report_on_air
    session.karaoke_setting = lambda: huddle_karaoke(channel_id)
    if track and track.get("audioUrl") and not player.get("paused") and not restore:
        position = float(player.get("positionMs") or 0) / 1000 + max(
            0.0, time.time() - float(player.get("updatedAt") or 0) / 1000)
        session.crate.used.add(track.get("id"))
        started = time.monotonic()

        async def takeover():
            try:
                await session.load("A", HuddleCrate._item(track), position=position,
                                   play=True, started=started)
                with session.engine.lock:
                    session.engine.xfader = -1.0
                    session.engine.master_name = "A"
            except Exception as error:
                session.notice = f"Couldn't carry the current song over: {error}"
            session.live = True
            await pause_hub()
        session.load_tasks["A"] = asyncio.create_task(takeover())
    else:
        session.live = True
        if track and not player.get("paused"):
            await pause_hub()
    session.start_ticking()
    return session
