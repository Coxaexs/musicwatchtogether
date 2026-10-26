"""The DJ's mood stations: Heavy rock, Indie rock, Sad, Pop, Dreamy, Party, Türkçe, Chill.

Not playlists. A station is a mood the DJ plays to: autoplay draws its songs from the whole library by that mood,
Real DJ picks each next song from the queue with the station's songs first and its energy where the station sits,
and it mixes in the station's vibe (a heavy rock night drops and cuts like a club peak, a sad one blends long and
slow). Songs anyone queues still play.

Which songs belong where: dj_brain/stations.json, made by scripts/dj-analysis/classify_stations.py (Gemma reading
each song's title and artist, and Gemini's genres and moods where it listened). A song is in one to three
stations, the best first. Read lazily, re-read when the file changes; never raises; nothing without the file.
"""

import json
import logging
import os
import threading
import time

logger = logging.getLogger("MusicBot.Stations")

HERE = os.path.dirname(os.path.abspath(__file__))
FILE = os.path.join(os.environ.get("DJ_BRAIN_DIR") or os.path.join(HERE, "dj_brain"), "stations.json")

#: id -> label, emoji, the vibe Real DJ plays it in, and the energy (0..1) its set sits around.
STATIONS = {
    "heavy": {"label": "Heavy rock", "emoji": "🤘", "vibe": "hype", "energy": 0.78},
    "indie": {"label": "Indie rock", "emoji": "🎸", "vibe": "club", "energy": 0.62},
    "sad": {"label": "Sad", "emoji": "💔", "vibe": "chill", "energy": 0.32},
    "pop": {"label": "Pop", "emoji": "🎤", "vibe": "club", "energy": 0.62},
    "dreamy": {"label": "Dreamy", "emoji": "🌙", "vibe": "chill", "energy": 0.4},
    "party": {"label": "Party", "emoji": "🪩", "vibe": "hype", "energy": 0.82},
    "turkish": {"label": "Türkçe", "emoji": "🇹🇷", "vibe": "club", "energy": 0.55},
    "chill": {"label": "Chill", "emoji": "🌿", "vibe": "chill", "energy": 0.35},
}
RECHECK_S = 60.0

_lock = threading.Lock()
_state = {"songs": {}, "mtime": None, "checked": 0.0}


def _load():
    with _lock:
        now = time.monotonic()
        if now - _state["checked"] < RECHECK_S and _state["mtime"] is not None:
            return _state["songs"]
        _state["checked"] = now
        try:
            mtime = os.path.getmtime(FILE)
        except OSError:
            _state.update(songs={}, mtime=None)
            return {}
        if mtime != _state["mtime"]:
            try:
                with open(FILE, encoding="utf-8") as f:
                    raw = json.load(f).get("songs") or {}
                _state["songs"] = {vid: {"in": [s for s in item.get("in") or [] if s in STATIONS], "title": item.get("title")}
                                   for vid, item in raw.items() if isinstance(item, dict)}
                logger.info("Mood stations loaded: %d songs", len(_state["songs"]))
            except Exception:
                logger.warning("stations.json could not be read", exc_info=True)
                _state["songs"] = {}
            _state["mtime"] = mtime
        return _state["songs"]


def catalog():
    """The stations for the booth and the club: [{id, label, emoji, songs}]."""
    songs = _load()
    return [{"id": sid, "label": s["label"], "emoji": s["emoji"], "songs": sum(sid in v["in"] for v in songs.values())}
            for sid, s in STATIONS.items()]


def _song_id(keys):
    for key in keys or ():
        if isinstance(key, str) and key.startswith("yt:"):
            return key[3:]
    return None


def rank(keys, station):
    """Where a song stands in a station: 0 its best fit, 1 or 2 a fit too; None when it isn't in it (or unknown)."""
    if station not in STATIONS:
        return None
    found = _load().get(_song_id(keys))
    if not found or station not in found["in"]:
        return None
    return found["in"].index(station)


def known(keys):
    """Whether the stations know this song at all."""
    return _song_id(keys) in _load()


def songs(station):
    """The station's songs: [(youtube id, title, rank)], best fits first."""
    found = [(vid, item.get("title"), item["in"].index(station)) for vid, item in _load().items() if station in item["in"]]
    return sorted(found, key=lambda x: x[2])


def score(keys, station):
    """What Real DJ's pick adds for the station (lower is better): a best fit -0.9, a fit -0.5, a song it knows
    to be elsewhere +1.3, a song it doesn't know +0.4 (a request, or a song added since the stations were made)."""
    if station not in STATIONS:
        return 0.0
    r = rank(keys, station)
    if r is not None:
        return -0.9 if r == 0 else -0.5
    return 1.3 if known(keys) else 0.4
