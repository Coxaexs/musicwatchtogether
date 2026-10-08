"""Stem separation for the DJ booth (drums / bass / vocals / other).

The separation itself runs in stems_worker.py, a long-lived subprocess in its
own venv with torch and Demucs, so the bot never loads torch. This module is
the bot-side client: it hashes a decoded track, serves the result from the
disk cache when it has one, and otherwise queues the track for the worker.

Results are int16 arrays of shape (3, n, 2): drums, bass, vocals at 48 kHz,
aligned with the deck's audio. "Other" is derived by the engine as the mix
minus those three.

Set DJ_STEMS=0 to turn it off. Without the worker venv it is off too, and the
booth simply works without stems.

Beyond the DJ booth, the cache also serves:
  * karaoke: instrumental() writes "<hash>.inst.flac" (the mix minus vocals),
    which the players stream instead of the phase-cancel filter;
  * an index (index.json) from track keys (mixer.track_key of the page URL /
    query) to content hashes, so a song can be found before it is decoded;
  * pins: songs the cache never evicts (the room's most played songs and
    songs in playlists someone prepared). Only unpinned songs count against
    DJ_STEMS_CACHE_GB.
"""

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import threading
import time

import numpy as np

logger = logging.getLogger("MusicBot.Stems")

HERE = os.path.dirname(os.path.abspath(__file__))
NAMES = ("drums", "bass", "vocals")          # stored, in this order
CONTROLS = ("drums", "bass", "vocals", "other")
PYTHON = os.environ.get("DJ_STEMS_PYTHON") or os.path.join(HERE, "stems-env", "bin", "python")
WORKER = os.path.join(HERE, "stems_worker.py")
CACHE_DIR = os.environ.get("DJ_STEMS_CACHE") or os.path.join(HERE, "stems_cache")
CACHE_BYTES = int(float(os.environ.get("DJ_STEMS_CACHE_GB") or 15) * 1024 ** 3)
IDLE_SECONDS = 15 * 60                       # free the GPU after this long unused
JOB_TIMEOUT = 15 * 60


def enabled():
    return os.environ.get("DJ_STEMS", "1") != "0" and os.path.exists(PYTHON)


_gpu_checked = (0.0, False)


def gpu_ok():
    """Whether the GPU answers (checked at most once a minute). Without it a
    song takes minutes on the CPU, so only on-demand work should run."""
    global _gpu_checked
    at, ok = _gpu_checked
    if time.monotonic() - at > 60:
        try:
            out = subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True, timeout=10)
            ok = out.returncode == 0 and "GPU" in out.stdout
        except Exception:
            ok = False
        _gpu_checked = (time.monotonic(), ok)
    return ok


_temp_checked = (0.0, None)
GPU_HOT = 78


def gpu_temperature():
    """GPU temperature in °C (checked at most every 30 s), or None."""
    global _temp_checked
    at, temp = _temp_checked
    if time.monotonic() - at > 30:
        try:
            out = subprocess.run(["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader"],
                                 capture_output=True, text=True, timeout=10).stdout
            temp = int(out.split()[0])
        except Exception:
            temp = None
        _temp_checked = (time.monotonic(), temp)
    return temp


#: () -> bool, set by dj.py: True while a DJ set is on air. Background work
#: (playlist prep, top songs) waits then, so the live mix keeps the CPU.
pause_background = lambda: False  # noqa: E731


def background_should_wait():
    if pause_background():
        return True
    temp = gpu_temperature()
    return temp is not None and temp >= GPU_HOT


def track_hash(audio):
    return hashlib.blake2b(np.ascontiguousarray(audio).data, digest_size=16).hexdigest()


def stems_path(digest):
    return os.path.join(CACHE_DIR, digest + ".npy")


def instrumental_path(digest):
    return os.path.join(CACHE_DIR, digest + ".inst.flac")


def _load(path):
    stems = np.load(path)
    os.utime(path)                           # LRU: recently used survives pruning
    return stems


# --------------------------------------------------------------------------
# Index (track key -> hash) and pins
# --------------------------------------------------------------------------

_index_lock = threading.Lock()


def _index_file():
    return os.path.join(CACHE_DIR, "index.json")


