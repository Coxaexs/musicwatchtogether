"""The sung melody of a song, read off its separated vocal stem.

Hoffle's singing voice (public/tune-worklet.js in the Huddle repo) pulls a
karaoke singer onto the note the original singer sings at that moment, so a
note sung well off is still corrected onto the right one. This works out
those notes: pitch (YIN) every 10 ms on the vocal stem, cut into held notes.

The result is a flat list [start_ms, end_ms, midi, ...] (midi to 0.1, which
keeps a song recorded off concert pitch where it is), cached as
"<hash>.melody.json" beside the stems. Songs without stems have no melody.
"""

import json
import logging
import os
import subprocess

import numpy as np

import stems

logger = logging.getLogger("MusicBot.Melody")

RATE = 16_000
HOP = 160                    # 10 ms
WINDOW = 1024                # 64 ms
MIN_HZ = 70
MAX_HZ = 1100
THRESHOLD = 0.22             # YIN: a dip this low is a pitch (lenient: sung, not played)
#: A held note: at least this long, and broken where the pitch moves this far
#: from the note so far, or after a gap this long.
MIN_NOTE_MS = 90
BREAK_SEMITONES = 0.7
GAP_MS = 40
MAX_NOTES = 1500             # what the hub accepts


def path(digest):
    return os.path.join(stems.CACHE_DIR, digest + ".melody.json")


def _vocals(digest):
    """The vocal stem, mono float32 at RATE."""
    raw = stems.stems_path(digest)
    if os.path.exists(raw):
        data = np.load(raw, mmap_mode="r")[stems.NAMES.index("vocals")]
        mono = data.astype(np.float32).mean(axis=1) / 32768.0
        step = stems.SR // RATE
        n = len(mono) // step * step
        # Averaging each three samples is low-pass enough for a voice's pitch.
        return mono[:n].reshape(-1, step).mean(axis=1)
    flac = stems.stems_flac_path(digest)
    if not os.path.exists(flac):
        raise FileNotFoundError(digest)
    channel = stems.NAMES.index("vocals") * 2
    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", flac,
         "-af", f"pan=mono|c0=0.5*c{channel}+0.5*c{channel + 1}",
         "-ar", str(RATE), "-f", "f32le", "pipe:1"],
        capture_output=True, timeout=300)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode(errors="replace").strip()[-200:])
    return np.frombuffer(result.stdout, dtype="<f4")


def pitch_track(x):
    """(midi or nan, rms) per HOP, by YIN."""
    max_tau = RATE // MIN_HZ
    min_tau = RATE // MAX_HZ
    span = WINDOW + max_tau
    frames = max(0, (len(x) - span) // HOP)
    midi = np.full(frames, np.nan, dtype=np.float32)
    rms = np.zeros(frames, dtype=np.float32)
    size = 1 << int(np.ceil(np.log2(span + WINDOW)))
    taus = np.arange(max_tau + 1)
    for begin in range(0, frames, 1000):
        idx = np.arange(begin, min(frames, begin + 1000))
        block = np.lib.stride_tricks.sliding_window_view(x, span)[idx * HOP]
        head = block[:, :WINDOW]
        rms[idx] = np.sqrt((head ** 2).mean(axis=1))
        # Difference function d(tau) = e0 + e(tau) - 2 r(tau), r by FFT.
        cross = np.fft.irfft(np.fft.rfft(head, size) .conj() * np.fft.rfft(block, size), size)[:, : max_tau + 1]
        sq = np.concatenate([np.zeros((len(idx), 1), np.float32), np.cumsum(block ** 2, axis=1)], axis=1)
        e0 = sq[:, WINDOW][:, None]
        e_tau = sq[:, taus + WINDOW] - sq[:, taus]
        d = np.maximum(e0 + e_tau - 2 * cross, 0)
        cum = np.cumsum(d[:, 1:], axis=1)
        cmnd = np.ones_like(d)
        cmnd[:, 1:] = d[:, 1:] * taus[1:] / np.maximum(cum, 1e-12)
        for row, frame in enumerate(idx):
            c = cmnd[row]
            below = np.nonzero(c[min_tau:] < THRESHOLD)[0]
            if not len(below):
                continue
            tau = below[0] + min_tau
            while tau + 1 <= max_tau and c[tau + 1] < c[tau]:
                tau += 1
            if tau <= 1 or tau >= max_tau:
                continue
            a, b, cc = c[tau - 1], c[tau], c[tau + 1]
            denom = a - 2 * b + cc
            exact = tau + (0.5 * (a - cc) / denom if denom else 0)
            midi[frame] = 69 + 12 * np.log2(RATE / exact / 440)
    return midi, rms


def notes_from(midi, rms):
    """Held notes as a flat [start_ms, end_ms, midi, ...]."""
    loud = rms[rms > 0]
    if not len(loud):
        return []
    # Bleed from the other stems is quiet: what is well under the singing is not singing.
    floor = max(0.004, 0.12 * float(np.percentile(loud, 95)))
    voiced = (rms >= floor) & ~np.isnan(midi)
    ms = 1000 * HOP / RATE
    centre = 500 * WINDOW / RATE           # a frame's pitch is its window's middle
    out = []
    start = None
    values = []
    gap = 0

    def close(end):
        if start is not None and (end - start) * ms >= MIN_NOTE_MS and values:
            out.extend([round(start * ms + centre), round(end * ms + centre), round(float(np.median(values)), 1)])

    for i in range(len(midi)):
        if not voiced[i]:
            gap += 1
            if start is not None and gap * ms > GAP_MS:
                close(i - gap + 1)
                start, values = None, []
            continue
        gap = 0
        m = float(midi[i])
        if start is not None and abs(m - float(np.median(values[-15:]))) > BREAK_SEMITONES:
            close(i)
            start, values = None, []
        if start is None:
            start = i
        values.append(m)
    close(len(midi))
    return out[: 3 * MAX_NOTES]


def melody(digest):
    """The song's melody (cached), or None without stems."""
    if not digest:
        return None
    cached = path(digest)
    try:
        with open(cached) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        pass
    if not stems.has_stems(digest):
        return None
    try:
        notes = notes_from(*pitch_track(_vocals(digest)))
    except Exception as error:
        logger.warning("Melody for %s: %s", digest, error)
        return None
    try:
        with open(cached + ".tmp", "w") as handle:
            json.dump(notes, handle, separators=(",", ":"))
        os.replace(cached + ".tmp", cached)
    except OSError as error:
        logger.debug("Melody for %s not cached: %s", digest, error)
    return notes
