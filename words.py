"""Word-timed lyrics: when each word of a song is sung, and each letter in it.

Lyrics from lrclib or Musixmatch are timed per line, and often for another
version of the song. Oracle times every word on the song's own vocal stem
(huddle-ops words-sync.py feeds oracle:~/hoffle-words, hq_words.py there does
the forced alignment), so the times fit the very audio a room or the DJ booth
plays. Two files sit beside a song's stems:

  <hash>.lyrics.json  what the bot wants timed: {"lines": [[at, text], ...],
                      "fp", "source", "title", "asked"}. Written whenever lyrics
                      are shown for a song that has stems; words-sync sends it.
  <hash>.words.json   Oracle's answer: {"fp", "mode", "fit", "lines": [{"at",
                      "end", "text", "words": [[start, end, word, [ms, ...], score]]}]}
                      (see hq_words.py; ms = when each letter ends, from the word's start).

Words are only used for the lyrics they were timed for (the fingerprint of
the line texts), so lyrics someone picks by hand never get another version's
words. They also drive the DJ booth's FOLLOW LYRICS setting (realdj): mixes and
loops that end where a line ends rather than mid-sentence. Songs nobody has
lyrics for are heard by Whisper on Oracle ("mode": "heard", no fingerprint):
its words are too often misheard to show, so those only give the DJ its timing.
"""

import hashlib
import json
import logging
import os
import re
import time
import unicodedata

import stems

logger = logging.getLogger("MusicBot.Words")

#: An aligned song is trusted when this share of its lines start within 1.5 s of where the
#: lyrics put them (after their overall offset): fewer means another song's or version's words.
MIN_FIT = 0.5
#: A line whose words the aligner was this unsure of (mean letter probability over the line) keeps its
#: line timing only. Sung words score low in general (median 0.26 over the first 37 songs) and a low score
#: is not a wrong time (a line at 0.11 sat within 1.2 s of lrclib), so only near-zero lines are dropped.
MIN_LINE_SCORE = 0.01
_cache = {}


def words_path(digest):
    return os.path.join(stems.CACHE_DIR, digest + ".words.json")


def request_path(digest):
    return os.path.join(stems.CACHE_DIR, digest + ".lyrics.json")


def _norm(text):
    text = unicodedata.normalize("NFKC", str(text)).casefold()
    return re.sub(r"[\W_]+", " ", text).strip()


def fingerprint(lines):
    """Lines [[at, text], ...] -> a short id of their words (not their times)."""
    joined = "\n".join(_norm(line[1]) for line in lines or () if line and len(line) > 1 and str(line[1]).strip())
    return hashlib.blake2b(joined.encode(), digest_size=8).hexdigest() if joined else None


def _read(path):
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    hit = _cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = None
    if len(_cache) > 128:
        _cache.clear()
    _cache[path] = (mtime, data)
    return data


def load(digest):
    """The timed words for a song, if Oracle made them and they are trustworthy, else None."""
    if not digest:
        return None
    data = _read(words_path(digest))
    if not isinstance(data, dict) or data.get("error") or not data.get("lines"):
        return None
    if data.get("mode") == "aligned" and data.get("fit") is not None and data["fit"] < MIN_FIT:
        return None
    return data


def want(digest, lines, source=None, title=None):
    """Ask for these lyrics to be timed on this song (words-sync collects it). Cheap when nothing changed."""
    fp = fingerprint(lines)
    if not digest or not fp:
        return False
    done = _read(words_path(digest))
    if isinstance(done, dict) and done.get("fp") == fp:
        return False                                  # timed already (or tried: an error stays put)
    asked = _read(request_path(digest))
    if isinstance(asked, dict) and asked.get("fp") == fp:
        return False
    data = {"lines": [[float(at) if at is not None else None, str(text)] for at, text in lines if str(text).strip()],
            "fp": fp, "source": source, "title": title, "asked": int(time.time())}
    path = request_path(digest)
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.chmod(tmp, 0o664)
        os.replace(tmp, path)
    except OSError as error:
        logger.debug("Could not ask for words of %s: %s", digest, error)
        return False
    return True


def for_lines(digest, lines):
    """Per shown line, its timed words [[start, end, word, [ms, ...]], ...] (or [] for a line without),
    when this song's words were timed for exactly these lyrics; else None."""
    data = load(digest)
    if not data or data.get("fp") != fingerprint(lines):
        return None
    timed = data["lines"]
    shown = [line for line in lines if str(line[1]).strip()]
    if len(timed) != len(shown):
        return None
    by_line, out = iter(timed), []
    for line in lines:
        if not str(line[1]).strip():
            out.append([])
            continue
        t = next(by_line)
        ws = t.get("words") or []
        scores = [w[4] for w in ws if len(w) > 4]
        lost = bool(scores) and sum(scores) / len(scores) < MIN_LINE_SCORE
        # A line where the aligner lost its way keeps its line timing only.
        out.append([] if lost or not ws else [[w[0], w[1], w[2], w[3]] for w in ws])
    return out


def spans(digest):
    """For the DJ: (lines, words) with lines = [(start, end)] of each sung line and words =
    [(start, end)] of every word, sorted, in the song's seconds. None without words."""
    data = load(digest)
    if not data:
        return None
    lines, words = [], []
    for line in data["lines"]:
        timed = [w for w in line.get("words") or () if w[0] is not None and w[1] is not None]
        if not timed:
            continue
        lines.append((float(timed[0][0]), float(timed[-1][1])))
        words += [(float(w[0]), float(w[1])) for w in timed]
    if not lines:
        return None
    return sorted(lines), sorted(words)
