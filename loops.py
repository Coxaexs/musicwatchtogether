"""The loop crate: real parts of the library's songs, for Real DJ to play over its mixes.

The stems cache holds most of the library split into drums, bass and vocals,
and many songs also into guitar and piano (stems.extras). Somewhere in most
songs one of those parts plays a riff on its own for a few bars: a guitar
figure between the verses, a piano vamp, a bass line, a drum groove with the
music gone. Those are what a DJ borrows. Real DJ lays them over a mix where
they fit: a guitar riff from one song under the vocal of another, a drum
groove carrying the beat while the next song comes in.

build() goes through the cache once (python loops.py build), finds in each
song the four-bar stretch where each part is loudest against the others,
repeats itself every two bars (so it loops without a seam), and has no singer
bleeding into it, and keeps where it is: nothing is copied, the crate is an
index of places in the stems (loops/index.json).

pick() chooses one for two songs being mixed: a tempo it can be stretched to
(12% at most, half or double time allowed) and a key it can be moved to (two
semitones at most) so it sits in the songs' key or the next one round the
wheel. render() reads it from the stems and stretches and shifts it with
rubberband (ffmpeg), to the exact length of its bars at the set's tempo.
"""

import json
import logging
import math
import os
import random
import subprocess
import sys
import threading
import time
from collections import deque

import numpy as np

import mixer
import stems

logger = logging.getLogger("MusicBot.Loops")

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_FILE = os.getenv("DJ_LOOPS_INDEX") or os.path.join(HERE, "loops", "index.json")
SR = 48_000

PARTS = ("guitar", "piano", "bass", "drums")
TONAL = ("guitar", "piano", "bass")
#: Each loop is four bars, two two-bar halves that repeat.
LOOP_BEATS = 16
#: How far a loop may be stretched (either way) and shifted (semitones).
MAX_STRETCH = 0.12
MAX_SHIFT = 2
#: A loop played at this level (dBFS RMS) before the routine's own gains.
TARGET_DB = {"guitar": -19.0, "piano": -20.0, "bass": -19.0, "drums": -17.0}

_RATE = 12_000                     # analysis: plenty for loudness every sixteenth
_STEP = 4                          # analysis frames per beat
# What each part has to be, at least, to count as a riff on its own.
_MIN_SHARE = {"guitar": 0.28, "piano": 0.28, "bass": 0.24, "drums": 0.42}
# A riff the singer sings over a little is still a riff (its stem carries next to none of the voice).
_MAX_VOCAL = {"guitar": 0.22, "piano": 0.22, "bass": 0.22, "drums": 0.15}
#: Loops of each part kept from one song, at most (the best, in different places).
_PER_SONG = {"guitar": 2, "piano": 2, "bass": 2, "drums": 1}
_MIN_REPEAT = {"guitar": 0.55, "piano": 0.55, "bass": 0.6, "drums": 0.7}
_MIN_SCORE = 1.6
#: Bumped when finding changes, so build() looks at every song again.
FIND_VERSION = 4

#: Played lately (ids): picked against, so the same riff doesn't come round every mix.
recent = deque(maxlen=24)
#: What was played over what, lately (newest last), for the crate page: {"id", "at", "over"}.
played = deque(maxlen=30)
RATINGS_FILE = os.getenv("DJ_LOOPS_RATINGS") or os.path.join(HERE, "loops", "ratings.json")
_ratings = {"mtime": None, "data": {"votes": {}, "approved_only": False}}
_ratings_lock = threading.Lock()

_index = {"mtime": None, "loops": []}
_index_lock = threading.Lock()


# --------------------------------------------------------------------------
# Keys
# --------------------------------------------------------------------------

def _major_pc(camelot):
    """The pitch class of the major key with these notes (a minor key: its relative major), or None."""
    code = mixer._camelot(camelot)
    if not code:
        return None
    return mixer._CAMELOT_MAJOR.index(code[0])


