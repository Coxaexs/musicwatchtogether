"""The DJ booth's sampler sounds, synthesised at start-up (no sample files).

Built the way the real ones are made rather than as test tones:

  horn    an air horn: three buzzy horn pipes in a chord, a pitch scoop into
          each blast, the horn's nasal formants, "bap bap bap baaaap" and a
          hall tail
  siren   a dub siren: a whooping oscillator through a resonant filter and a
          tape echo
  boom    an 808: a sine that drops in pitch, a click on the front, driven
          into gentle distortion so it reads on small speakers too
  clap    four noise bursts a few milliseconds apart and a short room tail
  riser   a supersaw and filtered noise climbing over four seconds
  laser   FM zaps falling in pitch, bounced left and right
  rewind  a tape-style rewind (the booth plays a real one on the deck when a
          song is on; this is for when nothing is)
  scratch a scratch on a vocal-ish sound (likewise: a real one when a song is on)

Every sound is float32 stereo at 48 kHz, peaking at about -1 dBFS before the
sampler volume.
"""

import numpy as np
from scipy import signal

SR = 48_000


def _t(seconds):
    return np.arange(int(seconds * SR)) / SR


def _band_limited(phase, harmonics, falloff):
    """Sum of harmonics of a phase in cycles: falloff(k) is harmonic k's level."""
    out = np.zeros_like(phase)
    for k in range(1, harmonics + 1):
        out += falloff(k) * np.sin(2 * np.pi * k * phase)
    return out


def _saw(freq, seconds=None, phase0=0.0):
    """A band-limited sawtooth following a frequency array (no aliasing)."""
    freq = np.asarray(freq, dtype=np.float64)
    phase = phase0 + np.cumsum(freq) / SR
    top = int(min(60, 18000 / max(40.0, float(freq.max()))))
    return _band_limited(phase, top, lambda k: (-1) ** (k + 1) / k) * (2 / np.pi)


def _pulse(freq, duty=0.3, phase0=0.0):
    """A band-limited pulse wave: the buzzy, reedy sound of a horn pipe."""
    freq = np.asarray(freq, dtype=np.float64)
    phase = phase0 + np.cumsum(freq) / SR
    top = int(min(60, 16000 / max(40.0, float(freq.max()))))
    return _band_limited(phase, top, lambda k: np.sin(np.pi * k * duty) / k) * (4 / np.pi)


def _peak(x, freq, q, gain_db):
    """A peaking EQ band (RBJ biquad)."""
    a = 10 ** (gain_db / 40)
    w = 2 * np.pi * freq / SR
    alpha = np.sin(w) / (2 * q)
    b = [1 + alpha * a, -2 * np.cos(w), 1 - alpha * a]
    den = [1 + alpha / a, -2 * np.cos(w), 1 - alpha / a]
    return signal.lfilter(b, den, x, axis=0)


def _filter(x, kind, freq, order=2):
    sos = signal.butter(order, freq, kind, fs=SR, output="sos")
    return signal.sosfilt(sos, x, axis=0)


def _reverb(mono, seconds, wet, rng, damping=4000.0, pre_delay=0.012):
    """A small stereo reverb: decaying noise impulse responses, different per side."""
    n = int(seconds * SR)
    t = np.arange(n) / SR
    out = []
    for _side in range(2):
        ir = rng.normal(0, 1, n) * np.exp(-6.9 * t / seconds)
        ir = _filter(ir, "lowpass", damping)
        ir = np.concatenate([np.zeros(int(pre_delay * SR)), ir])
        ir /= np.sqrt((ir ** 2).sum()) + 1e-9
        out.append(signal.fftconvolve(mono, ir)[: len(mono) + len(ir)])
    tail = np.stack(out, axis=1)
    dry = np.zeros_like(tail)
    dry[: len(mono)] = mono[:, None]
    return dry * (1 - wet) + tail * wet


def _finish(stereo, peak_db=-1.0):
    x = np.asarray(stereo, dtype=np.float64)
    if x.ndim == 1:
        x = np.stack([x, x], axis=1)
    # Fade the very end so nothing clicks, then set the level.
    fade = min(len(x), int(0.01 * SR))
    x[-fade:] *= np.linspace(1, 0, fade)[:, None]
    peak = np.abs(x).max() or 1.0
    return (x / peak * 10 ** (peak_db / 20)).astype(np.float32)


