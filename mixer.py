"""Spotify-style playlist mixing: analysis, transition presets and rendering.

One engine serves both outputs. Huddle's WebRTC publisher and Discord's voice
player each ask for a *plan* (where the outgoing song leaves, where the next
one comes in, how long the blend is) and a rendered *segment*: the few bars in
which both songs overlap, already processed. The player then plays

    outgoing up to plan.out_at -> segment -> incoming from plan.in_resume_at

so the heavy DSP runs once, ahead of time, instead of per 20 ms frame.

Transitions are described the way Spotify's Mix editor describes them: a
preset (Fade, Rise, Blend, Wave, Melt, Slam) or Auto, a length in bars, and
five lanes - volume, EQ, filter, effects and loop - each with named choices.
"""

import asyncio
import json
import logging
import math
import os
import re
import subprocess
import threading
import time

import numpy as np
from scipy import ndimage, signal

logger = logging.getLogger("MusicBot.Mixer")

SR = 48_000              # render rate (matches Discord and Huddle output)
ANALYSIS_SR = 11_025     # analysis rate: plenty for tempo, key and loudness
MAX_BLEND_SECONDS = 40.0
CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "mix_analysis.json")

# --------------------------------------------------------------------------
# Lane choices. Labels follow Spotify's Turkish UI; English for the API.
# --------------------------------------------------------------------------

VOLUME = {
    "smooth_fade": ("Sesi azaltarak yumuşak geçiş", "Smooth fade"),
    "overlay": ("Bindir", "Overlay"),
    "fade_in_out": ("Sesi kademeli artırarak giriş ve çıkış", "Fade in and out"),
    "direct_in_fade_out": ("Direkt giriş, sesi kademeli azaltarak çıkış", "Direct in, fade out"),
    "fade_in_cut_out": ("Sesi kademeli artırarak giriş, direkt keserek çıkış", "Fade in, cut out"),
    "mid_cut": ("Ortada kesme", "Cut in the middle"),
    "fade": ("Sesi azaltarak geçiş", "Fade"),
    "fade_in_fast_out": ("Sesi kademeli artırarak giriş, hızlı çıkış", "Fade in, fast out"),
}
EQ = {
    "mid_bass_swap": ("Ortada bas değişimi", "Bass swap in the middle"),
    "end_bass_swap": ("Sonda bas değişimi", "Bass swap at the end"),
    "start_bass_swap": ("Başta bas değişimi", "Bass swap at the start"),
    "three_band": ("3 bant geçiş", "3-band swap"),
    "fast_bass_cut": ("Hızlı basla kesme", "Fast bass cut"),
    "long_bass_cut": ("Uzun basla kesme", "Long bass cut"),
    "start_bass_fade": ("Başta basları kademeli azaltma", "Bass fade at the start"),
    "bass_fade": ("Basları kademeli azaltma", "Bass fade"),
    "none": ("Hiçbiri", "None"),
}
FILTER = {
    "lp_out": ("Alçak geçiren filtreyle çıkış", "Low-pass out"),
    "lp_in": ("Alçak geçiren filtreyle giriş", "Low-pass in"),
    "lp_in_out": ("Alçak geçiren filtreyle giriş ve çıkış", "Low-pass in and out"),
    "lp_in_hp_out": ("Alçak geçiren filtreyle giriş, yüksek geçiren filtreyle çıkış", "Low-pass in, high-pass out"),
    "hp_out": ("Yüksek geçiren filtreyle çıkış", "High-pass out"),
    "hp_in": ("Yüksek geçiren filtreyle giriş", "High-pass in"),
    "hp_in_out": ("Yüksek geçiren filtreyle giriş ve çıkış", "High-pass in and out"),
    "hp_in_lp_out": ("Yüksek geçiren filtreyle giriş, alçak geçiren filtreyle çıkış", "High-pass in, low-pass out"),
    "hp_half_out": ("Yüksek geçiren filtreyle yarı çıkış", "High-pass half out"),
    "noise_out": ("Noise son çıkış", "Noise riser out"),
    "none": ("Hiçbiri", "None"),
}
EFFECTS = {
    "reverb_mid_out": ("Reverb orta çıkış", "Reverb out in the middle"),
    "reverb_end_cut": ("Reverb son kesme", "Reverb cut at the end"),
    "reverb_end_out": ("Reverb son çıkış", "Reverb out at the end"),
    "delay_half_cut": ("Geciktirme ½ son kesme", "Delay ½ cut"),
    "delay_3q_cut": ("Geciktirme ¾ son kesme", "Delay ¾ cut"),
    "echo_half_cut": ("Yankı ½ son kesme", "Echo ½ cut"),
    "echo_half_out": ("Yankı ½ son çıkış", "Echo ½ out"),
    "echo_3q_cut": ("Yankı ¾ son kesme", "Echo ¾ cut"),
    "echo_3q_out": ("Yankı ¾ son çıkış", "Echo ¾ out"),
    "echo_1_cut": ("Yankı 1 son kesme", "Echo 1 cut"),
    "echo_1_out": ("Yankı 1 son çıkış", "Echo 1 out"),
    "none": ("Hiçbiri", "None"),
}
LOOP = {
    "none": ("Hiçbiri", "None"),
    "loop_1": ("Son 1 ölçüyü döngüle", "Loop 1 bar"),
    "loop_2": ("Son 2 ölçüyü döngüle", "Loop 2 bars"),
    "loop_4": ("Son 4 ölçüyü döngüle", "Loop 4 bars"),
}
LANES = {"volume": VOLUME, "eq": EQ, "filter": FILTER, "effects": EFFECTS, "loop": LOOP}

