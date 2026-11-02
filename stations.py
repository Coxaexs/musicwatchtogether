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
#: Where each room's lap through a station is kept (radio_next), so a restart does not start it over.
BAGS_FILE = os.path.join(HERE, "radio_bags.json")
#: A song the room heard this recently (plays, its links and titles counted) is passed over on the radio.
RADIO_RECENT = 60
#: A new lap's first songs are never the last lap's final ones.
LAP_GAP = 10

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


#: What people type for a station: /radio sad, /radio energetic, /radio türkçe ...
ALIASES = {
    "heavy": "heavy", "rock": "heavy", "metal": "heavy", "heavyrock": "heavy",
    "indie": "indie", "indierock": "indie", "alternative": "indie", "alt": "indie",
    "sad": "sad", "melancholy": "sad", "melancholic": "sad", "cry": "sad", "hüzünlü": "sad", "huzunlu": "sad",
    "pop": "pop",
    "dreamy": "dreamy", "dream": "dreamy", "night": "dreamy",
    "party": "party", "energetic": "party", "energy": "party", "hype": "party", "dance": "party",
    "turkish": "turkish", "türkçe": "turkish", "turkce": "turkish", "tr": "turkish",
    "chill": "chill", "calm": "chill", "relax": "chill", "lofi": "chill",
}

#: Best fits come round more often on the radio: a song's weight by its rank in the station.
RADIO_WEIGHTS = (3, 2, 1)


def station_id(text):
    """The station someone named (an id, a label, an alias, an emoji), or None."""
    raw = str(text or "").strip().lower()
    if not raw:
        return None
    for sid, s in STATIONS.items():
        if raw in (s["emoji"], s["label"].lower()):
            return sid
    return ALIASES.get("".join(ch for ch in raw if ch.isalnum()))


def label(station):
    """"💔 Sad", for messages."""
    s = STATIONS.get(station)
    return f"{s['emoji']} {s['label']}" if s else ""


def clean_title(title):
    """A title as people read it, not the file it was saved as: "NA - Song [abcdefghijk].mp3" -> "Song"; None for a
    bare download name (ytdl_abcdefghijk.mp3), which the song's own title replaces once it is looked up."""
    import re
    title = re.sub(r"\s*\[[A-Za-z0-9_-]{11}\]", "", str(title or ""))
    title = re.sub(r"\.(mp3|m4a|webm|opus|ogg|wav|flac)$", "", title, flags=re.I).strip()
    title = re.sub(r"^NA - ", "", title)
    return None if not title or re.fullmatch(r"ytdl_[A-Za-z0-9_-]{11}", title) else title


def youtube_id(url):
    """The YouTube id in a watch link, a yt: key or a bare id, or None."""
    value = str(url or "")
    for marker in ("v=", "youtu.be/", "/shorts/", "/embed/", "yt:"):
        if marker in value:
            vid = value.split(marker, 1)[1][:11]
            return vid if len(vid) == 11 else None
    return value if len(value) == 11 and value.replace("-", "").replace("_", "").isalnum() else None


_bags_lock = threading.Lock()


def _read_bags():
    try:
        with open(BAGS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_bags(data):
    tmp = BAGS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"))
        os.replace(tmp, BAGS_FILE)
    except OSError:
        logger.warning("Could not save the radio's place", exc_info=True)


def _lap(ranks, last, rng):
    """A new lap through the songs in `ranks` (youtube id -> rank in the station), with the previous lap's final
    songs (`last`) kept out of its start. A weighted shuffle: every song is in it once, but a best fit tends to
    come up earlier than a song that only fits a little (RADIO_WEIGHTS), so a listen of an hour or two leans on
    the mood's best songs while any song can still come first."""
    order = sorted(ranks, key=lambda vid: rng.random() ** (1 / RADIO_WEIGHTS[min(ranks[vid], 2)]), reverse=True)
    tail = set(last[-LAP_GAP:])
    if len(order) > 2 * LAP_GAP:
        head = [vid for vid in order if vid not in tail][:LAP_GAP]
        chosen = set(head)
        order = head + [vid for vid in order if vid not in chosen]
    return order


def radio_next(room, station, recent=(), never=(), rng=None):
    """The next song of a room's mood radio: (youtube id, title), or None without the station's songs.

    Every song of the station plays once before any plays again, in a shuffled order that leans its best fits
    towards the start (_lap); then the station is shuffled anew for the next lap, so it never repeats its order. The room's place is kept in BAGS_FILE.
    Songs added to the station mid-lap join the rest of it; what the room disliked (`never`) is left out, and
    what it heard lately (`recent`: links or titles, newest first) waits for the next lap."""
    import random
    rng = rng or random.Random()
    found = songs(station)
    titles = {vid: title for vid, title, _rank in found}
    ranks = {vid: rank for vid, _title, rank in found}
    if not titles:
        return None

    def keys(values):
        values = [str(v).lower() for v in values if v]
        return set(values) | {youtube_id(v) for v in values} - {None}

    banned = keys(never)
    heard = keys(list(recent)[:RADIO_RECENT])

    def ok(vid, skip):
        name = (titles.get(vid) or "").lower()
        return vid in titles and vid.lower() not in skip and name not in skip

    with _bags_lock:
        bags = _read_bags()
        bag = (bags.setdefault(str(room), {}).get(station) or {})
        order, pos = list(bag.get("order") or []), int(bag.get("pos") or 0)
        known = set(order)
        for vid in titles:                       # new to the station: somewhere in the rest of this lap
            if vid not in known:
                order.insert(rng.randrange(min(pos, len(order)), len(order) + 1), vid)
        pick = None
        for lap in range(2):
            while pos < len(order):
                vid = order[pos]
                pos += 1
                if ok(vid, banned | heard):
                    pick = vid
                    break
            if pick:
                break
            order, pos = _lap({vid: ranks[vid] for vid in titles if ok(vid, banned)}, order, rng), 0
            if lap and not pick:                 # everything was heard lately: the lap's first that isn't banned
                pick = order[0] if order else None
                pos = 1
        bags[str(room)][station] = {"order": order, "pos": pos}
        _write_bags(bags)
    return (pick, titles[pick]) if pick else None


def radio_pick(station, recent=(), never=(), rng=None):
    """The next song for a mood radio: (youtube id, title), or None without the station's songs.

    `recent` is what was played lately, newest first, and `never` what the room disliked (YouTube ids, links or
    titles). The radio never runs dry: when every song is recent it forgets the older half and tries again, so a
    station only repeats once it has been through the rest. Best fits come round more often (RADIO_WEIGHTS)."""
    import random
    rng = rng or random.Random()

    def keys(values):
        values = [str(v).lower() for v in values if v]
        return set(values) | {youtube_id(v) for v in values} - {None}

    banned = keys(never)
    pool = [(vid, title, r) for vid, title, r in songs(station)
            if vid.lower() not in banned and (title or "").lower() not in banned]
    if not pool:
        return None
    recent = list(recent)
    while True:
        skip = keys(recent)
        fresh = [(vid, title, r) for vid, title, r in pool if vid.lower() not in skip and (title or "").lower() not in skip]
        if fresh or not recent:
            break
        recent = recent[:len(recent) // 2]
    vid, title, _rank = rng.choices(fresh, weights=[RADIO_WEIGHTS[min(r, 2)] for _v, _t, r in fresh])[0]
    return vid, title

