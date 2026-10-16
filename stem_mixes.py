"""Playlists made from the stems library ("Stem mix: …").

Each day the library is sorted into a few playlists by tempo, key and energy,
ordered so that each song mixes well into the next (keys close on the Camelot
wheel, tempos a deck can match). A playlist may also take up to MAX_MISSING
songs that would fit but have no stems yet: songs from saved playlists,
favourites and play history that were already analysed. Those still play, and
the caller queues them for separation so the next day's mixes can use them as
stems too.

Everything here is pure: songs in, playlists out. webui.py gathers the songs.

A song is a dict: title, url, bpm, camelot, energy, duration (seconds),
stemmed (bool) and karaoke (bool, instrumental ready).
"""

import datetime
import math
import random

import mixer

PREFIX = "Stem mix: "
SIZE = 25
MAX_MISSING = 5
MIN_SIZE = 6


def _energy(song):
    value = song.get("energy")
    return 0.5 if value is None else float(value)


def _tempo_gap(a, b):
    ratio = mixer.tempo_ratio(a.get("bpm"), b.get("bpm"))
    return abs(math.log(ratio)) if ratio else 0.5


def _cost(a, b, rising=False):
    """How rough going from a to b would be: key clash, tempo gap, energy jump."""
    cost = mixer.key_distance(a.get("camelot"), b.get("camelot")) + _tempo_gap(a, b) * 12
    jump = _energy(b) - _energy(a)
    cost += abs(jump) * 2
    if rising and jump < 0:
        cost += -jump * 4
    return cost


def order(songs, rng, rising=False):
    """Greedy DJ order: each next song is the smoothest step from the last."""
    left = list(songs)
    if not left:
        return []
    if rising:
        first = min(left, key=_energy)
    else:
        first = rng.choice(left)
    out = [first]
    left.remove(first)
    while left:
        last = out[-1]
        nxt = min(left, key=lambda s: _cost(last, s, rising) + rng.random() * 0.4)
        out.append(nxt)
        left.remove(nxt)
    return out


def harmonic_run(songs, rng, attempts=24):
    """The longest chain where every step keeps the key (≤1 on the wheel) and
    a tempo the decks can match (within 6%)."""
    best = []
    pool = list(songs)
    for _ in range(min(attempts, len(pool))):
        start = rng.choice(pool)
        chain, left = [start], [s for s in pool if s is not start]
        while left and len(chain) < SIZE:
            last = chain[-1]
            fits = [s for s in left
                    if mixer.key_distance(last.get("camelot"), s.get("camelot")) <= 1 and _tempo_gap(last, s) <= 0.06]
            if not fits:
                break
            nxt = min(fits, key=lambda s: _cost(last, s) + rng.random() * 0.3)
            chain.append(nxt)
            left.remove(nxt)
        if len(chain) > len(best):
            best = chain
    return best


def _pick(pool, rng):
    """Up to SIZE songs, stemmed ones first, at most MAX_MISSING without stems."""
    stemmed = [s for s in pool if s.get("stemmed")]
    missing = [s for s in pool if not s.get("stemmed")]
    rng.shuffle(stemmed)
    rng.shuffle(missing)
    take_missing = min(MAX_MISSING, len(missing), max(0, SIZE - min(len(stemmed), SIZE - 2)))
    return stemmed[:SIZE - take_missing] + missing[:take_missing]


def _bpm(song):
    """Tempo folded into 70–140, so a half-time 75 and a 150 count the same."""
    bpm = song.get("bpm") or 0
    while bpm and bpm < 70:
        bpm *= 2
    while bpm > 140:
        bpm /= 2
    return bpm


MIXES = (
    ("Warm-up", "Easy tempos and lower energy, getting warmer song by song.",
     lambda s: _energy(s) < 0.62 and 80 <= _bpm(s) <= 125, True),
    ("Peak time", "The high-energy end of the library, kept in key and tempo.",
     lambda s: _energy(s) >= 0.62, False),
    ("Karaoke night", "Songs with their vocals split out, ready to sing over.",
     lambda s: s.get("karaoke") or not s.get("stemmed"), False),
    ("Chill", "Slow and soft, for the end of the night.",
     lambda s: _energy(s) < 0.45 or _bpm(s) < 92, True),
)

HARMONIC = ("Smooth harmonic run", "Every song shares a key and a matchable tempo with the next one, made for the DJ booth.")


def build(songs, day=None):
    """The day's stem mixes: [{"name", "description", "songs": [song, ...]}]."""
    day = day or datetime.date.today()
    usable = [s for s in songs if s.get("bpm") and s.get("url")]
    out = []
    for title, description, keep, rising in MIXES:
        rng = random.Random(f"{day.isoformat()}:{title}")
        picked = _pick([s for s in usable if keep(s)], rng)
        if sum(1 for s in picked if s.get("stemmed")) >= MIN_SIZE:
            out.append({"name": PREFIX + title, "description": description, "songs": order(picked, rng, rising)})
    rng = random.Random(f"{day.isoformat()}:harmonic")
    stemmed = [s for s in usable if s.get("stemmed")]
    run = harmonic_run(stemmed, rng)
    if len(run) >= MIN_SIZE:
        out.append({"name": PREFIX + HARMONIC[0], "description": HARMONIC[1], "songs": run})
    return out