PRESETS = {
    "fade": {"label": ("Azaltarak geç", "Fade"), "bars": 8,
             "volume": "smooth_fade", "eq": "bass_fade", "filter": "none",
             "effects": "none", "loop": "none"},
    "rise": {"label": ("Yükselterek geç", "Rise"), "bars": 8,
             "volume": "fade_in_out", "eq": "end_bass_swap", "filter": "noise_out",
             "effects": "none", "loop": "none"},
    "blend": {"label": ("Harmanla", "Blend"), "bars": 16,
              "volume": "overlay", "eq": "mid_bass_swap", "filter": "none",
              "effects": "none", "loop": "none"},
    "wave": {"label": ("Dalga", "Wave"), "bars": 8,
             "volume": "smooth_fade", "eq": "three_band", "filter": "hp_in_lp_out",
             "effects": "none", "loop": "none"},
    "melt": {"label": ("Erit", "Melt"), "bars": 8,
             "volume": "fade", "eq": "long_bass_cut", "filter": "lp_out",
             "effects": "reverb_end_out", "loop": "none"},
    "slam": {"label": ("Çarp", "Slam"), "bars": 2,
             "volume": "fade_in_cut_out", "eq": "fast_bass_cut", "filter": "none",
             "effects": "echo_half_cut", "loop": "none"},
    "none": {"label": ("Geçiş yok", "No transition"), "bars": 0,
             "volume": "overlay", "eq": "none", "filter": "none",
             "effects": "none", "loop": "none"},
}


def catalog():
    """Everything a UI needs to draw the editor."""
    def lane(options):
        return [{"id": key, "tr": tr, "en": en} for key, (tr, en) in options.items()]
    return {
        "presets": [{"id": key, "tr": value["label"][0], "en": value["label"][1],
                     "bars": value["bars"]} for key, value in PRESETS.items()],
        "lanes": {name: lane(options) for name, options in LANES.items()},
        "bars": [1, 2, 4, 8, 16, 32],
    }


def normalize_spec(spec):
    """Validate a stored/submitted transition. `None` means Auto."""
    if not isinstance(spec, dict):
        return {"preset": "auto"}
    preset = spec.get("preset") or "auto"
    if preset not in PRESETS and preset not in ("auto", "custom"):
        preset = "auto"
    clean = {"preset": preset}
    if preset == "auto":
        return clean
    base = PRESETS.get(preset) or PRESETS["fade"]
    for lane, options in LANES.items():
        value = spec.get(lane, base[lane])
        clean[lane] = value if value in options else base[lane]
    try:
        bars = int(spec.get("bars", base["bars"]))
    except (TypeError, ValueError):
        bars = base["bars"]
    clean["bars"] = bars if bars in (0, 1, 2, 4, 8, 16, 32) else base["bars"]
    return clean


# --------------------------------------------------------------------------
# Decoding
# --------------------------------------------------------------------------

def decode(source, start=0.0, duration=None, sr=SR, channels=2, atempo=1.0,
           timeout=90, filters=None, preroll=0.0):
    """Decode part of a file or URL to float32 samples shaped (n, channels).

    `filters` run before tempo stretching (e.g. the player's loudnorm chain);
    `preroll` seconds are decoded first and dropped so adaptive filters have
    settled by `start`.
    """
    preroll = min(preroll, start) if filters else 0.0
    start -= preroll
    command = ["ffmpeg", "-nostdin", "-loglevel", "error"]
    if str(source).startswith("http"):
        command += ["-reconnect", "1", "-reconnect_streamed", "1",
                    "-reconnect_delay_max", "2"]
    if start > 0:
        command += ["-ss", f"{start:.3f}"]
    command += ["-i", str(source), "-vn"]
    chain = list(filters or [])
    if abs(atempo - 1.0) > 1e-3:
        chain.append(f"atempo={atempo:.5f}")
    if chain:
        command += ["-af", ",".join(chain)]
    if duration is not None:
        # Pre-roll is decoded at native speed before the stretch applies to
        # the part we keep; -t counts output time.
        command += ["-t", f"{max(0.05, duration + preroll / atempo):.3f}"]
    command += ["-f", "f32le", "-ac", str(channels), "-ar", str(sr), "pipe:1"]
    result = subprocess.run(command, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, timeout=timeout)
    audio = np.frombuffer(result.stdout, dtype=np.float32)
    usable = len(audio) - len(audio) % channels
    audio = audio[:usable].reshape(-1, channels)
    if preroll:
        audio = audio[int(round(preroll / atempo * sr)):]
    return audio.copy()


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------

# Albrecht-Shanahan key profiles: in testing against Spotify's keys they beat
# Krumhansl, Temperley and EDMA; misses were relative major/minor, which share
# every note and so mix just as well.
_MAJOR = np.array([0.238, 0.006, 0.111, 0.006, 0.137, 0.094, 0.016, 0.214, 0.009, 0.080, 0.008, 0.081])
_MINOR = np.array([0.220, 0.006, 0.104, 0.123, 0.019, 0.103, 0.012, 0.214, 0.062, 0.022, 0.061, 0.052])
_NOTES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
# Camelot wheel numbers by pitch class of the tonic.
_CAMELOT_MAJOR = [8, 3, 10, 5, 12, 7, 2, 9, 4, 11, 6, 1]
_CAMELOT_MINOR = [5, 12, 7, 2, 9, 4, 11, 6, 1, 8, 3, 10]


