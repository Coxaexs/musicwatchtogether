"""Mixes made by hand in the booth's mix editor, kept per pair of songs.

A mix says how one particular song hands over to another: where on the old
song it starts, where the new song comes in, how many beats it takes, and a
curve for every control over those beats (each deck's volume, its bass, mids
and highs, its filter, and its drums, bass, vocals and music when the songs
have stems). Auto DJ and Real DJ play a pair's saved mix whenever those two
songs meet in that order, instead of planning one.

    {"beats": 64, "out_at": 162.4, "in_at": 0.19, "in_start": 0,
     "lanes": {"out.vol": [[0, 1], [48, 1], [64, 0]], "in.low": [[0, 0], [31, 0], [32, 1]], ...}}

A lane is a list of [beat, value] points joined by straight lines; before its
first point and after its last the value holds. A control without a lane
stays where it normally is (volume and tone full, filter off).
"""

import json
import logging
import os
import threading
import time

logger = logging.getLogger("MusicBot.Mixes")

DATA_DIR = os.getenv("DATA_DIR") or os.path.dirname(os.path.abspath(__file__))
FILE = os.path.join(DATA_DIR, "dj_mixes.json")
SIDES = ("out", "in")
TONE = ("low", "mid", "hi")
STEMS = ("drums", "bass", "vocals", "other")
#: control -> (lowest, highest, where it rests)
RANGE = dict({"vol": (0.0, 1.0, 1.0), "filter": (-1.0, 1.0, 0.0)},
             **{name: (0.0, 1.0, 1.0) for name in TONE + STEMS})
CONTROLS = tuple(f"{side}.{name}" for side in SIDES for name in RANGE)
MAX_BEATS = 128
MAX_POINTS = 64

_lock = threading.Lock()
_data = None


def clean(spec):
    """A mix as the editor sent it, checked and tidied. Raises ValueError with a sentence for the booth."""
    if not isinstance(spec, dict):
        raise ValueError("That mix can't be read.")
    try:
        beats = int(spec.get("beats") or 0)
    except (TypeError, ValueError):
        beats = 0
    if beats < 4 or beats > MAX_BEATS or beats % 4:
        raise ValueError("A mix is between 1 and 32 bars long.")

    def seconds(name):
        value = spec.get(name)
        if value is None:
            return None
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise ValueError("That mix can't be read.")
        if not 0 <= value < 3600:
            raise ValueError("That mix can't be read.")
        return round(value, 3)

    try:
        in_start = float(spec.get("in_start") or 0)
    except (TypeError, ValueError):
        in_start = 0.0
    in_start = min(max(0.0, in_start), beats - 1)
    lanes = {}
    for control, points in (spec.get("lanes") or {}).items():
        if control not in CONTROLS or not isinstance(points, list):
            continue
        low, high, _rest = RANGE[control.split(".", 1)[1]]
        tidy = []
        for point in points[:MAX_POINTS]:
            try:
                beat, value = float(point[0]), float(point[1])
            except (TypeError, ValueError, IndexError):
                continue
            if beat != beat or value != value:          # NaN
                continue
            tidy.append([round(min(max(0.0, beat), beats), 3), round(min(max(low, value), high), 3)])
        tidy.sort(key=lambda p: p[0])
        if tidy:
            lanes[control] = tidy
    return {"beats": beats, "out_at": seconds("out_at"), "in_at": seconds("in_at"),
            "in_start": round(in_start, 3), "lanes": lanes}


def value_at(points, beat):
    """A lane's value at `beat`."""
    if beat <= points[0][0]:
        return points[0][1]
    for (b0, v0), (b1, v1) in zip(points, points[1:]):
        if beat <= b1:
            return v1 if b1 <= b0 else v0 + (v1 - v0) * (beat - b0) / (b1 - b0)
    return points[-1][1]


def at(lanes, beat):
    """Every control that has a lane, at `beat` beats into the mix."""
    return {control: value_at(points, beat) for control, points in lanes.items() if points}


def song_id(info):
    """What a song is saved under: its YouTube id when it has one, else its first key, else its title."""
    keys = [str(k) for k in (info or {}).get("keys") or [] if k]
    for key in keys:
        if key.startswith("yt:"):
            return key
    if keys:
        return keys[0]
    title = str((info or {}).get("title") or "").strip().lower()
    return "title:" + title if title else None


def _load():
    global _data
    if _data is None:
        try:
            with open(FILE, encoding="utf-8") as f:
                loaded = json.load(f)
            _data = loaded if isinstance(loaded, dict) else {}
        except FileNotFoundError:
            _data = {}
        except (OSError, ValueError):
            logger.warning("dj_mixes.json could not be read; starting without saved mixes", exc_info=True)
            _data = {}
    return _data


def _save():
    tmp = FILE + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, FILE)


def _pair(out_info, in_info):
    a, b = song_id(out_info), song_id(in_info)
    return f"{a}\n{b}" if a and b and a != b else None


def get(out_info, in_info):
    """The mix saved for going from one song into another, or None."""
    pair = _pair(out_info, in_info)
    if not pair:
        return None
    with _lock:
        entry = _load().get(pair)
    return entry.get("mix") if entry else None


def put(out_info, in_info, spec, by=None):
    pair = _pair(out_info, in_info)
    if not pair:
        raise ValueError("These two songs can't be told apart, so a mix for them can't be saved.")
    mix = clean(spec)
    with _lock:
        _load()[pair] = {"mix": mix, "out": (out_info or {}).get("title"), "in": (in_info or {}).get("title"),
                         "by": by, "at": round(time.time())}
        _save()
    return mix


def delete(out_info, in_info):
    pair = _pair(out_info, in_info)
    with _lock:
        if pair and _load().pop(pair, None) is not None:
            _save()
            return True
    return False


def followers(out_info):
    """The ids of the songs that have a saved mix from this one."""
    a = song_id(out_info)
    if not a:
        return set()
    with _lock:
        return {pair.split("\n", 1)[1] for pair in _load() if pair.startswith(a + "\n")}
