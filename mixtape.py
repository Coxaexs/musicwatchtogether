"""Recordings of the booth's transitions, for the mix reviewer on Oracle.

Every mix from one song into the next is recorded as three tracks: what deck A and deck B each put into the mix
(after their EQ, fader and the crossfader) and the mix itself, mono at 24 kHz, from PRE seconds before it starts to
POST seconds after it ends. Two separate decks are what make a review possible: whether their beats line up and
whether their keys clash can only be heard apart. Oracle grades each one (huddle-ops brain_sync.py sends them,
hq_review.py there) and Real DJ learns which moves work (djbrain.review_bonus).

  DATA_DIR/dj_mix_reviews/<stamp>.flac  3 channels: A, B, mix (int16, 24 kHz)
  DATA_DIR/dj_mix_reviews/<stamp>.json  what the mix was: style, both songs, tempo, key, where it started

Only Hoffle rooms record (the session turns it on), the audio loop only copies 20 ms blocks into memory, and the
file is written by a thread. DJ_MIX_REVIEW=0 turns it off. At most KEEP recordings are kept.
"""

import json
import logging
import os
import queue
import subprocess
import threading
import time
from collections import deque

import numpy as np

logger = logging.getLogger("MusicBot.Mixtape")

SR = 48_000
DOWN = 2                      # 48 kHz -> 24 kHz: plenty for beats, keys and loudness
RATE = SR // DOWN
PRE = 8.0
POST = 8.0
MAX_SECONDS = 150.0           # a transition longer than this is cut (a stuck one)
KEEP = 400
DATA_DIR = os.getenv("DATA_DIR") or os.path.dirname(os.path.abspath(__file__))
FOLDER = os.path.join(DATA_DIR, "dj_mix_reviews")

_writes = queue.Queue()
_writer = None


def enabled():
    return os.environ.get("DJ_MIX_REVIEW", "1") != "0"


def _mono(block):
    """(n, 2) float -> (n / DOWN,) float32: both channels, every DOWN samples averaged."""
    if block is None:
        return None
    x = block.mean(axis=1) if block.ndim == 2 else block
    n = len(x) // DOWN * DOWN
    return x[:n].reshape(-1, DOWN).mean(axis=1).astype(np.float32)


def _beat(value):
    return None if value is None else round(float(value), 4)


def _deck_info(deck):
    info, meta = deck.info or {}, deck.meta or {}
    keys = info.get("keys") or []
    yt = next((k[3:] for k in keys if isinstance(k, str) and k.startswith("yt:")), None)
    return {"deck": deck.name, "title": info.get("title"), "yt": yt, "bpm": round(deck.bpm, 2) if deck.bpm else None,
            "beat_s": round(deck.beat_seconds, 5) if deck.bpm else None,
            "first_beat": round(deck.first_beat, 4) if deck.bpm else None,
            "camelot": meta.get("camelot"), "key_lock": bool(getattr(deck, "key_locked", False)),
            "pitch": round(float(getattr(deck, "pitch", 0.0) or 0.0), 2), "at": round(deck.seconds, 2)}


