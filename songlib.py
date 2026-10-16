"""Song library tools for the dashboard: find a downloaded song and delete it completely.

A song can go bad in three places, and a corrupted download makes all of them wrong at once:

  * the downloaded audio file (musics/Artist/Title - [videoID].mp3),
  * its stems and karaoke instrumental (stems_cache/, found through the stems index),
  * its tempo / key / energy analysis (mix_analysis.json).

delete() removes all three, so the next time anybody plays the song the bot downloads it again, analyses it again
and splits it into stems again, as if it had never been played.
"""

import logging
import os
import re

import djbrain
import mixer
import stems

logger = logging.getLogger("MusicBot.SongLib")

ID_RE = re.compile(r"^[0-9A-Za-z_-]{11}$")
_FILE_ID = re.compile(r"\[([0-9A-Za-z_-]{11})\]\.[A-Za-z0-9]+$")
_OLD_FILE_ID = re.compile(r"^ytdl_([0-9A-Za-z_-]{11})\.")


def _music():
    import music           # imported late: music.py is large and imports a lot
    return music


def video_id(path):
    """The YouTube id in a library file's name, or None."""
    name = os.path.basename(path)
    match = _FILE_ID.search(name) or _OLD_FILE_ID.match(name)
    return match.group(1) if match else None


def _keys(vid, path=None):
    return [f"yt:{vid}"] + ([path] if path else [])


def search(query, limit=40):
    """Downloaded songs whose name contains the text, newest first.

    [{id, title, artist, file, size, stems, analysed}]. Files without a video id in the name cannot be matched
    to their caches reliably, so they are listed with id None and cannot be deleted from here. `issues` are the
    problems Gemini heard in the audio (empty when none or not analysed): a likely corrupted download.
    """
    music = _music()
    needle = (query or "").strip().lower()
    rows = []
    for path in music.list_library_files():
        if needle and needle not in path.lower():
            continue
        try:
            stat = os.stat(path)
        except OSError:
            continue
        rows.append((stat.st_mtime, path, stat.st_size))
    rows.sort(reverse=True)
    rows.sort(key=lambda row: not djbrain.flags(_keys(video_id(row[1]), row[1]) if video_id(row[1]) else [row[1]]))
    out = []
    for _, path, size in rows[:limit]:
        vid = video_id(path)
        title, artist = music.parse_local_song_name(path)
        keys = _keys(vid, path) if vid else [path]
        out.append({"id": vid, "title": title, "artist": artist, "file": os.path.basename(path), "size": size,
                    "stems": bool(stems.lookup(keys)), "analysed": bool(mixer.cache.get(*keys)),
                    "issues": djbrain.flags(keys) if vid else []})
    return out


def delete(vid):
    """Remove a song's file, stems and analysis. Returns what was removed.

    Raises ValueError for something that is not a video id, LookupError when no such song is in the library.
    """
    if not ID_RE.fullmatch(vid or ""):
        raise ValueError("That is not a YouTube video id.")
    music = _music()
    files = [p for p in music.refresh_library_index() if video_id(p) == vid]
    if not files:
        raise LookupError("That song is not in the library.")
    removed = []
    for path in files:
        try:
            os.remove(path)
            removed.append(os.path.basename(path))
        except FileNotFoundError:
            pass
        except OSError as error:
            raise RuntimeError(f"Could not delete {os.path.basename(path)}: {error}") from error
    keys = _keys(vid) + list(files)
    had_stems = stems.forget_song(keys)
    had_analysis = bool(mixer.cache.get(*keys))
    mixer.cache.drop(*keys)
    music.refresh_library_index()
    logger.info("Deleted song %s: files=%s stems=%s analysis=%s", vid, removed, had_stems, had_analysis)
    return {"id": vid, "files": removed, "stems": had_stems, "analysis": had_analysis}