def load_index():
    try:
        with open(_index_file(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    data.setdefault("tracks", {})            # track key -> {"hash", "title"}
    data.setdefault("pins", {})              # hash -> [reason, ...]
    return data


def _save_index(data):
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = _index_file() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, _index_file())


def remember(keys, digest, title=None):
    """Record that these track keys decode to the audio with this hash."""
    keys = [k for k in keys or () if k]
    if not keys:
        return
    with _index_lock:
        data = load_index()
        for key in keys:
            data["tracks"][key] = {"hash": digest, "title": title}
        _save_index(data)


def lookup(keys):
    """Hash of a song whose stems are on disk, from any of its track keys."""
    tracks = load_index()["tracks"]
    for key in keys or ():
        entry = tracks.get(key) if key else None
        if entry and os.path.exists(stems_path(entry["hash"])):
            return entry["hash"]
    return None


def instrumental_for(keys):
    """Path of a ready karaoke instrumental for this song, or None."""
    digest = lookup(keys)
    path = digest and instrumental_path(digest)
    return path if path and os.path.exists(path) else None


def set_pins(reason, digests):
    """Make `reason` pin exactly these hashes (and nothing else)."""
    digests = set(digests)
    with _index_lock:
        data = load_index()
        pins = data["pins"]
        for digest in list(pins):
            if reason in pins[digest] and digest not in digests:
                pins[digest].remove(reason)
                if not pins[digest]:
                    del pins[digest]
        for digest in digests:
            if reason not in pins.setdefault(digest, []):
                pins[digest].append(reason)
        _save_index(data)


def pin(digest, reason):
    with _index_lock:
        data = load_index()
        if reason not in data["pins"].setdefault(digest, []):
            data["pins"][digest].append(reason)
            _save_index(data)


def pinned():
    return set(load_index()["pins"])


_library_cache = (None, [])


def library():
    """Every song with stems on disk, most recently used first.

    One entry per separated song: {"hash", "title", "youtube" (video id or
    None), "keys", "seconds", "karaoke" (instrumental ready), "pinned",
    "used" (epoch of last use)}. Songs whose stems file was pruned are left
    out. Reading this never touches the files, so browsing the list doesn't
    change what the cache keeps.
    """
    global _library_cache
    try:
        stamp = (os.path.getmtime(_index_file()), os.path.getmtime(CACHE_DIR))
    except OSError:
        return []
    if _library_cache[0] == stamp:
        return [dict(item) for item in _library_cache[1]]
    data = load_index()
    songs = {}
    for key, info in data["tracks"].items():
        digest = (info or {}).get("hash")
        if not digest:
            continue
        song = songs.setdefault(digest, {"hash": digest, "title": None, "youtube": None, "keys": []})
        song["keys"].append(key)
        song["title"] = song["title"] or (info or {}).get("title")
        if key.startswith("yt:") and not song["youtube"]:
            song["youtube"] = key[3:]
    out = []
    for digest, song in songs.items():
        path = stems_path(digest)
        try:
            used = os.path.getmtime(path)
            frames = np.load(path, mmap_mode="r").shape[1]
        except (OSError, ValueError, IndexError):
            continue
        song.update(
            title=song["title"] or "Unknown",
            seconds=int(frames / SR),
            karaoke=os.path.exists(instrumental_path(digest)),
            pinned=digest in data["pins"],
            used=used,
        )
        out.append(song)
    out.sort(key=lambda song: song["used"], reverse=True)
    _library_cache = (stamp, out)
    return [dict(item) for item in out]


def forget(keys):
    """Drop a song's stems and instrumental (a "recompute" starts from scratch)."""
    digest = lookup(keys)
    if not digest:
        return
    for path in (stems_path(digest), instrumental_path(digest)):
        try:
            os.remove(path)
        except OSError:
            pass


def _prune():
    """Evict least recently used, unpinned songs beyond the size budget."""
    keep = pinned()
    sizes, used = {}, {}
    try:
        names = os.listdir(CACHE_DIR)
    except OSError:
        return
    for name in names:
        if not (name.endswith(".npy") or name.endswith(".inst.flac")):
            continue
        digest = name.split(".", 1)[0]
        if digest in keep:
            continue
        try:
            st = os.stat(os.path.join(CACHE_DIR, name))
        except OSError:
            continue
        sizes[digest] = sizes.get(digest, 0) + st.st_size
        used[digest] = max(used.get(digest, 0), st.st_mtime)
    total = 0
    for digest in sorted(sizes, key=used.get, reverse=True):
        total += sizes[digest]
        if total > CACHE_BYTES:
            for path in (stems_path(digest), instrumental_path(digest)):
                try:
                    os.remove(path)
                except OSError:
                    pass


# --------------------------------------------------------------------------
# Decoding and karaoke instrumentals
# --------------------------------------------------------------------------

SR = 48_000
MAX_SECONDS = 15 * 60
KARAOKE_GUIDE = 0.0          # how much of the original vocal stays in karaoke


def decode(source):
    """Whole track as int16 (n, 2) at 48 kHz (same as the DJ deck's audio)."""
    command = ["ffmpeg", "-nostdin", "-loglevel", "error"]
    if str(source).startswith(("http://", "https://")):
        # A stalled stream fails after 20 s instead of hanging to the timeout.
        command += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "3",
                    "-rw_timeout", "20000000"]
    command += ["-i", str(source), "-vn", "-t", str(MAX_SECONDS),
                "-f", "s16le", "-ac", "2", "-ar", str(SR), "pipe:1"]
    result = subprocess.run(command, capture_output=True, timeout=300)
    audio = np.frombuffer(result.stdout, dtype=np.int16)
    audio = audio[: len(audio) // 2 * 2].reshape(-1, 2)
    if len(audio) < SR * 3:
        detail = result.stderr.decode(errors="replace").strip()[-200:]
        raise RuntimeError(detail or "The track has no audio.")
    return audio


def local_audio(keys):
    """(audio, stems) of a song rebuilt entirely from the cache, or None.

    The karaoke instrumental is the mix minus the vocal stem, so adding the
    vocal back gives the original track: a prepared song plays without
    fetching it from YouTube again (stream links expire and get throttled).
    """
    digest = lookup(keys)
    if not digest or not os.path.exists(instrumental_path(digest)):
        return None
    try:
        stems = _load(stems_path(digest))
        inst = decode(instrumental_path(digest))
    except Exception as error:
        logger.warning("Could not rebuild %s from the cache: %s", digest, error)
        return None
    n = stems.shape[1]
    if len(inst) < n:
        inst = np.pad(inst, ((0, n - len(inst)), (0, 0)))
    vocals = stems[NAMES.index("vocals")].astype(np.int32) * (1 - KARAOKE_GUIDE)
    audio = np.clip(inst[:n].astype(np.int32) + vocals, -32768, 32767).astype(np.int16)
    return audio, stems


def write_instrumental(audio, stems, digest):
    """The mix minus its vocals, as FLAC next to the stems."""
    vocals = stems[NAMES.index("vocals")].astype(np.float32)
    mix = audio.astype(np.float32) - vocals * (1 - KARAOKE_GUIDE)
    pcm = np.clip(mix, -32768, 32767).astype("<i2").tobytes()
    path = instrumental_path(digest)
    tmp = path + ".part.flac"
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "s16le",
                    "-ar", str(SR), "-ac", "2", "-i", "pipe:0", "-c:a", "flac", tmp],
                   input=pcm, check=True, timeout=300)
    os.replace(tmp, path)
    return path