def key_shift(source, target):
    """(semitones, cost): how far to move a loop in key `source` to sit in `target`
    (the same notes, or the neighbour a fifth either way round the wheel), at
    most MAX_SHIFT; None when it can't."""
    a, b = _major_pc(source), _major_pc(target)
    if a is None or b is None:
        return None
    best = None
    for shift in range(-MAX_SHIFT, MAX_SHIFT + 1):
        gap = (a + shift - b) % 12
        if gap == 0:
            cost = 0.0
        elif gap in (5, 7):
            cost = 0.45
        else:
            continue
        cost += 0.25 * abs(shift)
        if best is None or cost < best[1]:
            best = (shift, cost)
    return best


def stretch(source_bpm, target_bpm):
    """(factor, rubberband tempo) for playing a loop at target_bpm: factor 0.5/1/2
    is half / straight / double time. None beyond MAX_STRETCH."""
    if not source_bpm or not target_bpm:
        return None
    best = None
    for factor in (0.5, 1.0, 2.0):
        tempo = target_bpm / (source_bpm * factor)
        if abs(math.log(tempo)) <= math.log(1 + MAX_STRETCH) and (best is None or abs(math.log(tempo)) < abs(math.log(best[1]))):
            best = (factor, tempo)
    return best


# --------------------------------------------------------------------------
# Finding them
# --------------------------------------------------------------------------

