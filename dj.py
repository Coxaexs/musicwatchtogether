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
import base64
import logging
import math
import subprocess
import threading
import time

import numpy as np
from scipy import signal

import mixer

logger = logging.getLogger("MusicBot.DJ")

SR = 48_000
BLOCK = 960                      # 20 ms, one Discord/Opus frame
FRAME_BYTES = BLOCK * 2 * 2      # s16le stereo
MAX_TRACK_SECONDS = 15 * 60
WAVE_RATE = 20                   # waveform bins per second of audio
KILL_DB = -26.0                  # EQ knob fully left = band killed
DECKS = ("A", "B")

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
}
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


def sample_bank():
    global _SAMPLE_BANK
    if _SAMPLE_BANK is None:
        _SAMPLE_BANK = _synth_samples()
    return _SAMPLE_BANK


# --------------------------------------------------------------------------
# Loading: decode a whole track and analyse it
# --------------------------------------------------------------------------

def decode_track(source):
    """Whole track as int16 (n, 2) at 48 kHz."""
    command = ["ffmpeg", "-nostdin", "-loglevel", "error"]
    if str(source).startswith(("http://", "https://")):
        command += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "3"]
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
        if self.scratch is not None:
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
        a = self.audio[index].astype(np.float32)
        b = self.audio[index + 1].astype(np.float32)
        out = (a + (b - a) * frac) * (valid[:, None] / 32768.0)
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
        }
        if full:
            result["wave"] = self.wave
        return result


# --------------------------------------------------------------------------
# Auto transitions
# --------------------------------------------------------------------------

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
        # Brake/spinback stop the old track before the new one drops.
        self.in_delay = 2 if style in ("brake", "spinback") else 0

    @property
    def progress(self):
        return _clamp(self.elapsed / self.beats, 0.0, 1.0)

    def json(self):
        return {"from": self.out.name, "to": self.inn.name, "style": self.style,
                "label": self.label, "beats": self.beats, "started": self.started,
                "progress": round(self.progress, 3),
                "starts_in_beats": (None if self.started or self.start_beat is None
                                    else round(self.start_beat - self.out.beat_at(self.out.seconds), 2))}


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
    if keys_ok and min(energy_out, energy_in) > 0.45:
        return "blend"
    if energy_in - energy_out > 0.25:
        return "filter"
    return "fade"


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------