def _stft_mag(mono, n_fft, hop):
    frames = 1 + max(0, (len(mono) - n_fft) // hop)
    if frames < 2:
        return np.zeros((1, n_fft // 2 + 1), dtype=np.float32)
    index = np.arange(n_fft)[None, :] + hop * np.arange(frames)[:, None]
    window = np.hanning(n_fft).astype(np.float32)
    return np.abs(np.fft.rfft(mono[index] * window, axis=1)).astype(np.float32)


def _tempo(mono):
    """BPM and first-beat time from a spectral-flux onset envelope."""
    hop = 256
    spectrum = np.log1p(10 * _stft_mag(mono, 1024, hop))
    flux = np.maximum(0, np.diff(spectrum, axis=0)).sum(axis=1)
    if len(flux) < 64 or not flux.any():
        return None, 0.0, 0.0
    flux = flux - np.convolve(flux, np.ones(16) / 16, mode="same")
    flux = np.maximum(flux, 0)
    fps = ANALYSIS_SR / hop
    ac = np.correlate(flux, flux, mode="full")[len(flux) - 1:]
    ac /= ac[0] + 1e-9
    lags = np.arange(len(ac))
    min_lag, max_lag = int(fps * 60 / 200), int(fps * 60 / 60)
    candidates = lags[min_lag:max_lag]
    bpms = 60 * fps / candidates
    # Prefer the 90-140 BPM octave most pop sits in, like DJ software does.
    weight = np.exp(-0.5 * (np.log2(bpms / 120) / 0.9) ** 2)
    scores = ac[min_lag:max_lag] * weight
    best = int(np.argmax(scores))
    lag = candidates[best]
    if 0 < best < len(scores) - 1:
        a, b, c = scores[best - 1], scores[best], scores[best + 1]
        denom = a - 2 * b + c
        lag = lag + (0.5 * (a - c) / denom if denom else 0)
    # Half-time check: many songs autocorrelate as strongly at two beats as at
    # one. Prefer the faster reading when it is nearly as strong.
    half = lag / 2
    if 60 * fps / half <= 190 and half >= min_lag:
        if ac[int(round(half))] >= 0.55 * ac[int(round(lag))]:
            lag = half
    bpm = 60 * fps / lag
    confidence = float(np.clip(ac[int(round(lag))] * 2, 0, 1))
    # Beat phase: the offset whose comb collects the most onset energy.
    period = float(lag)
    comb = []
    for phase in range(int(period)):
        ticks = np.round(phase + np.arange(0, (len(flux) - phase) / period) * period)
        ticks = ticks[ticks < len(flux)].astype(int)
        comb.append(flux[ticks].sum())
    first_beat = float(np.argmax(comb) / fps) if comb else 0.0
    return round(float(bpm), 1), first_beat, confidence


def _key(mono):
    n_fft = 8192
    spectrum = _stft_mag(mono, n_fft, n_fft // 4)
    freqs = np.fft.rfftfreq(n_fft, 1 / ANALYSIS_SR)
    usable = (freqs > 55) & (freqs < 2000)
    pitch = np.round(12 * np.log2(freqs[usable] / 440) + 69).astype(int) % 12
    # Per-frame normalised, log-compressed chroma so loud sections and bass
    # don't outvote the harmony.
    frames = np.log1p(spectrum[:, usable] * 10)
    per_frame = np.stack([np.bincount(pitch, weights=row, minlength=12) for row in frames])
    per_frame /= per_frame.max(axis=1, keepdims=True) + 1e-9
    chroma = per_frame.mean(axis=0)
    if not chroma.any():
        return None, None
    chroma = (chroma - chroma.mean()) / (chroma.std() + 1e-9)
    best = (-2.0, 0, "major")
    for tonic in range(12):
        for mode, profile in (("major", _MAJOR), ("minor", _MINOR)):
            rolled = np.roll(profile, tonic)
            rolled = (rolled - rolled.mean()) / rolled.std()
            score = float(np.dot(chroma, rolled) / 12)
            if score > best[0]:
                best = (score, tonic, mode)
    _, tonic, mode = best
    if mode == "major":
        return f"{_NOTES[tonic]} major", f"{_CAMELOT_MAJOR[tonic]}B"
    return f"{_NOTES[tonic]} minor", f"{_CAMELOT_MINOR[tonic]}A"


def analyze_samples(mono):
    """Analysis of a mono ANALYSIS_SR signal."""
    duration = len(mono) / ANALYSIS_SR
    bpm, first_beat, confidence = _tempo(mono)
    key_name, camelot = _key(mono)
    block = ANALYSIS_SR // 2
    count = max(1, len(mono) // block)
    rms = np.sqrt((mono[: count * block].reshape(count, block) ** 2).mean(axis=1) + 1e-12)
    db = 20 * np.log10(rms)
    loud = np.percentile(db, 90) if len(db) else -20
    audible = np.where(db > loud - 30)[0]
    start = float(audible[0] * 0.5) if len(audible) else 0.0
    end = float((audible[-1] + 1) * 0.5) if len(audible) else duration
    # Masters are all loud, so energy leans on how busy (onset flux) and how
    # bright the song is. Calibrated so ballads land ~0.3, rock ~0.8.
    spectrum = _stft_mag(mono, 1024, 256)
    flux = float(np.maximum(0, np.diff(np.log1p(10 * spectrum), axis=0)).sum(axis=1).mean())
    freqs = np.fft.rfftfreq(1024, 1 / ANALYSIS_SR)
    centroid = float(np.median((spectrum * freqs).sum(axis=1) / (spectrum.sum(axis=1) + 1e-9)))
    energy = float(np.clip(
        0.6 * np.clip((flux - 70) / 70, 0, 1)
        + 0.25 * np.clip((centroid - 600) / 1400, 0, 1)
        + 0.15 * np.clip((np.median(db) + 18) / 10, 0, 1), 0, 1))
    return {
        "bpm": bpm, "bpm_confidence": round(confidence, 2),
        "first_beat": round(first_beat, 3),
        "key": key_name, "camelot": camelot,
        "energy": round(energy, 3), "loudness_db": round(float(loud), 1),
        "duration": round(duration, 2),
        "music_start": round(start, 2), "music_end": round(end, 2),
        "analyzed_at": time.time(),
    }


class AnalysisCache:
    """JSON cache keyed by track identity (page URL, query or file path)."""

    def __init__(self, path=CACHE_FILE):
        self.path = path
        self.lock = threading.Lock()
        try:
            with open(path, encoding="utf-8") as handle:
                self.data = json.load(handle)
        except (OSError, ValueError):
            self.data = {}

    def get(self, *keys):
        for key in keys:
            if key and key in self.data:
                return self.data[key]
        return None

    def put(self, value, *keys):
        with self.lock:
            for key in keys:
                if key:
                    self.data[key] = value
            tmp = self.path + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as handle:
                    json.dump(self.data, handle)
                os.replace(tmp, self.path)
            except OSError as error:
                logger.warning("Could not save mix analysis: %s", error)


    def drop(self, *keys):
        """Forget a song's analysis (a "recompute" redoes it)."""
        with self.lock:
            if not any(key in self.data for key in keys if key):
                return
            for key in keys:
                self.data.pop(key, None)
            tmp = self.path + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as handle:
                    json.dump(self.data, handle)
                os.replace(tmp, self.path)
            except OSError as error:
                logger.warning("Could not save mix analysis: %s", error)


cache = AnalysisCache()
_inflight = {}


def track_key(value):
    """Stable cache key for a URL/query: YouTube id when there is one."""
    if not value:
        return None
    match = re.search(r"(?:v=|youtu\.be/|shorts/)([A-Za-z0-9_-]{11})", value)
    if match:
        return "yt:" + match.group(1)
    return value.strip().lower()


def analyze(source, *keys):
    """Blocking analysis with caching. `source` is a file path or URL."""
    cached = cache.get(*keys)
    if cached:
        return cached
    mono = decode(source, sr=ANALYSIS_SR, channels=1, timeout=180)[:, 0]
    if len(mono) < ANALYSIS_SR * 5:
        raise ValueError("Not enough audio to analyse.")
    result = analyze_samples(mono)
    cache.put(result, *keys)
    return result


async def analyze_async(source, *keys):
    """Shared, de-duplicated analysis off the event loop."""
    cached = cache.get(*keys)
    if cached:
        return cached
    inflight_key = keys[0] if keys and keys[0] else source
    task = _inflight.get(inflight_key)
    if task is None:
        task = asyncio.get_running_loop().run_in_executor(None, analyze, source, *keys)
        _inflight[inflight_key] = task
    try:
        return await task
    finally:
        _inflight.pop(inflight_key, None)


# --------------------------------------------------------------------------
# Compatibility and Auto
# --------------------------------------------------------------------------

def _camelot(code):
    if not code:
        return None
    match = re.match(r"^(\d+)([AB])$", code)
    return (int(match.group(1)), match.group(2)) if match else None


def key_distance(a, b):
    """0 = same key, 1 = neighbours on the Camelot wheel, larger = clash."""
    ca, cb = _camelot(a), _camelot(b)
    if not ca or not cb:
        return 1
    steps = min((ca[0] - cb[0]) % 12, (cb[0] - ca[0]) % 12)
    if ca[1] == cb[1]:
        return steps
    return steps + 1 if steps else 1  # relative major/minor


def tempo_ratio(bpm_out, bpm_in):
    """Incoming->outgoing speed ratio, allowing half/double time matches."""
    if not bpm_out or not bpm_in:
        return None
    best = None
    for factor in (0.5, 1.0, 2.0):
        ratio = bpm_out / (bpm_in * factor)
        if best is None or abs(math.log(ratio)) < abs(math.log(best)):
            best = ratio
    return best


def auto_spec(out_meta, in_meta):
    """Pick a transition the way a DJ would, from tempo, key and energy."""
    ratio = tempo_ratio(out_meta.get("bpm"), in_meta.get("bpm"))
    tempo_gap = abs(1 - ratio) if ratio else 1.0
    keys = key_distance(out_meta.get("camelot"), in_meta.get("camelot"))
    lift = (in_meta.get("energy") or 0.5) - (out_meta.get("energy") or 0.5)
    confident = min(out_meta.get("bpm_confidence") or 0,
                    in_meta.get("bpm_confidence") or 0) >= 0.3
    if tempo_gap <= 0.04 and keys <= 1 and confident:
        preset, bars = "blend", 16
    elif tempo_gap <= 0.06 and confident:
        preset, bars = "wave", 8
    elif lift > 0.15:
        preset, bars = "rise", 8
    elif lift < -0.15:
        preset, bars = "melt", 8
    elif tempo_gap > 0.15:
        preset, bars = "slam", 2
    else:
        preset, bars = "fade", 8
    spec = dict(PRESETS[preset])
    spec.pop("label", None)
    spec["preset"] = preset
    spec["bars"] = bars
    spec["auto"] = True
    return spec


def resolve_spec(spec, out_meta, in_meta):
    spec = normalize_spec(spec)
    if spec["preset"] == "auto":
        return auto_spec(out_meta, in_meta)
    return spec


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------

def plan(out_meta, in_meta, spec=None, out_duration=None, out_position=0.0):
    """Where to leave the outgoing song, where to enter the next, how long.

    Returns None when there should be no transition (hard cut).
    """
    spec = resolve_spec(spec, out_meta, in_meta)
    if spec["bars"] <= 0:
        return None
    bpm_out = out_meta.get("bpm") or 120.0
    bar = 4 * 60.0 / bpm_out
    duration = out_duration or out_meta.get("duration") or 0
    music_end = min(out_meta.get("music_end") or duration, duration or 1e9)
    length = spec["bars"] * bar
    # Never let the blend eat more than a quarter of the song.
    while spec["bars"] > 1 and (length > MAX_BLEND_SECONDS or length > music_end * 0.25):
        spec["bars"] //= 2
        length = spec["bars"] * bar
    first_beat = out_meta.get("first_beat") or 0.0
    out_at = music_end - length
    # Snap back to a bar line of the outgoing song.
    bars_in = math.floor((out_at - first_beat) / bar)
    out_at = first_beat + bars_in * bar
    if out_at <= out_position + 3:
        return None
    ratio = tempo_ratio(bpm_out, in_meta.get("bpm")) or 1.0
    # Beat-match when the tempos are close enough to stretch cleanly (atempo
    # is transparent to ~10%); the newcomer settles at its own tempo after.
    stretch = ratio if abs(1 - ratio) <= 0.10 else 1.0
    cue = in_meta.get("first_beat") or 0.0
    music_start = in_meta.get("music_start") or 0.0
    if cue < music_start and in_meta.get("bpm"):
        in_bar = 4 * 60.0 / in_meta["bpm"]
        cue += math.ceil((music_start - cue) / in_bar) * in_bar
    return {
        "spec": spec,
        "out_at": round(out_at, 3),
        "length": round(length, 3),
        "in_at": round(cue, 3),
        "stretch": round(stretch, 5),
        # Where the incoming song carries on once the segment has played.
        "in_resume_at": round(cue + length * stretch, 3),
        "bpm_out": out_meta.get("bpm"),
        "bpm_in": in_meta.get("bpm"),
    }


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def _ramp(n, start, end, v0, v1, shape="linear"):
    """Curve over n samples: v0 before `start`, v1 after `end` (fractions)."""
    t = np.linspace(0, 1, n, endpoint=False, dtype=np.float32)
    if end <= start:
        return np.where(t < start, v0, v1).astype(np.float32)
    x = np.clip((t - start) / (end - start), 0, 1)
    if shape == "cos":
        x = 0.5 - 0.5 * np.cos(np.pi * x)
    elif shape == "power":  # equal-power
        return (v0 + (v1 - v0) * np.sin(x * np.pi / 2)).astype(np.float32) if v1 > v0 \
            else (v1 + (v0 - v1) * np.cos(x * np.pi / 2)).astype(np.float32)
    return (v0 + (v1 - v0) * x).astype(np.float32)


def _volume_curves(kind, n):
    one = np.ones(n, dtype=np.float32)
    if kind == "smooth_fade":
        return _ramp(n, 0, 1, 1, 0, "power"), _ramp(n, 0, 1, 0, 1, "power")
    if kind == "overlay":
        return _ramp(n, 0.85, 1, 1, 0, "cos"), _ramp(n, 0, 0.15, 0.5, 1, "cos")
    if kind == "fade_in_out":
        return _ramp(n, 0.3, 1, 1, 0, "cos"), _ramp(n, 0, 0.7, 0, 1, "cos")
    if kind == "direct_in_fade_out":
        return _ramp(n, 0, 1, 1, 0, "cos"), one
    if kind == "fade_in_cut_out":
        return _ramp(n, 0.97, 1, 1, 0), _ramp(n, 0, 0.9, 0, 1, "cos")
    if kind == "mid_cut":
        return _ramp(n, 0.49, 0.51, 1, 0), _ramp(n, 0.49, 0.51, 0, 1)
    if kind == "fade_in_fast_out":
        return _ramp(n, 0.75, 1, 1, 0, "cos"), _ramp(n, 0, 0.75, 0, 1, "cos")
    return _ramp(n, 0, 1, 1, 0), _ramp(n, 0, 1, 0, 1)  # fade


def _eq_curves(kind, n):
    """(out_low, out_mid, out_high), (in_low, in_mid, in_high) gain curves."""
    one = np.ones(n, dtype=np.float32)

    def swap(at, width=0.04):
        return _ramp(n, at - width, at + width, 1, 0, "cos"), _ramp(n, at - width, at + width, 0, 1, "cos")

    if kind == "mid_bass_swap":
        out_low, in_low = swap(0.5)
    elif kind == "end_bass_swap":
        out_low, in_low = swap(0.88)
    elif kind == "start_bass_swap":
        out_low, in_low = swap(0.12)
    elif kind == "three_band":
        out_low, in_low = swap(0.3)
        out_mid, in_mid = swap(0.5)
        out_high, in_high = swap(0.7)
        return (out_low, out_mid, out_high), (in_low, in_mid, in_high)
    elif kind == "fast_bass_cut":
        out_low = _ramp(n, 0, 0.1, 1, 0, "cos")
        in_low = _ramp(n, 0.9, 1, 0, 1, "cos")
    elif kind == "long_bass_cut":
        out_low = _ramp(n, 0, 0.8, 1, 0, "cos")
        in_low = _ramp(n, 0.5, 1, 0, 1, "cos")
    elif kind == "start_bass_fade":
        out_low = _ramp(n, 0, 0.4, 1, 0, "cos")
        in_low = _ramp(n, 0, 0.5, 0, 1, "cos")
    elif kind == "bass_fade":
        out_low = _ramp(n, 0, 1, 1, 0, "cos")
        in_low = _ramp(n, 0, 1, 0, 1, "cos")
    else:
        return (one, one, one), (one, one, one)
    return (out_low, one, one), (in_low, one, one)


def _bands(audio):
    """Split into low / mid / high around 250 Hz and 2.5 kHz (LR4-style)."""
    low_sos = signal.butter(2, 250, "low", fs=SR, output="sos")
    high_sos = signal.butter(2, 2500, "high", fs=SR, output="sos")
    low = signal.sosfilt(low_sos, signal.sosfilt(low_sos, audio, axis=0), axis=0)
    high = signal.sosfilt(high_sos, signal.sosfilt(high_sos, audio, axis=0), axis=0)
    return low, audio - low - high, high


def _apply_eq(audio, gains):
    low_g, mid_g, high_g = gains
    if all(np.allclose(g, 1) for g in gains):
        return audio
    low, mid, high = _bands(audio)
    return low * low_g[:, None] + mid * mid_g[:, None] + high * high_g[:, None]


def _biquad(kind, freq, q=0.9):
    w = 2 * math.pi * min(freq, SR * 0.45) / SR
    alpha = math.sin(w) / (2 * q)
    cos = math.cos(w)
    if kind == "low":
        b = [(1 - cos) / 2, 1 - cos, (1 - cos) / 2]
    else:
        b = [(1 + cos) / 2, -(1 + cos), (1 + cos) / 2]
    a = [1 + alpha, -2 * cos, 1 - alpha]
    return np.array(b) / a[0], np.array(a) / a[0]


def _sweep(audio, kind, f_start, f_end, start=0.0, end=1.0, block=256):
    """Time-varying resonant filter, exponential sweep between fractions."""
    n = len(audio)
    out = np.empty_like(audio)
    zi = np.zeros((2, audio.shape[1]))
    for offset in range(0, n, block):
        frac = min(1.0, max(0.0, ((offset + block / 2) / n - start) / max(1e-6, end - start)))
        freq = f_start * (f_end / f_start) ** frac
        b, a = _biquad(kind, freq)
        chunk = audio[offset:offset + block]
        if (kind == "low" and freq >= 19000) or (kind == "high" and freq <= 21):
            out[offset:offset + block] = chunk
            zi[:] = 0
            continue
        for channel in range(audio.shape[1]):
            out[offset:offset + block, channel], zi[:, channel] = signal.lfilter(
                b, a, chunk[:, channel], zi=zi[:, channel])
    return out


def _apply_filter(kind, out_audio, in_audio):
    if kind in ("lp_out", "lp_in_out", "hp_in_lp_out"):
        out_audio = _sweep(out_audio, "low", 20000, 250)
    if kind in ("hp_out", "lp_in_hp_out"):
        out_audio = _sweep(out_audio, "high", 20, 1800)
    if kind == "hp_half_out":
        out_audio = _sweep(out_audio, "high", 20, 700, 0, 0.5)
    if kind == "noise_out":
        out_audio = _sweep(out_audio, "high", 20, 1200, 0.2, 0.95)
    if kind in ("lp_in", "lp_in_out", "lp_in_hp_out"):
        in_audio = _sweep(in_audio, "low", 250, 20000)
    if kind in ("hp_in", "hp_in_out", "hp_in_lp_out"):
        in_audio = _sweep(in_audio, "high", 1800, 20)
    if kind == "hp_in_out":
        out_audio = _sweep(out_audio, "high", 20, 1800)
    return out_audio, in_audio


def _noise_riser(n, seed=7):
    rng = np.random.default_rng(seed)
    noise = rng.standard_normal((n, 2)).astype(np.float32) * 0.12
    noise = _sweep(noise, "high", 300, 9000)
    return noise * _ramp(n, 0.1, 0.93, 0, 1, "cos")[:, None] * _ramp(n, 0.93, 0.97, 1, 0)[:, None]


def _impulse(kind, beat, n_max):
    """Impulse response for reverb / delay / echo sends."""
    if kind == "reverb":
        length = int(SR * 2.8)
        rng = np.random.default_rng(3)
        t = np.arange(length) / SR
        decay = np.exp(-6.9 * t / 2.6)[:, None]
        ir = rng.standard_normal((length, 2)) * decay
        ir[: int(SR * 0.02)] = 0
        ir = signal.sosfilt(signal.butter(1, 6000, "low", fs=SR, output="sos"), ir, axis=0)
        return (ir / np.sqrt((ir ** 2).sum(axis=0).mean()) * 0.6).astype(np.float32)
    division, feedback = {
        "delay_half": (0.5, 0.42), "delay_3q": (0.75, 0.42),
        "echo_half": (0.5, 0.62), "echo_3q": (0.75, 0.62), "echo_1": (1.0, 0.62),
    }[kind]
    step = int(SR * beat * division)
    taps = max(1, min(16, int(n_max / max(1, step))))
    ir = np.zeros((step * taps + 1, 2), dtype=np.float32)
    for k in range(1, taps + 1):
        # Ping-pong between the ears.
        ir[k * step, (k + 1) % 2] = feedback ** k
    return ir


def _apply_effects(kind, out_audio, beat):
    """Returns (processed outgoing, wet tail) with dry cut/fade as named."""
    n = len(out_audio)
    if kind == "none":
        return out_audio, np.zeros_like(out_audio)
    if kind.startswith("reverb"):
        ir_kind = "reverb"
        cut_at = 0.5 if kind == "reverb_mid_out" else 0.8
        send = _ramp(n, 0.1, cut_at, 0, 1, "cos")
    else:
        ir_kind = kind.rsplit("_", 1)[0]
        cut_at = 0.7
        send = _ramp(n, cut_at - 0.12, cut_at, 0, 1, "cos")
    wet = signal.fftconvolve(out_audio * send[:, None], _impulse(ir_kind, beat, n), axes=0)[:n]
    if ir_kind in ("echo_half", "echo_3q", "echo_1"):
        wet = signal.sosfilt(signal.butter(2, 3500, "low", fs=SR, output="sos"), wet, axis=0)
    if kind.endswith("_cut") or kind == "reverb_mid_out":
        dry = out_audio * _ramp(n, cut_at, cut_at + 0.01, 1, 0)[:, None]
    else:
        dry = out_audio * _ramp(n, cut_at - 0.2, cut_at + 0.1, 1, 0, "cos")[:, None]
    # Let the tail die inside the segment.
    wet *= _ramp(n, 0.9, 1.0, 1, 0, "cos")[:, None]
    return dry, wet.astype(np.float32)


def _loop(audio, bars, bar_seconds):
    if not bars:
        return audio
    length = int(bars * bar_seconds * SR)
    if length <= 0 or length >= len(audio):
        return audio
    piece = audio[:length].copy()
    fade = min(240, length // 4)
    piece[:fade] *= np.linspace(0, 1, fade, dtype=np.float32)[:, None]
    piece[-fade:] *= np.linspace(1, 0, fade, dtype=np.float32)[:, None]
    reps = int(math.ceil(len(audio) / length))
    looped = np.tile(piece, (reps, 1))[: len(audio)]
    looped[:fade] = audio[:fade]  # keep the first entry seamless
    return looped


def _limit(audio, ceiling=0.97):
    """Transparent peak limiter: 5 ms look-around, smoothed gain."""
    peak = np.max(np.abs(audio), axis=1)
    if peak.max() <= ceiling:
        return audio
    gain = np.minimum(1.0, ceiling / np.maximum(peak, 1e-9))
    window = int(SR * 0.005)
    gain = ndimage.minimum_filter1d(gain, size=2 * window + 1)
    gain = ndimage.uniform_filter1d(gain, size=window)
    return audio * np.minimum(gain, np.minimum(1.0, ceiling / np.maximum(peak, 1e-9)))[:, None]


def render_arrays(out_audio, in_audio, plan_):
    """Mix two already-decoded, equal-length stereo arrays per the plan."""
    spec = plan_["spec"]
    n = min(len(out_audio), len(in_audio))
    if n == 0:
        return np.zeros((0, 2), dtype=np.float32)
    out_audio = out_audio[:n].astype(np.float32)
    in_audio = in_audio[:n].astype(np.float32)
    if len(out_audio) < n:
        out_audio = np.pad(out_audio, ((0, n - len(out_audio)), (0, 0)))
    bpm = plan_.get("bpm_out") or 120.0
    beat = 60.0 / bpm
    loop_bars = {"loop_1": 1, "loop_2": 2, "loop_4": 4}.get(spec.get("loop"), 0)
    out_audio = _loop(out_audio, loop_bars, 4 * beat)
    # Loudness match: bring the newcomer to the outgoing level, then let go.
    out_rms = float(np.sqrt((out_audio ** 2).mean()) + 1e-9)
    in_rms = float(np.sqrt((in_audio ** 2).mean()) + 1e-9)
    match = float(np.clip(out_rms / in_rms, 0.7, 1.4))
    in_audio = in_audio * _ramp(n, 0.6, 1.0, match, 1.0, "cos")[:, None]

    out_eq, in_eq = _eq_curves(spec.get("eq", "none"), n)
    out_audio = _apply_eq(out_audio, out_eq)
    in_audio = _apply_eq(in_audio, in_eq)
    out_audio, in_audio = _apply_filter(spec.get("filter", "none"), out_audio, in_audio)
    out_audio, wet = _apply_effects(spec.get("effects", "none"), out_audio, beat)
    out_gain, in_gain = _volume_curves(spec.get("volume", "fade"), n)
    mixed = out_audio * out_gain[:, None] + wet + in_audio * in_gain[:, None]
    if spec.get("filter") == "noise_out":
        mixed += _noise_riser(n)
    return _limit(mixed).astype(np.float32)


def render(out_source, in_source, plan_, filters=None):
    """Decode both sides of the plan and render the overlap as s16le bytes.

    Pass the player's own filter chain (e.g. loudnorm) as `filters` so the
    transition sits at the same level as the songs around it.
    """
    length = plan_["length"]
    out_audio = decode(out_source, plan_["out_at"], length,
                       filters=filters, preroll=6.0)
    # atempo shortens the output, so ask for `length` seconds of output.
    in_audio = decode(in_source, plan_["in_at"], length,
                      atempo=plan_["stretch"], filters=filters, preroll=6.0)
    n = int(round(length * SR))
    out_audio = np.pad(out_audio, ((0, max(0, n - len(out_audio))), (0, 0)))[:n]
    in_audio = np.pad(in_audio, ((0, max(0, n - len(in_audio))), (0, 0)))[:n]
    mixed = render_arrays(out_audio, in_audio, plan_)
    return to_pcm16(mixed)


def to_pcm16(audio):
    return (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()


def render_preview(out_source, in_source, plan_, lead=6.0, tail=6.0):
    """Lead-in of the outgoing song, the transition, then the incoming song."""
    before = decode(out_source, max(0.0, plan_["out_at"] - lead), lead)
    segment = np.frombuffer(render(out_source, in_source, plan_), dtype="<i2") \
        .reshape(-1, 2).astype(np.float32) / 32767
    after = decode(in_source, plan_["in_resume_at"], tail)
    return to_pcm16(np.concatenate([before, segment, after]))


# --------------------------------------------------------------------------
# Smart reorder
# --------------------------------------------------------------------------

def transition_cost(a, b):
    """How rough a -> b would be; used by Smart Reorder.

    Tempo matters most: within ~6% the decks beat-match, past ~8% they can't
    (the booth falls back to an echo out), so the cost jumps there. Then key
    (Camelot neighbours are free-ish, a clash is not) and mood: energy, plus
    a small step for flipping between minor and major.
    """
    ratio = tempo_ratio(a.get("bpm"), b.get("bpm"))
    gap = abs(math.log(ratio)) if ratio else 0.3
    tempo = gap * 8 + (1.5 if gap > 0.08 else 0.0)
    steps = key_distance(a.get("camelot"), b.get("camelot"))
    keys = (0.0, 0.15, 0.7)[steps] if steps < 3 else 1.2
    energy = abs((b.get("energy") or 0.5) - (a.get("energy") or 0.5)) * 1.6
    mode_a, mode_b = (a.get("camelot") or "")[-1:], (b.get("camelot") or "")[-1:]
    mood = 0.15 if mode_a and mode_b and mode_a != mode_b else 0.0
    return tempo + keys + energy + mood


def _path_cost(order, cost):
    return sum(cost[order[i]][order[i + 1]] for i in range(len(order) - 1))


def _improve(order, cost):
    """2-opt plus moving single songs, on an open path (any start and end)."""
    count = len(order)
    improved, rounds = True, 0
    while improved and rounds < 40:
        improved, rounds = False, rounds + 1
        # 2-opt: reverse a stretch. Edges at the path's ends cost nothing.
        for i in range(0, count - 1):
            for j in range(i + 1, count):
                a = order[i - 1] if i > 0 else None
                d = order[j + 1] if j + 1 < count else None
                b, c = order[i], order[j]
                before = (cost[a][b] if a is not None else 0) + (cost[c][d] if d is not None else 0)
                after = (cost[a][c] if a is not None else 0) + (cost[b][d] if d is not None else 0)
                if after < before - 1e-9:
                    order[i:j + 1] = reversed(order[i:j + 1])
                    improved = True
        # Or-opt: take one song out and put it where it fits best.
        for i in range(count):
            node = order[i]
            rest = order[:i] + order[i + 1:]
            current = _path_cost(order, cost)
            best, best_at = current, None
            for k in range(len(rest) + 1):
                trial = rest[:k] + [node] + rest[k:]
                value = _path_cost(trial, cost)
                if value < best - 1e-9:
                    best, best_at = value, k
            if best_at is not None:
                order[:] = rest[:best_at] + [node] + rest[best_at:]
                improved = True
    return order


def smart_order(metas):
    """Order indices so neighbours match in tempo, key and mood.

    Greedy from every starting song, the best one polished with 2-opt and
    single moves; then played in the direction that warms up (energy rises
    over the first half) rather than one that starts at its peak.
    """
    count = len(metas)
    if count < 3:
        return list(range(count))
    cost = [[transition_cost(metas[i], metas[j]) if i != j else 0
             for j in range(count)] for i in range(count)]
    best = None
    for start in range(count):
        order, left = [start], set(range(count)) - {start}
        while left:
            last = order[-1]
            nxt = min(left, key=lambda j: cost[last][j])
            order.append(nxt)
            left.remove(nxt)
        value = _path_cost(order, cost)
        if best is None or value < best[0]:
            best = (value, order)
    order = _improve(best[1], cost)
    if _path_cost(order[::-1], cost) <= _path_cost(order, cost) + 1e-9:
        energy = [metas[i].get("energy") or 0.5 for i in order]
        half = max(1, count // 2)
        if sum(energy[:half // 2 or 1]) / (half // 2 or 1) > sum(energy[half // 2:half]) / max(1, half - half // 2):
            order.reverse()
    return order