def _read(path, channels):
    """A stems file (FLAC or .npy) as float32 (frames, channels) at _RATE."""
    if path.endswith(".npy"):
        data = np.load(path, mmap_mode="r")            # (parts, n, 2) at 48 kHz
        return np.ascontiguousarray(data[:, ::SR // _RATE, :].transpose(1, 0, 2).reshape(-1, channels)).astype(np.float32) / 32768
    result = subprocess.run(["nice", "-n", "15", "ffmpeg", "-nostdin", "-loglevel", "error", "-i", path,
                             "-f", "s16le", "-ac", str(channels), "-ar", str(_RATE), "pipe:1"],
                            capture_output=True, timeout=300)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode(errors="replace")[-200:])
    data = np.frombuffer(result.stdout, "<i2")
    return data[: len(data) // channels * channels].reshape(-1, channels).astype(np.float32) / 32768


def _parts(digest):
    """{part: mono float32 at _RATE} for drums, bass, vocals (and guitar, piano when it has them)."""
    main = next((p for p in (stems.stems_path(digest), stems.stems_flac_path(digest)) if os.path.exists(p)), None)
    if main is None:
        return None
    data = _read(main, 6)
    out = {name: data[:, 2 * i:2 * i + 2].mean(axis=1) for i, name in enumerate(stems.NAMES)}
    extra = next((p for p in (stems.extra_path(digest), stems.extra_flac_path(digest)) if os.path.exists(p)), None)
    if extra is not None:
        data = _read(extra, 4)
        n = min(len(data), len(out["drums"]))
        for i, name in enumerate(stems.EXTRA_NAMES):
            out[name] = data[:n, 2 * i:2 * i + 2].mean(axis=1)
        for name in stems.NAMES:
            out[name] = out[name][:n]
    return out


def _envelopes(parts, bpm, first_beat):
    """RMS of each part every sixteenth from the first beat: {part: float array}."""
    hop = 60.0 / bpm / _STEP * _RATE
    start = first_beat * _RATE
    n = min(len(x) for x in parts.values())
    count = int((n - start) // hop)
    if count <= LOOP_BEATS * _STEP * 2:
        return None, 0
    edges = np.round(start + np.arange(count + 1) * hop).astype(np.int64)
    out = {}
    for name, x in parts.items():
        cs = np.concatenate([[0.0], np.cumsum(np.square(x[:n].astype(np.float64)))])
        out[name] = np.sqrt(np.maximum(0.0, cs[edges[1:]] - cs[edges[:-1]]) / np.diff(edges))
    return out, count


def _corr(a, b):
    a = a - a.mean()
    b = b - b.mean()
    d = math.sqrt(float((a * a).sum() * (b * b).sum()))
    return float((a * b).sum() / d) if d > 1e-12 else 0.0


def _chroma(x, low, high):
    """Mean pitch-class profile of a mono signal at _RATE, bins low..high Hz."""
    n_fft = 4096
    if len(x) < n_fft:
        return None
    frames = np.lib.stride_tricks.sliding_window_view(x, n_fft)[:: n_fft // 2] * np.hanning(n_fft)
    mag = np.abs(np.fft.rfft(frames, axis=1))
    freqs = np.fft.rfftfreq(n_fft, 1 / _RATE)
    use = (freqs >= low) & (freqs <= high)
    pcs = np.round(12 * np.log2(freqs[use] / 440) + 69).astype(int) % 12
    chroma = np.bincount(pcs, weights=np.log1p(mag[:, use] * 10).sum(axis=0), minlength=12)
    return chroma / (chroma.max() + 1e-9)


def _key_fit(chroma, camelot):
    """How well a loop's notes sit in its song's key (Pearson against the key's profile), or None."""
    code = mixer._camelot(camelot)
    if chroma is None or not code:
        return None
    table = mixer._CAMELOT_MAJOR if code[1] == "B" else mixer._CAMELOT_MINOR
    tonic = table.index(code[0])
    profile = np.roll(mixer._MAJOR if code[1] == "B" else mixer._MINOR, tonic)
    return _corr(chroma, profile)


def find(parts, bpm, first_beat, camelot):
    """The best loops of each part in one song: [{part, beat, score, ...}]."""
    env, count = _envelopes(parts, bpm, first_beat)
    if env is None:
        return []
    frames = LOOP_BEATS * _STEP
    half = frames // 2
    power = {name: np.square(e) for name, e in env.items()}
    total = sum(power.values()) + 1e-12
    found = []
    for part in PARTS:
        if part not in env:
            continue
        e = env[part]
        # How loud the part gets in this song, a bar at a time.
        bars = len(e) // (4 * _STEP)
        per_bar = np.sqrt(np.mean(np.square(e[: bars * 4 * _STEP]).reshape(bars, -1), axis=1)) if bars else e
        loud_ref = float(np.percentile(per_bar, 90)) or 1e-9
        candidates = []
        for beat in range(0, count // _STEP - LOOP_BEATS + 1, 4):        # on every bar line
            a = beat * _STEP
            window = slice(a, a + frames)
            share = float(power[part][window].sum() / total[window].sum())
            vocal = float(power["vocals"][window].sum() / total[window].sum())
            if share < _MIN_SHARE[part] or vocal > _MAX_VOCAL[part]:
                continue
            first, second = e[a:a + half], e[a + half:a + frames]
            repeat = _corr(first, second)
            if repeat < _MIN_REPEAT[part]:
                continue
            l1, l2 = float(np.sqrt(np.mean(first ** 2))), float(np.sqrt(np.mean(second ** 2)))
            steady = min(l1, l2) / max(l1, l2, 1e-9)
            loud = min(1.0, math.sqrt(l1 * l1 + l2 * l2) / math.sqrt(2) / loud_ref)
            if steady < 0.7 or loud < 0.45:
                continue
            # Something has to happen in it: hits, not one held chord.
            punch = float(np.maximum(0.0, np.diff(e[window])).mean() / (e[window].mean() + 1e-9))
            if punch < (0.12 if part == "drums" else 0.06):
                continue
            score = 1.4 * share + 1.2 * repeat + 0.5 * steady + 0.5 * loud + min(0.4, punch) - 3 * vocal
            candidates.append({"part": part, "beat": beat, "score": score, "share": share, "repeat": repeat})
        kept = []
        for best in sorted(candidates, key=lambda c: -c["score"]):
            if len(kept) >= _PER_SONG[part] or best["score"] < _MIN_SCORE:
                break
            if any(abs(best["beat"] - k["beat"]) < 2 * LOOP_BEATS for k in kept):
                continue                                   # the same riff again, a bar on
            if part in TONAL:
                # Its notes have to be its song's key, or moving it to another song's key means nothing.
                hop = 60.0 / bpm * _RATE
                a = int((first_beat + best["beat"] * 60.0 / bpm) * _RATE)
                x = parts[part][a:a + int(LOOP_BEATS * hop)]
                chroma = _chroma(x, 40, 400) if part == "bass" else _chroma(x, 110, 2000)
                fit = _key_fit(chroma, camelot)
                if fit is None or fit < 0.45:
                    continue
                best["key_fit"] = round(fit, 3)
                best["chroma"] = [round(float(v), 3) for v in chroma]
                best["score"] += 0.5 * fit
            kept.append(best)
        found += kept
    return found


def _song_meta(tracks, digest):
    """The analysis (bpm, first beat, key, energy) and a title for a cached song, from any of its track keys."""
    keys = [key for key, value in tracks.items() if value.get("hash") == digest]
    meta = mixer.cache.get(*keys) if keys else None
    title = next((tracks[k].get("title") for k in keys if tracks[k].get("title")), None)
    return meta, keys, title


def build(limit=None, log=print):
    """Go through the stems cache and write the crate's index. Returns how many loops."""
    index = stems.load_index()
    tracks = index.get("tracks", {})
    digests = sorted({value.get("hash") for value in tracks.values() if value.get("hash")})
    try:
        with open(INDEX_FILE, encoding="utf-8") as f:
            old = json.load(f)
    except (OSError, ValueError):
        old = {}
    done = {song["digest"]: song for song in old.get("songs", [])}
    songs, loops = [], []
    started = time.time()
    for n, digest in enumerate(digests):
        if limit and n >= limit:
            break
        meta, keys, title = _song_meta(tracks, digest)
        if not meta or not meta.get("bpm") or (meta.get("bpm_confidence") or 0) < 0.3 or not stems.has_stems(digest):
            continue
        bpm = float(meta["bpm"]) * float(meta.get("bpm_scale") or 1.0)
        first_beat = float(meta.get("first_beat") or 0.0)
        camelot = meta.get("camelot")
        record = done.get(digest)
        has_extra = any(os.path.exists(p) for p in (stems.extra_path(digest), stems.extra_flac_path(digest)))
        if not (record and record.get("bpm") == bpm and record.get("extra") == has_extra
                and record.get("version") == FIND_VERSION):
            try:
                parts = _parts(digest)
                found = find(parts, bpm, first_beat, camelot) if parts else []
            except Exception as error:
                log(f"skip {digest} ({title}): {error}")
                continue
            record = {"digest": digest, "bpm": bpm, "extra": has_extra, "version": FIND_VERSION, "found": found}
        songs.append(record)
        for loop in record["found"]:
            loops.append({
                "id": f"{digest[:12]}-{loop['part']}-{loop['beat']}", "digest": digest, "keys": keys[:4],
                "title": title, "part": loop["part"], "bpm": bpm, "camelot": camelot,
                "energy": meta.get("energy"), "start": round(first_beat + loop["beat"] * 60.0 / bpm, 4),
                "beats": LOOP_BEATS, "score": round(loop["score"], 3),
                **({"chroma": loop["chroma"]} if loop.get("chroma") else {}),
            })
        if n % 25 == 0:
            log(f"{n}/{len(digests)} songs, {len(loops)} loops, {time.time() - started:.0f}s")
    os.makedirs(os.path.dirname(INDEX_FILE), exist_ok=True)
    tmp = INDEX_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"built": time.time(), "songs": songs, "loops": loops}, f)
    os.replace(tmp, INDEX_FILE)
    return len(loops)


# --------------------------------------------------------------------------
# Using them
# --------------------------------------------------------------------------

def crate():
    """Every loop in the index (re-read when the file changes)."""
    try:
        mtime = os.path.getmtime(INDEX_FILE)
    except OSError:
        return []
    with _index_lock:
        if _index["mtime"] != mtime:
            try:
                with open(INDEX_FILE, encoding="utf-8") as f:
                    data = json.load(f)
                _index["loops"] = [loop for loop in data.get("loops", []) if loop.get("part") in PARTS]
            except (OSError, ValueError):
                logger.warning("Could not read the loop crate", exc_info=True)
                _index["loops"] = []
            _index["mtime"] = mtime
        return _index["loops"]


def enabled():
    return os.getenv("DJ_LOOPS", "1") != "0" and bool(crate())


# --------------------------------------------------------------------------
# What people think of them (the crate page: loops_ui.py)
# --------------------------------------------------------------------------

def ratings():
    """{"votes": {id: "up" | "down"}, "approved_only": bool}: re-read when the file changes."""
    with _ratings_lock:
        try:
            mtime = os.path.getmtime(RATINGS_FILE)
        except OSError:
            mtime = None
        if (RATINGS_FILE, mtime) != _ratings["mtime"]:
            data = {"votes": {}, "approved_only": False}
            if mtime is not None:
                try:
                    with open(RATINGS_FILE, encoding="utf-8") as f:
                        raw = json.load(f)
                    data["votes"] = {str(k): v for k, v in (raw.get("votes") or {}).items() if v in ("up", "down")}
                    data["approved_only"] = bool(raw.get("approved_only"))
                except (OSError, ValueError):
                    logger.warning("Could not read the loop ratings", exc_info=True)
            _ratings["data"], _ratings["mtime"] = data, (RATINGS_FILE, mtime)
        return _ratings["data"]


def _save_ratings(data):
    os.makedirs(os.path.dirname(RATINGS_FILE), exist_ok=True)
    tmp = RATINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, RATINGS_FILE)
    with _ratings_lock:
        _ratings["mtime"] = None


def vote(loop_id, value):
    """Thumbs up ("up"), down ("down": never played again) or back to unrated (None)."""
    if value not in ("up", "down", None):
        raise ValueError("A vote is up, down or none.")
    if not any(loop["id"] == loop_id for loop in crate()):
        raise LookupError("No such loop.")
    data = {**ratings(), "votes": dict(ratings()["votes"])}
    if value is None:
        data["votes"].pop(loop_id, None)
    else:
        data["votes"][loop_id] = value
    _save_ratings(data)
    return data


def set_approved_only(on):
    """Only thumbed-up loops get played (the others wait for someone to listen to them)."""
    data = {**ratings(), "approved_only": bool(on)}
    _save_ratings(data)
    return data


def remember(loop_id, over=None):
    """A loop just went out over a mix (for the crate page's "played lately")."""
    recent.append(loop_id)
    played.append({"id": loop_id, "at": time.time(), "over": over})


def pick(parts, bpm, camelot, energy=None, exclude=(), rng=random, keys_also=None, against=None):
    """A loop for a mix at `bpm` in key `camelot`: {"loop", "shift", "factor", "tempo", "harmony"}, or None.

    parts: the parts wanted, in order of preference (the first one counts a little more).
    exclude: song digests not to borrow from (the songs being mixed).
    keys_also: another key the loop has to fit too (the other song, when they differ).
    against: (deck, t0, t1) windows of the music the loop will play over. A riff is then moved to whichever key
    (two semitones either way at most) makes its notes agree best with all of them, by ear rather than by the
    songs' key labels (which are often wrong), and left out unless they agree to HARMONY_MIN.
    """
    if not enabled():
        return None
    heard = {}

    def music(part):
        kind = "bass" if part == "bass" else "music"
        if kind not in heard:
            heard[kind] = [c for c in (_music_chroma_or_none(deck_music(deck, t0, t1, part), part)
                                       for deck, t0, t1 in against or ()) if c is not None]
        return heard[kind]
    rated = ratings()
    votes, approved_only = rated["votes"], rated["approved_only"]
    best = None
    for loop in crate():
        if loop["part"] not in parts or loop["digest"] in exclude:
            continue
        verdict = votes.get(loop["id"])
        if verdict == "down" or (approved_only and verdict != "up"):
            continue
        timing = stretch(loop.get("bpm"), bpm)
        if timing is None:
            continue
        agree = None
        if loop["part"] == "drums":
            shift = (0, 0.0)                     # drums have no key: never shifted
        elif loop.get("chroma") and music(loop["part"]):
            by_ear = best_shift(np.asarray(loop["chroma"], np.float32), music(loop["part"]))
            if by_ear is None:
                continue
            shift, agree = (by_ear[0], 0.15 * abs(by_ear[0]) + 2.0 * (1 - by_ear[1])), by_ear[1]
        else:
            shift = key_shift(loop.get("camelot"), camelot)
            if shift is None:
                continue
            if keys_also and keys_also != camelot:
                other = key_shift(loop.get("camelot"), keys_also)
                if other is None or other[0] != shift[0]:
                    continue
        cost = shift[1] + 4 * abs(math.log(timing[1])) + 0.3 * parts.index(loop["part"]) - 0.25 * loop.get("score", 0)
        if energy is not None and loop.get("energy") is not None:
            cost += 1.2 * abs(loop["energy"] - energy)
        if loop["id"] in recent:
            cost += 2.0
        if verdict == "up":
            cost -= 0.6
        cost += rng.random() * 0.6
        if best is None or cost < best[0]:
            best = (cost, {"loop": loop, "shift": shift[0], "factor": timing[0], "tempo": timing[1], "harmony": agree})
    return best[1] if best else None


def best_shift(loop_chroma, musics):
    """(semitones, agreement): the move (at most MAX_SHIFT either way) that makes a riff's pitch-class profile
    agree best with every one of `musics` (the worst of them counts); None if none reaches HARMONY_MIN."""
    best = None
    for shift in range(-MAX_SHIFT, MAX_SHIFT + 1):
        moved = np.roll(loop_chroma, shift)
        worst = min(_corr(moved, m) for m in musics)
        if best is None or worst > best[1] + 1e-6 or (abs(worst - best[1]) <= 1e-6 and abs(shift) < abs(best[0])):
            best = (shift, worst)
    return best if best and best[1] >= HARMONY_MIN else None


def _source(loop):
    """(path, first channel) holding the loop's part."""
    digest = loop["digest"]
    if loop["part"] in stems.EXTRA_NAMES:
        paths, channel = (stems.extra_path(digest), stems.extra_flac_path(digest)), 2 * stems.EXTRA_NAMES.index(loop["part"])
    else:
        paths, channel = (stems.stems_path(digest), stems.stems_flac_path(digest)), 2 * stems.NAMES.index(loop["part"])
    path = next((p for p in paths if os.path.exists(p)), None)
    if path is None:
        raise FileNotFoundError(f"{loop['part']} of {digest}")
    return path, channel


def render(choice, bpm):
    """The chosen loop at `bpm`, float32 (n, 2) at 48 kHz, exactly its bars long
    (LOOP_BEATS * factor beats of `bpm`), at the crate's level, with the seam smoothed."""
    loop = choice["loop"]
    path, channel = _source(loop)
    seconds = loop["beats"] * 60.0 / loop["bpm"]
    pad = 0.5                                 # either side, so the stretcher's edges fall outside the loop
    start = max(0.0, loop["start"] - pad)
    lead = loop["start"] - start
    filters = [f"pan=stereo|c0=c{channel}|c1=c{channel + 1}"]
    tempo, pitch = choice["tempo"], 2 ** (choice["shift"] / 12)
    if abs(tempo - 1) > 1e-4 or choice["shift"]:
        detail = "crisp" if loop["part"] == "drums" else "mixed"
        filters.append(f"rubberband=tempo={tempo:.6f}:pitch={pitch:.6f}:transients={detail}:pitchq=quality")
    command = ["nice", "-n", "5", "ffmpeg", "-nostdin", "-loglevel", "error"]
    if path.endswith(".npy"):
        data = np.load(path, mmap_mode="r")
        part = (stems.EXTRA_NAMES if loop["part"] in stems.EXTRA_NAMES else stems.NAMES).index(loop["part"])
        a, b = int(start * SR), int((loop["start"] + seconds + pad) * SR)
        raw = np.ascontiguousarray(data[part, a:b]).astype("<i2").tobytes()
        filters[0] = "anull"
        command += ["-f", "s16le", "-ar", str(SR), "-ac", "2", "-i", "pipe:0"]
    else:
        raw = None
        command += ["-ss", f"{start:.4f}", "-t", f"{lead + seconds + pad:.4f}", "-i", path]
    command += ["-af", ",".join(filters), "-f", "f32le", "-ar", str(SR), "-ac", "2", "pipe:1"]
    result = subprocess.run(command, input=raw, capture_output=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode(errors="replace")[-200:])
    audio = np.frombuffer(result.stdout, "<f4").reshape(-1, 2)
    length = int(round(loop["beats"] * choice["factor"] * 60.0 / bpm * SR))
    offset = int(round(lead / tempo * SR))
    out = np.zeros((length, 2), np.float32)
    piece = audio[offset:offset + length]
    out[: len(piece)] = piece
    # The seam: the loop's last few milliseconds fade into what came just before its start.
    fade = int(0.006 * SR)
    before = audio[max(0, offset - fade):offset]
    if len(before) == fade:
        ramp = np.linspace(0, 1, fade, dtype=np.float32)[:, None]
        out[-fade:] = out[-fade:] * (1 - ramp) + before * ramp
    rms = float(np.sqrt(np.mean(np.square(out)))) if len(out) else 0.0
    if rms > 1e-5:
        out *= 10 ** (TARGET_DB[loop["part"]] / 20) / rms
    return out


# --------------------------------------------------------------------------
# Does it fit what's playing?
# --------------------------------------------------------------------------

#: How alike a riff's notes and the music under it have to be (Pearson of their pitch-class profiles; see
#: best_shift()). Measured on the crate: a riff against its own song ~0.90, other songs in the same key ~0.74
#: (median), songs labelled as clashing ~0.55 (the labels are often wrong); a fifth of same-key pairs are below 0.5.
HARMONY_MIN = 0.5


def _music_chroma(x48, part):
    """Pitch-class profile of mono float audio at 48 kHz (bass notes for a bass line, else chords and melody)."""
    from scipy import signal
    x = signal.resample_poly(np.asarray(x48, np.float32), 1, SR // _RATE)
    return _chroma(x, 40, 400) if part == "bass" else _chroma(x, 110, 2000)


def _music_chroma_or_none(x48, part):
    return None if x48 is None else _music_chroma(x48, part)


def deck_music(deck, t0, t1, part):
    """What a loaded deck plays between t0 and t1 seconds that a riff has to agree with, as mono float at 48 kHz:
    its bass line for a borrowed bass, else everything but the drums (the whole mix without stems); None if empty."""
    audio = getattr(deck, "audio", None)
    if audio is None:
        return None
    a, b = max(0, int(t0 * SR)), min(len(audio), int(t1 * SR))
    if b - a < SR:
        return None
    stems_ = getattr(deck, "stems", None)
    if stems_ is not None:
        if part == "bass":
            return stems_[1, a:b].astype(np.float32).mean(axis=1) / 32768
        return (audio[a:b].astype(np.float32) - stems_[0, a:b]).mean(axis=1) / 32768
    return audio[a:b].astype(np.float32).mean(axis=1) / 32768


def describe(choice):
    """What the booth and the club say about a loop: its part, the song it's from, its id (to rate it)."""
    loop = choice["loop"]
    return {"part": loop["part"], "title": loop.get("title"), "shift": choice["shift"], "id": loop["id"]}


def preview(loop_id, passes=2):
    """A loop as it is (its own tempo and key), `passes` times round: WAV bytes for the crate page."""
    import io
    import wave
    loop = next((x for x in crate() if x["id"] == loop_id), None)
    if loop is None:
        raise LookupError("No such loop.")
    audio = render({"loop": loop, "shift": 0, "factor": 1.0, "tempo": 1.0}, loop["bpm"])
    pcm = (np.clip(np.tile(audio, (passes, 1)), -1, 1) * 32767).astype("<i2")
    out = io.BytesIO()
    with wave.open(out, "wb") as f:
        f.setnchannels(2)
        f.setsampwidth(2)
        f.setframerate(SR)
        f.writeframes(pcm.tobytes())
    return out.getvalue()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) > 1 and sys.argv[1] == "build":
        count = build(limit=int(sys.argv[2]) if len(sys.argv) > 2 else None)
        print(f"{count} loops in {INDEX_FILE}")
    else:
        print("usage: python loops.py build [limit]")
