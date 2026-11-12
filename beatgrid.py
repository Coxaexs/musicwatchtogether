"""A song's beat grid put exactly on its drums, once the song has stems.

The analysis reads tempo off the whole mix to a tenth of a bpm and takes the
beat's phase from wherever the mix is busiest. That is close enough to count
phrases, but two beat-matched songs need better: a tenth of a bpm is a
sixteenth note of drift over three minutes, and a grid sitting on the snare or
the bass line puts one song's kick between the other's.

refine() fits the grid to the kick drum instead: the low end of the drum stem
has little else in it. It only trusts a song that keeps time (a drum machine,
a click-tracked band). A band that breathes has no single tempo to find, and
its grid is left as it was, with `grid_steady` saying how well the grid holds
so a planner can keep long blends for songs that can take them.
"""

import logging

import numpy as np
from scipy import signal

logger = logging.getLogger("MusicBot.BeatGrid")

SR = 48_000
HOP = 240                        # 5 ms
STEADY = 4.0                     # how far the kick's beat stands out of the rest of the bar, to trust the grid
NEAR = 4                         # the beat is this many hops wide (25 ms)
SEARCH = 0.012                   # how far the tempo may move (either way)
MAX_SHIFT = 0.3                  # beats the grid may move


def kick_onsets(drums):
    """Onset strength of the kick, every 5 ms, from a drum stem (int16 (n, 2) at 48 kHz)."""
    mono = drums[::4].astype(np.float32).mean(axis=1) / 32768.0          # 12 kHz is plenty under 150 Hz
    low = signal.sosfilt(signal.butter(4, 150, "lowpass", fs=SR // 4, output="sos"), mono)
    hop = HOP // 4
    count = len(low) // hop
    if count < 400:
        return None
    env = np.sqrt((low[: count * hop].reshape(count, hop) ** 2).mean(axis=1))
    rise = np.maximum(0.0, np.diff(env, prepend=env[0]))
    floor = np.percentile(rise, 90)
    return np.maximum(0.0, rise - floor)


def _fold(onsets, period):
    """Onset strength folded onto one beat of `period` hops: (strength per hop of the beat)."""
    index = np.arange(len(onsets))
    phase = np.floor(index % period).astype(np.int64)
    return np.bincount(phase, weights=onsets, minlength=int(np.ceil(period)))


def _score(onsets, period):
    """(contrast, phase in hops): how much of the kick falls in the best 25 ms of the beat, against an even spread."""
    folded = _fold(onsets, period)
    window = np.convolve(np.concatenate([folded, folded[:NEAR]]), np.ones(NEAR + 1), mode="valid")
    best = int(np.argmax(window))
    even = folded.sum() * (NEAR + 1) / len(folded)
    return float(window[best] / (even + 1e-12)), best


def fit(onsets, bpm):
    """(bpm, first beat in seconds, steadiness) of the grid that collects the most kick onsets near `bpm`.
    Steadiness is how many times more kick lands on the beat than would by chance (1 = no better)."""
    total = float(onsets.sum())
    if total <= 0 or not bpm:
        return None
    hops_per_second = SR / HOP
    period0 = 60.0 / bpm * hops_per_second
    best = None
    span = SEARCH
    centre = period0
    # coarse to fine: 0.05% steps, then 0.005%
    for step in (5e-4, 5e-5):
        for k in np.arange(-span, span + step / 2, step):
            period = centre * (1 + k)
            score, phase = _score(onsets, period)
            if best is None or score > best[0]:
                best = (score, period, phase)
        centre, span = best[1], step
    score, period, phase = best
    # the onset's centre of mass in the winning window, for a first beat finer than a hop
    folded = _fold(onsets, period)
    idx = (phase + np.arange(NEAR + 1)) % len(folded)
    weights = folded[idx]
    centre_hop = phase + float((weights * np.arange(NEAR + 1)).sum() / (weights.sum() + 1e-12))
    return float(60.0 * hops_per_second / period), float(centre_hop / hops_per_second), float(score)


def refine(meta, drums):
    """`meta` with its tempo and first beat fitted to the drum stem's kick, when the song keeps time.

    Always sets "grid_steady" (how many times more of the kick lands on the beat than would by chance). The grid moves by less than half
    a beat, so beat 0 stays the bar and phrase start it was (realdj.aligned_grid). Never raises.
    """
    try:
        scale = float(meta.get("bpm_scale") or 1.0)
        bpm = float(meta.get("bpm") or 0) * scale
        if not bpm or (meta.get("bpm_confidence") or 0) < 0.15 or drums is None:
            return meta
        onsets = kick_onsets(drums)
        if onsets is None:
            return meta
        fitted = fit(onsets, bpm)
        if fitted is None:
            return meta
        new_bpm, phase, steady = fitted
        meta = dict(meta, grid_steady=round(steady, 2))
        if steady < STEADY:
            return meta
        beat = 60.0 / new_bpm
        first = float(meta.get("first_beat") or 0.0)
        # The kick's phase nearest the grid's own first beat. The drift over the song's first seconds is nothing,
        # so the old first beat still says which beat is beat 0.
        shift = float(((phase - first) / beat + 0.5) % 1.0 - 0.5)
        if abs(shift) > MAX_SHIFT:
            return meta
        fix = dict(meta.get("grid_fix") or {}, kick_tempo=round(new_bpm / bpm - 1, 5), kick_shift=round(shift, 3))
        return dict(meta, bpm=round(new_bpm / scale, 4), first_beat=round(first + shift * beat, 4), grid_fix=fix)
    except Exception:
        logger.debug("Beat grid refinement skipped", exc_info=True)
        return meta