async def prepare(source, keys, title=None, instrumental=False, pin_reason=None,
                  background=False):
    """Make sure a song's stems (and optionally its karaoke instrumental) exist.

    `source` is a stream URL or file path. Returns the song's hash, or None
    when stems are off. Cheap when everything is cached already.
    """
    if not enabled():
        return None
    loop = asyncio.get_running_loop()
    digest = lookup(keys)
    if digest and (not instrumental or os.path.exists(instrumental_path(digest))):
        if pin_reason:
            pin(digest, pin_reason)
        return digest
    audio = await loop.run_in_executor(None, decode, source)
    result = await SEPARATOR.separate(audio, background=background)
    if result is None:
        return None
    digest = await loop.run_in_executor(None, track_hash, audio)
    remember(keys, digest, title)
    if instrumental and not os.path.exists(instrumental_path(digest)):
        await loop.run_in_executor(None, write_instrumental, audio, result, digest)
    if pin_reason:
        pin(digest, pin_reason)
    return digest


class Separator:
    def __init__(self):
        self.proc = None
        self.lock = asyncio.Lock()
        self.next_id = 0
        self.last_used = 0.0
        self.idle_task = None
        self.device = None
        self.interactive_waiting = 0     # background work yields to these

    async def _start(self):
        os.makedirs(os.path.join(CACHE_DIR, "models"), exist_ok=True)
        env = dict(os.environ, TORCH_HOME=os.path.join(CACHE_DIR, "models"))
        log = open(os.path.join(CACHE_DIR, "worker.log"), "ab")
        self.proc = await asyncio.create_subprocess_exec(
            PYTHON, WORKER,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=log,
            env=env, limit=1 << 20)
        log.close()
        line = await asyncio.wait_for(self.proc.stdout.readline(), timeout=600)
        try:
            hello = json.loads(line or b"{}")
        except ValueError:
            hello = {}
        if not hello.get("ready"):
            await self._kill()
            raise RuntimeError("The stem separator did not start (see stems_cache/worker.log).")
        self.device = hello.get("device")
        logger.info("Stem separator up (%s on %s)", hello.get("model"), self.device)
        if not self.idle_task:
            self.idle_task = asyncio.create_task(self._idle_loop())

    async def _kill(self):
        proc, self.proc = self.proc, None
        if proc and proc.returncode is None:
            proc.kill()
            await proc.wait()

    async def _idle_loop(self):
        try:
            while True:
                await asyncio.sleep(60)
                if self.proc and not self.lock.locked() and time.monotonic() - self.last_used > IDLE_SECONDS:
                    async with self.lock:
                        logger.info("Stem separator idle, stopping it")
                        await self._kill()
        finally:
            self.idle_task = None

    async def separate(self, audio, wanted=lambda: True, background=False):
        """Stems for a decoded track, or None when off/unwanted. Raises on failure.

        Background work (playlist prep, warming the most played songs) waits
        until no one is waiting for a song they are about to hear.
        """
        if not enabled():
            return None
        loop = asyncio.get_running_loop()
        key = await loop.run_in_executor(None, track_hash, audio)
        path = os.path.join(CACHE_DIR, key + ".npy")
        if os.path.exists(path):
            return await loop.run_in_executor(None, _load, path)
        if background:
            # Yield to listeners, to a live DJ set, and to a hot GPU (whose
            # jobs would otherwise fall back to the CPU).
            while self.interactive_waiting or self.lock.locked() or background_should_wait():
                await asyncio.sleep(5 if background_should_wait() else 1)
        else:
            self.interactive_waiting += 1
        try:
            await self.lock.acquire()
        finally:
            if not background:
                self.interactive_waiting -= 1
        try:
            # The deck may have moved on while this waited behind another song.
            if not wanted():
                return None
            if os.path.exists(path):
                return await loop.run_in_executor(None, _load, path)
            os.makedirs(CACHE_DIR, exist_ok=True)
            raw = os.path.join(CACHE_DIR, key + ".raw")
            await loop.run_in_executor(None, np.ascontiguousarray(audio, dtype="<i2").tofile, raw)
            try:
                if not self.proc or self.proc.returncode is not None:
                    await self._start()
                self.next_id += 1
                job = {"id": self.next_id, "input": raw, "output": path}
                self.proc.stdin.write((json.dumps(job) + "\n").encode())
                await self.proc.stdin.drain()
                line = await asyncio.wait_for(self.proc.stdout.readline(), timeout=JOB_TIMEOUT)
                if not line:
                    await self._kill()
                    raise RuntimeError("The stem separator crashed (see stems_cache/worker.log).")
                reply = json.loads(line)
                if not reply.get("ok"):
                    raise RuntimeError(reply.get("error") or "Stem separation failed.")
                logger.info("Separated %.0f s of audio in %.1f s", len(audio) / 48000, reply.get("seconds", 0))
            except (asyncio.TimeoutError, asyncio.CancelledError):
                await self._kill()           # a half-read reply would desync the pipe
                raise
            finally:
                self.last_used = time.monotonic()
                try:
                    os.remove(raw)
                except OSError:
                    pass
        finally:
            self.lock.release()
        await loop.run_in_executor(None, _prune)
        return await loop.run_in_executor(None, _load, path)


SEPARATOR = Separator()