class DJEngine:
    def __init__(self):
        self.lock = threading.RLock()
        self.decks = {name: Deck(name) for name in DECKS}
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
                if self.fx.on and self.fx.target == out.name:
                    self.fx.set_on(False, self.clock()[0])
            if p > 0.5:
                self.xfader = _xfader_toward(side, 0.5 + 0.5 * _smooth((p - 0.5) / 0.5))
        elif style in ("brake", "spinback"):
            if not inn.playing and tr.elapsed >= tr.in_delay:
                self._drop_in(inn)
                self.xfader = _xfader_toward(side, 1.0)
        # "cut" happens entirely in _start_transition
        if p >= 1:
            self._finish_transition(tr)

    def _start_transition(self, tr):
        out, inn = tr.out, tr.inn
        tr.started = True
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
        if not inn.playing and not tr.in_delay:
            self._drop_in(inn)
        if tr.style == "echo":
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
            self.frames += 1
            self._sync_tempo()
            beat_seconds, beat_pos = self.clock()
            beat_step = BLOCK / SR / beat_seconds
            self._run_transition(beat_step)
            self._run_tempo_release(beat_step)
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
#: async (query) -> {"audio_url", "page_url", "title"?, ...}; set by webui
resolver = None


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
        return self._item(song)

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
                "keys": [mixer.track_key(track.get("pageUrl")), mixer.track_key(track.get("query"))]}

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
        self.auto_style = "auto"
        self.auto_beats = None           # None = the style's default
        self.history = []
        self.live = False                # engine is the room's output
        self.active = True
        self.started_at = time.time()
        self.notice = None
        self.load_tasks = {}
        self.tick_task = None
        self.on_stop = None              # platform clean-up, set by start_*()

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
        try:
            source = item.get("file") or item.get("audio_url")
            keys = [k for k in (item.get("keys") or []) if k]
            if not source:
                if not resolver:
                    raise RuntimeError("Song lookup is not available.")
                resolved = await resolver(item.get("query") or item.get("title"))
                source = resolved["audio_url"]
                keys.append(mixer.track_key(resolved.get("page_url")))
                item = {**item, "title": item.get("title") or resolved.get("title") or item.get("query"),
                        "artist": item.get("artist") or resolved.get("artist"),
                        "thumbnail": item.get("thumbnail") or resolved.get("thumbnail")}
            loop = asyncio.get_running_loop()
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
            deck.meta = dict(meta)
            deck.wave = wave
            deck.wave_id += 1
            deck.info = {"title": item.get("title"), "artist": item.get("artist"),
                         "thumbnail": item.get("thumbnail")}
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
        return self.crate._item(song) if song else None

    async def _tick(self):
        engine = self.engine
        with engine.lock:
            events, engine.events = engine.events, []
        if not self.auto or not self.live:
            return
        with engine.lock:
            decks = engine.decks
            playing = [d for d in decks.values() if d.playing]
            tr = engine.transition
        if not playing:
            # Nothing on air: start whatever is loaded, else pull a track in.
            ready = next((d for d in decks.values() if d.loaded and not d.ended), None)
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
            style = self.auto_style
            if style == "auto":
                style = choose_style(m, other)
            beats = self.auto_beats or STYLES[style][1]
            if other.bpm and m.bpm:
                ratio = mixer.tempo_ratio(m.live_bpm(), other.bpm)
                other.sync = bool(ratio and abs(ratio - 1) <= 0.08)
            if m.bpm:
                end_beat = m.beat_at(m.music_end())
                tail = beats if style not in ("echo", "cut", "brake", "spinback") else max(2, beats // 2)
                start_beat = math.floor((end_beat - tail) / 8) * 8
                current = m.beat_at(m.seconds)
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
        if op == "auto":
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
            bank = sample_bank()
            if name in bank:
                engine.voices.append([bank[name], 0])
                engine.voices = engine.voices[-6:]
        elif op == "mix":
            self._manual_mix(body)
        elif op == "cancel_mix":
            engine.transition = None
        else:
            raise ValueError(f"Unknown DJ control {op!r}.")

    def _set_deck(self, deck, param, value):
        engine = self.engine
        tr = engine.transition
        if tr and tr.started and deck in (tr.out, tr.inn) and param in (
                "eq_low", "color", "fader"):
            engine.transition = None  # a human took over
        if param in ("eq_hi", "eq_mid", "eq_low"):
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
        beats = int(body.get("beats") or self.auto_beats or STYLES[style][1])
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
            "auto": {"on": self.auto, "style": self.auto_style, "beats": self.auto_beats},
            "crate": self.crate.items()[:60],
            "history": self.history[:20],
            "notice": self.notice,
            "server_time": time.time(),
        })
        self.notice = None
        return state

    async def stop(self):
        self.active = False
        if self.tick_task:
            self.tick_task.cancel()
        for task in self.load_tasks.values():
            task.cancel()
        sessions.pop(self.key, None)
        if self.on_stop:
            try:
                await self.on_stop()
            except Exception:
                logger.exception("DJ %s: clean-up failed", self.key)


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


async def start_discord(player, auto=True):
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
    sessions[key] = session
    song = player.current
    carry = None
    if song and (vc.is_playing() or vc.is_paused()):
        # The song on air keeps playing from deck A.
        carry = {"item": session.crate._item(song),
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
            asyncio.run_coroutine_threadsafe(session.stop(), player.bot.loop)
            asyncio.run_coroutine_threadsafe(player.play_next(), player.bot.loop)

    async def on_stop():
        player._suppress_after = True
        try:
            if vc.is_connected() and (vc.is_playing() or vc.is_paused()):
                vc.stop()
            await asyncio.sleep(0.3)
        finally:
            player._suppress_after = False
        await player.play_next()

    session.on_stop = on_stop
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

async def start_huddle(channel_id, auto=True):
    import huddle
    import huddle_voice
    key = huddle.PREFIX + channel_id
    if key in sessions:
        session = sessions[key]
        session.auto = auto or session.auto
        return session
    session = DJSession(key, "huddle", HuddleCrate(channel_id), auto=auto)
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

    async def on_stop():
        # Hand the room back to its normal player, past the song DJ took.
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
    if track and track.get("audioUrl") and not player.get("paused"):
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
