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

import difflib
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
#: ... unless Whisper confirmed the words (share of heard words in the lyrics, hq_words.PLAIN_MATCH): then
#: the lyrics' own line times were another version's, and ours are the ones to trust.
MIN_MATCH = 0.6
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
    return _trusted(_read(words_path(digest)))


def _trusted(data):
    if not isinstance(data, dict) or data.get("error") or not data.get("lines"):
        return None
    if data.get("mode") == "aligned" and data.get("fit") is not None and data["fit"] < MIN_FIT \
            and (data.get("match") or 0) < MIN_MATCH:
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


#: Lyrics shown that differ a little from the ones that were timed (another lrclib upload: a line split in
#: two, "I push it down" for "push it down", ad libs in brackets): words are matched one by one. A shown line
#: gets its words when this share of them matched a timed word; the song needs MAP_SONG of all of them.
MAP_LINE = 0.7
MAP_SONG = 0.6


def _lost(line):
    scores = [w[4] for w in line.get("words") or () if len(w) > 4]
    return bool(scores) and sum(scores) / len(scores) < MIN_LINE_SCORE


def _key(word):
    return _norm(word).replace(" ", "")


def _same_lines(timed, lines):
    by_line, out = iter(timed), []
    for line in lines:
        if not str(line[1]).strip():
            out.append([])
            continue
        t = next(by_line)
        ws = t.get("words") or []
        # A line where the aligner lost its way keeps its line timing only.
        out.append([] if _lost(t) or not ws else [[w[0], w[1], w[2], w[3]] for w in ws])
    return out


def _mapped(timed, lines):
    """The timed words put on the shown lyrics word by word (difflib over the normalised words)."""
    pool = [w for t in timed if not _lost(t) for w in t.get("words") or () if w[0] is not None and w[1] is not None]
    shown = [(li, word) for li, line in enumerate(lines) for word in str(line[1]).split()]
    if not pool or not shown:
        return None
    match = difflib.SequenceMatcher(None, [_key(w[2]) for w in pool], [_key(w) for _, w in shown], autojunk=False)
    hit = {}
    for a, b, size in match.get_matching_blocks():
        for k in range(size):
            hit[b + k] = pool[a + k]
    if len(hit) < MAP_SONG * len(shown):
        return None
    out, k = [[] for _ in lines], 0
    for li, line in enumerate(lines):
        n = len(str(line[1]).split())
        idx = list(range(k, k + n))
        k += n
        if not n or sum(1 for i in idx if i in hit) < MAP_LINE * n:
            continue
        row = []
        for j, i in enumerate(idx):
            word = shown[i][1]
            if i in hit:
                w = hit[i]
                row.append([w[0], w[1], word, list(w[3])])
                continue
            # A word only the shown lyrics have: the gap between its timed neighbours in the line.
            before = next((hit[idx[m]][1] for m in range(j - 1, -1, -1) if idx[m] in hit), None)
            after = next((hit[idx[m]][0] for m in range(j + 1, n) if idx[m] in hit), None)
            start = before if before is not None else max(0.0, after - 0.3)
            end = after if after is not None else start + 0.3
            row.append([start, max(start, end), word, []])
        # Never backwards: a repeated chorus word matched out of order keeps the line moving forward.
        for j in range(1, len(row)):
            if row[j][0] < row[j - 1][0]:
                row[j][0] = row[j - 1][0]
                row[j][1] = max(row[j][1], row[j][0])
                row[j][3] = []
        out[li] = row
    return out if any(out) else None


def for_lines(digest, lines):
    """Per shown line, its timed words [[start, end, word, [ms, ...]], ...] (or [] for a line without),
    or None when this song has no timed words that fit these lyrics. Lyrics timed exactly as shown are used
    line for line; slightly different ones (another upload of the same lyrics) word by word (_mapped)."""
    return _fit_to(load(digest), lines)


def _fit_to(data, lines):
    if not data:
        return None
    timed = data["lines"]
    shown = [line for line in lines if str(line[1]).strip()]
    if data.get("fp") == fingerprint(lines) and len(timed) == len(shown):
        return _same_lines(timed, lines)
    return _mapped(timed, lines)


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


# ---------------------------------------------------------------- twins: other uploads of a timed song
#
# The same song is often in the library several times, from different YouTube uploads (an official video
# with an intro, an audio upload, a lyric video). Only uploads with stems get timed on Oracle. For an upload
# without stems, words-sync finds a timed upload whose words fit the same lyrics, lines the two recordings up
# (cross-correlation; both halves of the song must agree on the shift, so a sped-up or live version is not
# taken), and writes the timed words shifted onto this upload:
#
#   twins/<YouTube id>.ask.json    the bot's request: {"keys", "title", "duration", "lines", "fp", "asked"}
#   twins/<YouTube id>.words.json  the shifted words (the format of <hash>.words.json, plus "twin_of", "shift")
#   twins/<YouTube id>.none.json   no timed upload of this recording (yet): {"at", "why"}

def twins_dir():
    return os.path.join(stems.CACHE_DIR, "twins")


def _video(keys):
    for key in keys or ():
        if key and key.startswith("yt:") and re.fullmatch(r"[0-9A-Za-z_-]{11}", key[3:]):
            return key[3:]
    return None


def twin_lines(keys, lines):
    """for_lines() for an upload without stems, from its twin's shifted words; None without."""
    video = _video(keys)
    if not video:
        return None
    return _fit_to(_trusted(_read(os.path.join(twins_dir(), video + ".words.json"))), lines)


def want_twin(keys, lines, title=None, duration=None):
    """Ask words-sync to look for a timed twin of this upload. Cheap when nothing changed."""
    video, fp = _video(keys), fingerprint(lines)
    if not video or not fp:
        return False
    base = os.path.join(twins_dir(), video)
    if os.path.exists(base + ".words.json"):
        return False
    asked = _read(base + ".ask.json")
    if isinstance(asked, dict) and asked.get("fp") == fp:
        return False
    data = {"keys": list(keys), "title": title, "duration": duration, "fp": fp, "asked": int(time.time()),
            "lines": [[float(at) if at is not None else None, str(text)] for at, text in lines if str(text).strip()]}
    try:
        if not os.path.isdir(twins_dir()):
            os.makedirs(twins_dir(), exist_ok=True)
            os.chmod(twins_dir(), 0o2775)      # words-sync (another user, group server) writes the answers here
        tmp = base + ".ask.json.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.chmod(tmp, 0o664)
        os.replace(tmp, base + ".ask.json")
    except OSError as error:
        logger.debug("Could not ask for a twin of %s: %s", video, error)
        return False
    return True


def available(urls):
    """Which of these track URLs have word-timed lyrics to show: their own (aligned, not just heard by
    Whisper) or a twin's. For Hoffle's music search, which marks them. One read of the stems index."""
    tracks = stems.load_index()["tracks"]
    out = set()
    for url in urls:
        found = re.search(r"(?:v=|youtu\.be/|shorts/)([A-Za-z0-9_-]{11})", str(url or ""))
        if not found:
            continue
        video = found.group(1)
        entry = tracks.get("yt:" + video) or {}
        data = load(entry.get("hash")) if entry.get("hash") else None
        if not data:
            data = _trusted(_read(os.path.join(twins_dir(), video + ".words.json")))
        if data and data.get("mode") == "aligned":
            out.add(url)
    return out