# --------------------------------------------------------------------------

def air_horn(rng):
    """BAP BAP BAP BAAAAAP: three pipes (a buzzy major chord) blowing together."""
    pipes = (311.1, 392.0, 466.2)            # E flat, G, B flat: the classic "truck" chord
    blasts = []
    for length in (0.12, 0.12, 0.12, 0.9):
        t = _t(length)
        tone = np.zeros(len(t))
        for i, f0 in enumerate(pipes):
            # Each blast scoops up into pitch, with a little breath wobble.
            scoop = 1 - 0.035 * np.exp(-t / 0.035)
            wobble = 1 + 0.003 * np.sin(2 * np.pi * (5.5 + i) * t + i)
            tone += _pulse(f0 * scoop * wobble, duty=0.28 + 0.04 * i, phase0=rng.random())
        tone /= len(pipes)
        attack = np.minimum(1, t / 0.008)
        release = np.minimum(1, (length - t) / (0.025 if length < 0.5 else 0.12))
        blasts.append(tone * attack * release)
        blasts.append(np.zeros(int(0.045 * SR)))
    horn = np.concatenate(blasts)
    horn = np.tanh(horn * 2.5)               # an air horn is loud and squashed
    horn = _filter(horn, "highpass", 220)
    horn = _peak(horn, 1150, 1.4, 7)          # the honk
    horn = _peak(horn, 2600, 2.0, 4)
    horn = _filter(horn, "lowpass", 7500)
    return _finish(_reverb(horn, 1.1, 0.22, rng, damping=5000))


def dub_siren(rng):
    """A whooping dub siren through a resonant filter and a tape echo."""
    t = _t(1.6)
    lfo = 2 * np.abs(2 * ((t * 2.6) % 1) - 1) - 1          # triangle
    freq = 880 * 2 ** (lfo * 0.75)                         # up and down three-quarters of an octave
    tone = 0.6 * _saw(freq) + 0.4 * np.sign(np.sin(2 * np.pi * np.cumsum(freq) / SR)) * 0.6
    tone = _filter(tone, "lowpass", 3800)
    tone = _peak(tone, 1400, 3.0, 6)
    tone *= np.minimum(1, t / 0.02) * np.minimum(1, (t[-1] - t) / 0.08)
    # Tape echo: dotted-eighth repeats at 120 bpm, darker each time.
    delay = int(0.375 * SR)
    out = np.concatenate([tone, np.zeros(delay * 5)])
    echo = np.zeros_like(out)
    tap = out.copy()
    for k in range(1, 6):
        tap = np.concatenate([np.zeros(delay), tap[:-delay]]) * 0.5
        tap = _filter(tap, "lowpass", 3000 - 300 * k)
        echo += tap
    return _finish(_reverb(out + echo, 0.8, 0.15, rng))


def boom_808(rng):
    """An 808 kick-bass: a falling sine with a click and some drive."""
    t = _t(1.8)
    freq = 48 + 110 * np.exp(-t / 0.045)
    body = np.sin(2 * np.pi * np.cumsum(freq) / SR) * np.exp(-t / 0.75)
    click = _filter(rng.normal(0, 1, len(t)) * np.exp(-t / 0.002), "highpass", 2500) * 0.35
    boom = np.tanh((body + click) * 2.2) / np.tanh(2.2)
    boom = _filter(boom, "lowpass", 9000)
    return _finish(boom, -1.5)


def clap(rng):
    """Four noise bursts a few milliseconds apart, then a room tail."""
    t = _t(0.5)
    shape = np.zeros(len(t))
    for i, offset in enumerate((0.0, 0.009, 0.019, 0.027)):
        s = int(offset * SR)
        decay = 0.0045 if i < 3 else 0.09
        shape[s:] += np.exp(-t[: len(t) - s] / decay) * (0.8 if i < 3 else 1.0)
    noise = rng.normal(0, 1, len(t)) * shape
    hit = _filter(noise, "bandpass", [750, 2600], order=2)
    hit = _peak(hit, 1200, 1.5, 5)
    return _finish(_reverb(hit, 0.45, 0.25, rng, damping=6500, pre_delay=0.006))