class MixRecorder:
    """Fed every rendered block by the engine (under its lock): cheap copies only."""

    def __init__(self, context=None):
        self.context = context or (lambda: {})      # the session's view: room, Auto / Real DJ, vibe
        self.pre = deque(maxlen=int(PRE * SR / 960) + 1)
        self.take = None

    def block(self, channels, mix, transition, beats=None):
        """channels: {"A": (n, 2) or None, "B": ...} as they reach the mix; mix: (n, 2); beats: {"A": where deck A
        is on its own beat grid (deck.beat_at) at the end of this block, or None, "B": ...}. With those, the
        reviewer tells a beat grid that sits off the song's kicks from decks that weren't matched."""
        m = _mono(mix)
        a = _mono(channels.get("A"))
        b = _mono(channels.get("B"))
        zeros = np.zeros_like(m)
        beats = beats or {}
        row = (a if a is not None else zeros, b if b is not None else zeros, m,
               (_beat(beats.get("A")), _beat(beats.get("B"))))
        take = self.take
        if take is None:
            self.pre.append(row)
            if transition is not None and transition.started and not transition.done and enabled():
                self._start(transition)
            return
        take["rows"].append(row)
        take["frames"] += len(m)
        if take["ends_at"] is None and (transition is not take["tr"] or take["tr"].done):
            take["ends_at"] = take["frames"]
        if take["ends_at"] is not None and take["frames"] - take["ends_at"] >= POST * RATE:
            self._finish()
        elif take["frames"] >= (PRE + MAX_SECONDS + POST) * RATE:
            take["ends_at"] = take["ends_at"] or take["frames"]
            self._finish(cut=True)

    def _start(self, tr):
        rows = list(self.pre)
        frames = sum(len(r[2]) for r in rows)
        try:
            context = dict(self.context() or {})
        except Exception:
            context = {}
        self.take = {"tr": tr, "rows": rows, "frames": frames, "starts_at": frames, "ends_at": None,
                     "meta": dict(context, style=tr.style, label=tr.label, beats=tr.beats,
                                  move=getattr(tr, "move", None), why=getattr(tr, "why", None),
                                  out=_deck_info(tr.out), inn=_deck_info(tr.inn), when=int(time.time()))}
        self.pre.clear()

    def _finish(self, cut=False):
        take, self.take = self.take, None
        # Each deck's place on its beat grid at the end of every block (block_s apart, the first block ending
        # block_s into the recording).
        meta = dict(take["meta"], rate=RATE, tracks=["A", "B", "mix"],
                    start=round(take["starts_at"] / RATE, 3), end=round(take["ends_at"] / RATE, 3),
                    cut=cut, landed=_deck_info(take["tr"].inn), block_s=round(len(take["rows"][0][2]) / RATE, 5),
                    grid={"A": [r[3][0] for r in take["rows"]], "B": [r[3][1] for r in take["rows"]]},
                    beat_s={"A": take["meta"]["out"].get("beat_s"), "B": take["meta"]["inn"].get("beat_s")})
        audio = np.stack([np.concatenate([r[i] for r in take["rows"]]) for i in range(3)], axis=1)
        _queue_write(audio, meta)


def _queue_write(audio, meta):
    global _writer
    _writes.put((audio, meta))
    if _writer is None or not _writer.is_alive():
        _writer = threading.Thread(target=_write_loop, name="mixtape", daemon=True)
        _writer.start()


def _write_loop():
    while True:
        audio, meta = _writes.get()
        try:
            _write(audio, meta)
        except Exception:
            logger.warning("Could not save a mix recording", exc_info=True)


def _write(audio, meta):
    os.makedirs(FOLDER, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(meta.get("when") or time.time()))
    name = "%s-%s" % (stamp, (meta.get("room") or "room").replace("/", "_")[:40])
    peak = float(np.abs(audio).max()) or 1.0
    pcm = np.ascontiguousarray((audio * min(1.0, 0.98 / peak) * 32767).round().astype("<i2"))
    tmp = os.path.join(FOLDER, name + ".part.flac")
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "s16le", "-ar", str(RATE), "-ac", "3",
                    "-i", "pipe:0", "-c:a", "flac", tmp], input=pcm.tobytes(), check=True, timeout=300)
    os.replace(tmp, os.path.join(FOLDER, name + ".flac"))
    meta = dict(meta, scale=round(min(1.0, 0.98 / peak), 4))
    with open(os.path.join(FOLDER, name + ".json.tmp"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    os.replace(os.path.join(FOLDER, name + ".json.tmp"), os.path.join(FOLDER, name + ".json"))
    logger.info("Mix recorded for review: %s -> %s (%s)", (meta.get("out") or {}).get("title"),
                (meta.get("inn") or {}).get("title"), meta.get("label"))
    _prune()


def _prune():
    try:
        names = sorted(n[:-5] for n in os.listdir(FOLDER) if n.endswith(".json"))
    except OSError:
        return
    for name in names[:-KEEP]:
        for ext in (".flac", ".json"):
            try:
                os.remove(os.path.join(FOLDER, name + ext))
            except OSError:
                pass