def riser(rng):
    """Four seconds of tension: a detuned supersaw climbing two octaves and
    noise opening up through a sweeping band-pass, getting louder and wider."""
    seconds = 4.0
    t = _t(seconds)
    q = t / seconds
    freq = 110 * 4 ** (q ** 1.6)
    saws = np.zeros((len(t), 2))
    for i, detune in enumerate((-0.25, -0.14, -0.06, 0.0, 0.06, 0.14, 0.25)):
        side = i % 2
        saws[:, side] += _saw(freq * 2 ** (detune / 12), phase0=rng.random())
        saws[:, 1 - side] += 0.6 * _saw(freq * 2 ** (detune / 12), phase0=rng.random())
    saws /= 7
    noise = rng.normal(0, 1, (len(t), 2))
    swept = np.zeros_like(noise)
    chunk = SR // 50
    zi = None
    for start in range(0, len(t), chunk):
        centre = 400 * 22 ** (start / len(t))
        sos = signal.butter(2, [centre / 1.6, min(20000, centre * 1.6)], "bandpass", fs=SR, output="sos")
        if zi is None:
            zi = np.zeros((sos.shape[0], 2, 2))
        swept[start:start + chunk], zi = signal.sosfilt(sos, noise[start:start + chunk], axis=0, zi=zi)
    level = (q ** 2.2)[:, None]
    mix = (saws * 0.55 + swept * 1.3) * level
    mix = _filter(mix, "highpass", 90)
    return _finish(mix, -2.0)


def laser(rng):
    """Three FM zaps falling in pitch, alternating left and right."""
    zaps = []
    for i in range(3):
        t = _t(0.15)
        carrier = 3000 * np.exp(-t / 0.05) + 260
        index = 3.5 * np.exp(-t / 0.04)
        mod = np.sin(2 * np.pi * np.cumsum(carrier * 1.5) / SR)
        zap = np.sin(2 * np.pi * np.cumsum(carrier) / SR + index * mod)
        zap *= np.minimum(1, t / 0.002) * np.exp(-t / 0.06)
        stereo = np.zeros((len(t), 2))
        stereo[:, i % 2] = zap
        stereo[:, 1 - i % 2] = zap * 0.45
        zaps.append(stereo)
    return _finish(np.concatenate(zaps), -2.0)


def _vocal_like(rng, seconds):
    """A short sung "ahh": a pulse with formants, to scratch when no song plays."""
    t = _t(seconds)
    voice = _pulse(220 * (1 + 0.01 * np.sin(2 * np.pi * 5 * t)), duty=0.15)
    voice = _peak(voice, 800, 4, 12)
    voice = _peak(voice, 1150, 5, 9)
    voice = _peak(voice, 2900, 6, 6)
    return voice * np.minimum(1, t / 0.01)


def _play_at_rate(source, rates):
    """Read `source` with a varying playback rate (negative = backwards)."""
    pos = np.clip(np.cumsum(rates), 0, len(source) - 2)
    i = pos.astype(int)
    frac = pos - i
    return source[i] * (1 - frac) + source[i + 1] * frac


def tape_rewind(rng):
    """A rewind: a voice spun backwards fast, slowing as it goes."""
    voice = _vocal_like(rng, 2.0)
    t = _t(0.9)
    rates = -np.interp(t, [0, 0.08, 0.9], [0, 3.6, 1.2])
    start = np.full(len(t), 0.0)
    start[0] = len(voice) - 2
    out = _play_at_rate(voice, start + rates)
    return _finish(_filter(out, "highpass", 120) * np.minimum(1, (t[-1] - t) / 0.1))


def scratch(rng):
    """Two baby scratches on a voice: forward and back across the start of a word."""
    voice = _vocal_like(rng, 1.0)
    t = _t(0.55)
    # Hand position: over and back twice, so the rate is its derivative.
    position = 0.12 * SR * (1 - np.cos(2 * np.pi * t / 0.27)) / 2
    rates = np.diff(np.concatenate([[0.0], position]))
    out = _play_at_rate(voice, np.concatenate([[int(0.05 * SR)], rates[1:]]))
    return _finish(_filter(out, "highpass", 150))


BUILDERS = {
    "horn": air_horn, "siren": dub_siren, "rewind": tape_rewind, "laser": laser,
    "boom": boom_808, "clap": clap, "riser": riser, "scratch": scratch,
}


def build(seed=7):
    """Every sampler sound: {name: float32 (n, 2)}."""
    rng = np.random.default_rng(seed)
    return {name: make(rng) for name, make in BUILDERS.items()}
