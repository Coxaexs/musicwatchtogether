"""DJ booth: a stems-capable two-deck mixer served at ``/dj/``.

The booth itself is a browser app (``webstatic/dj/``) that mixes with the Web
Audio API. This module is its back office:

* a DJ library (``dj_cache/index.json``) of tracks and crates. Tracks come
  from the same resolver as ``/play`` (search, YouTube, Spotify playlists and
  albums via their YouTube matches, the local music library) or from audio
  files uploaded by the DJ;
* a job pipeline that fetches each track once, transcodes it to a
  browser-decodable file, analyses tempo/beat-grid/waveform and, when a source
  separation model is installed, splits it into stems (vocals, drums, bass,
  other, and guitar/piano with a 6-stem model);
* a WebSocket that takes the booth's master mix as PCM and publishes it into a
  Huddle voice room through ``huddle_voice``.

Stem separation uses Demucs. It is optional and heavy (PyTorch), so it can
live in its own virtualenv: point ``DJ_DEMUCS_PYTHON`` at that interpreter.
Without it the booth still works with full tracks, and DJs can upload stems
they already own (from stem packs, Beatport/Bandcamp purchases, etc.).
"""

import asyncio
from array import array
import hashlib
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from typing import Optional
from urllib.parse import unquote

from aiohttp import web

import huddle
from storage import load_json, save_json

logger = logging.getLogger('MusicBot.DJ')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, 'webstatic', 'dj')
DJ_DIR = os.path.abspath(os.getenv('DJ_CACHE_DIR') or os.path.join(BASE_DIR, 'dj_cache'))
TRACKS_DIR = os.path.join(DJ_DIR, 'tracks')
INDEX_FILE = os.path.join(DJ_DIR, 'index.json')

DEMUCS_PYTHON = os.getenv('DJ_DEMUCS_PYTHON') or sys.executable
DEMUCS_MODEL = os.getenv('DJ_STEMS_MODEL', 'htdemucs')
DEMUCS_DEVICE = os.getenv('DJ_STEMS_DEVICE', '')  # '' lets Demucs pick cuda/cpu
DEMUCS_JOBS = max(1, int(os.getenv('DJ_STEMS_JOBS', '0') or 0) or min(4, os.cpu_count() or 1))
FETCH_WORKERS = 2

MODEL_STEMS = {
    'htdemucs': ['vocals', 'drums', 'bass', 'other'],
    'htdemucs_ft': ['vocals', 'drums', 'bass', 'other'],
    'mdx_extra': ['vocals', 'drums', 'bass', 'other'],
    'htdemucs_6s': ['vocals', 'drums', 'bass', 'guitar', 'piano', 'other'],
}
STEM_NAMES = ('vocals', 'drums', 'bass', 'guitar', 'piano', 'other')
UPLOAD_EXTENSIONS = {'.mp3', '.wav', '.flac', '.ogg', '.oga', '.opus', '.m4a',
                     '.aac', '.aif', '.aiff', '.webm', '.alac'}
MAX_UPLOAD_BYTES = 400 * 1024 ** 2

ANALYSIS_RATE = 11025
PEAK_RATE = 50          # waveform bins per second
PEAK_SAMPLES = ANALYSIS_RATE // PEAK_RATE
BPM_MIN, BPM_MAX = 60.0, 200.0
DJ_BPM_LOW, DJ_BPM_HIGH = 78.0, 160.0  # fold half/double-time readings into this band

_TRACK_ID = re.compile(r'^[a-f0-9]{16}$')
_CAMELOT = re.compile(r'^(1[0-2]|[1-9])[AB]$')
_SPOTIFY_COLLECTION = re.compile(
    r'(open\.spotify\.com/(playlist|album)/|spotify:(playlist|album):)', re.I)


def _now_ms() -> int:
    return int(time.time() * 1000)


def track_id_for(kind: str, value: str) -> str:
    """Stable id, so importing the same song twice reuses its files."""
    key = f'{kind}:{value.strip().lower() if kind == "search" else value.strip()}'
    return hashlib.sha1(key.encode('utf-8')).hexdigest()[:16]


def track_dir(track_id: str) -> str:
    if not _TRACK_ID.match(track_id or ''):
        raise ValueError('bad track id')
    return os.path.join(TRACKS_DIR, track_id)


# ---------- audio analysis (pure Python; ffmpeg does the decoding) ----------

def _ffmpeg_pcm(args: list, timeout: float = 300) -> bytes:
    result = subprocess.run(
        ['ffmpeg', '-v', 'error', '-nostdin', *args],
        capture_output=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode('utf-8', 'replace')[-400:] or 'ffmpeg failed')
    return result.stdout


def probe_duration(path: str) -> Optional[float]:
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', path],
            capture_output=True, text=True, timeout=30)
        return float(result.stdout.strip())
    except Exception:
        return None


def _samples(pcm: bytes) -> array:
    samples = array('h')
    samples.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    if sys.byteorder != 'little':
        samples.byteswap()
    return samples


def onset_envelope(samples: array, hop: int = 128, win: int = 256) -> list:
    """Rectified rise of log energy: where drum hits and note attacks land."""
    import math
    energies = []
    for start in range(0, len(samples) - win, hop):
        chunk = samples[start:start + win]
        energies.append(math.sqrt(sum(s * s for s in chunk[::4]) / (win / 4)))
    onsets = [0.0]
    prev = math.log(energies[0] + 1) if energies else 0.0
    for energy in energies[1:]:
        cur = math.log(energy + 1)
        onsets.append(max(0.0, cur - prev))
        prev = cur
    return onsets


def estimate_tempo(samples: array, rate: int = ANALYSIS_RATE, hop: int = 128):
    """(bpm, confidence, first_beat_seconds) for mono PCM, or (None, 0, None).

    Onset autocorrelation finds the beat period; the beat phase is the offset
    inside that period where onsets recur most strongly. Half/double-time
    readings are folded into the usual DJ tempo band.
    """
    if len(samples) < rate * 8:
        return None, 0.0, None
    onsets = onset_envelope(samples, hop)
    mean = sum(onsets) / len(onsets)
    if mean < 0.02:
        return None, 0.0, None
    centred = [o - mean for o in onsets]
    fps = rate / hop
    min_lag = max(1, int(fps * 60.0 / BPM_MAX))
    max_lag = int(fps * 60.0 / BPM_MIN)
    count = len(centred) - max_lag - 1
    if count <= fps * 4:
        return None, 0.0, None
    zero = sum(o * o for o in centred[:count]) / count
    scores = {}
    for lag in range(min_lag, max_lag + 2):
        scores[lag] = sum(centred[i] * centred[i + lag] for i in range(count)) / count
    best = max(range(min_lag, max_lag + 1), key=scores.get)
    if scores[best] <= 0 or zero <= 0:
        return None, 0.0, None
    refined = float(best)
    if min_lag < best:
        y0, y1, y2 = scores[best - 1], scores[best], scores[best + 1]
        denom = y0 - 2 * y1 + y2
        if abs(denom) > 1e-12:
            refined += 0.5 * (y0 - y2) / denom
    bpm = 60.0 * fps / refined
    while bpm < DJ_BPM_LOW:
        bpm *= 2
    while bpm > DJ_BPM_HIGH:
        bpm /= 2
    bpm, phase = refine_grid(onsets, fps, bpm)
    # Onset frames are indexed by where their window starts, so they report
    # an attack early; 1.25 hops is the measured lag on sharp kicks.
    phase = (phase + 1.25 * hop / rate) % (60.0 / bpm)
    return bpm, scores[best] / zero, phase


def _comb(onsets: list, fps: float, bpm: float):
    """Best (score, phase_frames) of a beat comb at this tempo."""
    period = 60.0 / bpm * fps
    best_score, best_phase = -1.0, 0.0
    n = len(onsets)
    steps = max(1, int(period * 2))
    for k in range(steps):
        phase = k / 2
        total, pos = 0.0, phase
        while pos < n - 1:
            i = int(pos)
            frac = pos - i
            total += onsets[i] * (1 - frac) + onsets[i + 1] * frac
            pos += period
        if total > best_score:
            best_score, best_phase = total, phase
    return best_score, best_phase


def refine_grid(onsets: list, fps: float, bpm: float):
    """Polish a tempo estimate so the beat grid stays locked over the whole
    analysed section (an autocorrelation lag is only accurate to a frame,
    which drifts by tens of milliseconds across a minute of music)."""
    best = (-1.0, bpm, 0.0)
    for span, step in ((0.015, 0.05), (0.0006, 0.005)):
        centre = best[1]
        candidate = centre * (1 - span)
        while candidate <= centre * (1 + span):
            score, phase = _comb(onsets, fps, candidate)
            if score > best[0]:
                best = (score, candidate, phase)
            candidate += step
    return best[1], best[2] / fps


def analyse_file(path: str) -> dict:
    """Tempo, beat grid, loudness and a 3-band waveform for the booth.

    Blocking (ffmpeg + Python math, a few seconds per song): run in a thread.
    """
    duration = probe_duration(path) or 0.0
    # Three bands decoded side by side: lows / mids / highs, as DJ software
    # colours its waveforms.
    pcm = _ffmpeg_pcm([
        '-i', path, '-filter_complex',
        '[0:a]aformat=channel_layouts=mono,aresample=%d,asplit=3[a][b][c];'
        '[a]lowpass=f=200[l];[b]highpass=f=200,lowpass=f=3500[m];'
        '[c]highpass=f=3500[h];[l][m][h]join=inputs=3:channel_layout=3.0[out]' % ANALYSIS_RATE,
        '-map', '[out]', '-f', 's16le', '-acodec', 'pcm_s16le', 'pipe:1',
    ])
    interleaved = _samples(pcm)
    bands = [interleaved[index::3] for index in range(3)]
    bins = len(bands[0]) // PEAK_SAMPLES
    peaks = bytearray(bins * 3)
    loudest = 1
    raw = []
    for band in bands:
        levels = []
        for i in range(bins):
            chunk = band[i * PEAK_SAMPLES:(i + 1) * PEAK_SAMPLES]
            levels.append(max(max(chunk), -min(chunk)))
        raw.append(levels)
        loudest = max(loudest, max(levels or [1]))
    for b, levels in enumerate(raw):
        for i, level in enumerate(levels):
            peaks[i * 3 + b] = min(255, int(255 * (level / loudest) ** 0.7))

    # Tempo from up to 90 seconds in the middle of the song (intros/outros
    # are often beatless). The grid is then anchored back to the first beat.
    full = _samples(_ffmpeg_pcm([
        '-i', path, '-ac', '1', '-ar', str(ANALYSIS_RATE), '-f', 's16le', 'pipe:1']))
    total = len(full) / ANALYSIS_RATE
    duration = duration or total
    window = min(90.0, total)
    start = max(0.0, (total - window) / 2)
    section = full[int(start * ANALYSIS_RATE):int((start + window) * ANALYSIS_RATE)]
    bpm, confidence, phase = estimate_tempo(section)
    beat_offset = None
    if bpm and phase is not None:
        period = 60.0 / bpm
        beat_offset = round((start + phase) % period, 4)

    # Audible start/end (trim silence) for Auto DJ mix points.
    combined = [max(raw[0][i], raw[1][i], raw[2][i]) for i in range(bins)]
    floor = (max(combined) if combined else 0) * 0.04
    first = next((i for i, v in enumerate(combined) if v > floor), 0)
    last = next((i for i in range(bins - 1, -1, -1) if combined[i] > floor), bins - 1)

    # Energy 0..1 from onset density across the analysed section.
    energy = None
    if section:
        onsets = onset_envelope(section)
        flux = sum(onsets) / max(1, len(onsets))
        energy = round(max(0.0, min(1.0, (flux - 0.02) / 0.06)), 3)

    return {
        'duration': round(duration, 3),
        'bpm': round(bpm, 2) if bpm and confidence >= 0.08 else None,
        'bpm_confidence': round(confidence, 3),
        'beat_offset': beat_offset if bpm and confidence >= 0.08 else None,
        'audible_start': round(first / PEAK_RATE, 2),
        'audible_end': round((last + 1) / PEAK_RATE, 2),
        'energy': energy,
        'peaks': bytes(peaks),
    }


# ---------- library ----------

class Library:
    """Tracks + crates, persisted as one JSON document."""

    def __init__(self, path: Optional[str] = None):
        self.path = path or INDEX_FILE
        data = load_json(self.path, {}, logger) or {}
        self.tracks: dict = data.get('tracks') or {}
        self.crates: list = data.get('crates') or []
        self._dirty = False

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        save_json(self.path, {'tracks': self.tracks, 'crates': self.crates}, logger)
        self._dirty = False

    def add_track(self, *, kind: str, value: str, title: str, artist=None,
                  thumbnail=None, duration=None, track_id: Optional[str] = None) -> dict:
        track_id = track_id or track_id_for(kind, value)
        existing = self.tracks.get(track_id)
        if existing:
            return existing
        track = {
            'id': track_id,
            'title': title or value,
            'artist': artist,
            'thumbnail': thumbnail,
            'duration': duration,
            'source': {'kind': kind, 'value': value},
            'added_at': _now_ms(),
            'audio': 'missing',
            'analysis': None,
            'key': None,
            'cues': [],
            'stems': {'status': 'none', 'names': [], 'progress': 0, 'error': None},
            'job': {'stage': 'idle', 'progress': 0, 'error': None},
        }
        self.tracks[track_id] = track
        return track

    def crate(self, name: str, source: Optional[str] = None) -> dict:
        for crate in self.crates:
            if crate['name'] == name or (source and crate.get('source') == source):
                return crate
        crate = {'id': hashlib.sha1(f'{name}{time.time()}'.encode()).hexdigest()[:10],
                 'name': name, 'source': source, 'track_ids': []}
        self.crates.append(crate)
        return crate

    def public_track(self, track: dict) -> dict:
        return {key: value for key, value in track.items() if key != 'source'} | {
            'source_kind': track['source']['kind'],
        }


# ---------- the service ----------

class DJService:
    def __init__(self, cog_getter=None, loop=None):
        self.cog_getter = cog_getter or (lambda: None)
        self.library = Library()
        self.fetch_queue: asyncio.Queue = asyncio.Queue()
        self.stems_queue: asyncio.Queue = asyncio.Queue()
        self.queued: set = set()
        self.tasks: list = []
        self._stems_available: Optional[bool] = None
        self._lock = asyncio.Lock()
        self.version = 0  # bumps on every library change for cheap polling

    # ----- lifecycle -----

    async def start(self):
        os.makedirs(TRACKS_DIR, exist_ok=True)
        for _ in range(FETCH_WORKERS):
            self.tasks.append(asyncio.create_task(self._fetch_worker()))
        self.tasks.append(asyncio.create_task(self._stems_worker()))
        # Resume work interrupted by a restart.
        for track in self.library.tracks.values():
            stage = track['job'].get('stage')
            if stage in ('queued', 'fetching', 'analysing'):
                await self.enqueue(track['id'], stems=track['stems']['status'] in ('queued', 'running'))
            elif track['stems']['status'] in ('queued', 'running'):
                track['stems']['status'] = 'queued'
                await self._enqueue_stems(track['id'])

    async def stop(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()

    def touch(self):
        self.version += 1
        self.library.save()

    # ----- capabilities -----

    async def stems_available(self) -> bool:
        if self._stems_available is None:
            try:
                process = await asyncio.create_subprocess_exec(
                    DEMUCS_PYTHON, '-c', 'import demucs.separate',
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                self._stems_available = await asyncio.wait_for(process.wait(), 60) == 0
            except Exception:
                self._stems_available = False
            logger.info('DJ stem separation %s (%s, model %s)',
                        'available' if self._stems_available else 'unavailable',
                        DEMUCS_PYTHON, DEMUCS_MODEL)
        return self._stems_available

    async def capabilities(self) -> dict:
        return {
            'stems': await self.stems_available(),
            'stems_model': DEMUCS_MODEL,
            'stem_names': MODEL_STEMS.get(DEMUCS_MODEL, MODEL_STEMS['htdemucs']),
            'upload_stem_names': list(STEM_NAMES),
            'live': _live_supported(),
            'spotify': True,
        }

    # ----- queueing -----

    async def enqueue(self, track_id: str, stems: bool = False):
        track = self.library.tracks.get(track_id)
        if not track:
            return
        want_stems = stems and track['stems']['status'] not in ('ready', 'running')
        if want_stems and await self.stems_available():
            track['stems'].update(status='queued', progress=0, error=None)
        if track['audio'] == 'ready' and track.get('analysis'):
            if want_stems and track['stems']['status'] == 'queued':
                await self._enqueue_stems(track_id)
            return
        if track_id in self.queued:
            return
        self.queued.add(track_id)
        track['job'].update(stage='queued', progress=0, error=None)
        await self.fetch_queue.put(track_id)

    async def _enqueue_stems(self, track_id: str):
        await self.stems_queue.put(track_id)

    # ----- workers -----

    async def _fetch_worker(self):
        while True:
            track_id = await self.fetch_queue.get()
            try:
                await self._prepare(track_id)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning('DJ prepare failed for %s: %s', track_id, error, exc_info=True)
                track = self.library.tracks.get(track_id)
                if track:
                    track['job'].update(stage='failed', error=str(error)[:300])
                    if track['stems']['status'] == 'queued':
                        track['stems'].update(status='none')
                    self.touch()
            finally:
                self.queued.discard(track_id)

    async def _prepare(self, track_id: str):
        track = self.library.tracks.get(track_id)
        if not track:
            return
        folder = track_dir(track_id)
        os.makedirs(folder, exist_ok=True)
        full = os.path.join(folder, 'full.m4a')
        if track['audio'] != 'ready' or not os.path.exists(full):
            track['job'].update(stage='fetching', progress=0)
            self.touch()
            source_path = await self._fetch_source(track, folder)
            await asyncio.to_thread(transcode, source_path, full)
            track['audio'] = 'ready'
        if not track.get('analysis'):
            track['job'].update(stage='analysing', progress=0)
            self.touch()
            result = await asyncio.to_thread(analyse_file, full)
            with open(os.path.join(folder, 'peaks.bin'), 'wb') as handle:
                handle.write(result.pop('peaks'))
            if result.get('duration'):
                track['duration'] = result['duration']
            track['analysis'] = result
        track['job'].update(stage='ready', progress=100, error=None)
        self.touch()
        if track['stems']['status'] == 'queued':
            await self._enqueue_stems(track_id)

    async def _fetch_source(self, track: dict, folder: str) -> str:
        kind, value = track['source']['kind'], track['source']['value']
        if kind in ('upload', 'local'):
            path = value if kind == 'local' else os.path.join(folder, value)
            if not os.path.isfile(path):
                raise FileNotFoundError('The source file is gone.')
            return path

        # Prefer a copy already in the bot's local music library.
        cog = self.cog_getter()
        if cog and kind == 'search' and hasattr(cog, '_local_song_for_query'):
            requester = SimpleNamespace(id=0, bot=True, display_name='DJ booth')
            try:
                song = await asyncio.to_thread(cog._local_song_for_query, value, requester)
                if song and os.path.isfile(str(song.url)):
                    return str(song.url)
            except Exception as error:
                logger.debug('Local library lookup failed: %s', error)

        query = value if kind == 'url' else f'ytsearch1:{value}'
        return await asyncio.to_thread(download_audio, query, folder, track)

    async def _stems_worker(self):
        while True:
            track_id = await self.stems_queue.get()
            track = self.library.tracks.get(track_id)
            if not track or track['stems']['status'] != 'queued':
                continue
            try:
                await self._separate(track)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning('Stem separation failed for %s: %s', track_id, error)
                track['stems'].update(status='failed', error=str(error)[:300])
                self.touch()

    async def _separate(self, track: dict):
        folder = track_dir(track['id'])
        full = os.path.join(folder, 'full.m4a')
        stems_dir = os.path.join(folder, 'stems')
        track['stems'].update(status='running', progress=0, error=None, model=DEMUCS_MODEL)
        self.touch()
        with tempfile.TemporaryDirectory(prefix='djstems-', dir=folder) as work:
            command = [DEMUCS_PYTHON, '-m', 'demucs.separate', '-n', DEMUCS_MODEL,
                       '-o', work, '--filename', '{stem}.{ext}', '-j', str(DEMUCS_JOBS)]
            if DEMUCS_DEVICE:
                command += ['-d', DEMUCS_DEVICE]
            # Demucs reads audio through ffmpeg too, but a wav input avoids
            # torchaudio backend differences across versions.
            source_wav = os.path.join(work, 'input.wav')
            await asyncio.to_thread(subprocess.run, [
                'ffmpeg', '-v', 'error', '-nostdin', '-y', '-i', full,
                '-ac', '2', '-ar', '44100', source_wav], check=True, timeout=300)
            command.append(source_wav)
            process = await asyncio.create_subprocess_exec(
                *command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            tail = b''
            last_save = 0.0
            while True:
                chunk = await process.stderr.read(4096)
                if not chunk:
                    break
                tail = (tail + chunk)[-4000:]
                found = re.findall(rb'(\d{1,3})%\|', chunk)
                if found:
                    track['stems']['progress'] = min(99, int(found[-1]))
                    self.version += 1
                    if time.monotonic() - last_save > 5:
                        last_save = time.monotonic()
                        self.library.save()
            code = await process.wait()
            if code != 0:
                message = tail.decode('utf-8', 'replace').strip().splitlines()
                raise RuntimeError(message[-1] if message else f'demucs exited with {code}')
            produced = os.path.join(work, DEMUCS_MODEL)
            names = []
            os.makedirs(stems_dir, exist_ok=True)
            for name in MODEL_STEMS.get(DEMUCS_MODEL, STEM_NAMES):
                wav = os.path.join(produced, f'{name}.wav')
                if os.path.exists(wav):
                    await asyncio.to_thread(transcode, wav, os.path.join(stems_dir, f'{name}.m4a'))
                    names.append(name)
            if len(names) < 2:
                raise RuntimeError('Demucs produced no stems.')
        track['stems'].update(status='ready', names=names, progress=100, source='demucs')
        self.touch()

    # ----- library operations used by the HTTP API -----

    async def add_query(self, query: str, stems: bool, prepare: bool = True) -> dict:
        query = query.strip()
        if not query:
            raise ValueError('Type a song, a link, or a Spotify playlist.')
        cog = self.cog_getter()
        added = []
        crate = None
        if _SPOTIFY_COLLECTION.search(query):
            if cog and hasattr(cog, '_blocking_spotify_collection'):
                payload = await asyncio.to_thread(cog._blocking_spotify_collection, query, 500)
            else:
                payload = await asyncio.to_thread(fetch_spotify_collection, query, 500)
            crate = self.library.crate(payload['name'], source=query)
            for item in payload['tracks']:
                duration_ms = item.get('duration_ms')
                artist = ', '.join(item.get('artists') or []) or None
                track = self.library.add_track(
                    kind='search', value=item['search_query'],
                    title=_title_only(item['title'], artist), artist=artist,
                    thumbnail=item.get('thumbnail'),
                    duration=duration_ms / 1000 if duration_ms else None)
                added.append(track)
        elif 'list=' in query and cog:
            requester = SimpleNamespace(id=0, bot=True, display_name='DJ booth')
            songs, _total = await cog.process_youtube_playlist_fast(query, requester)
            crate = self.library.crate('YouTube playlist', source=query)
            for song in songs:
                added.append(self.library.add_track(
                    kind='url', value=song.url, title=song.title, artist=song.artist,
                    thumbnail=song.thumbnail, duration=_seconds(song.duration)))
        elif re.match(r'^https?://', query):
            added.append(self.library.add_track(kind='url', value=query, title=query))
        else:
            added.append(self.library.add_track(kind='search', value=query, title=query))
        if crate is not None:
            for track in added:
                if track['id'] not in crate['track_ids']:
                    crate['track_ids'].append(track['id'])
        self.touch()
        if prepare:
            for track in added:
                await self.enqueue(track['id'], stems=stems)
            self.touch()
        return {'added': [t['id'] for t in added], 'crate': crate}

    def add_local(self, path: str) -> dict:
        title, artist = _name_from_path(path)
        return self.library.add_track(kind='local', value=path, title=title, artist=artist)


def fetch_spotify_collection(url: str, limit: int = 500) -> dict:
    """Spotify playlist/album metadata with the bot's Spotify app keys.

    Same shape as MusicCog._blocking_spotify_collection, so the booth (and
    dj_import.py) can import without the Discord bot running.
    """
    import spotipy
    from spotipy.oauth2 import SpotifyClientCredentials
    client_id = os.getenv('SPOTIFY_CLIENT_ID')
    secret = os.getenv('SPOTIFY_CLIENT_SECRET')
    if not (client_id and secret):
        raise RuntimeError('Spotify importing needs SPOTIFY_CLIENT_ID and '
                           'SPOTIFY_CLIENT_SECRET in .env.')
    match = re.search(r'(playlist|album)[/:]([A-Za-z0-9]+)', url)
    if not match:
        raise ValueError('Paste a Spotify playlist or album link.')
    kind, ident = match.group(1), match.group(2)
    sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
        client_id=client_id, client_secret=secret))
    if kind == 'playlist':
        collection = sp.playlist(ident)
        page = collection.get('tracks') or {}
        items = list(page.get('items') or [])
        while page.get('next') and len(items) < limit:
            page = sp.next(page)
            items.extend(page.get('items') or [])
        tracks = [item.get('track') for item in items if item.get('track')]
    else:
        collection = sp.album(ident)
        page = collection.get('tracks') or {}
        tracks = list(page.get('items') or [])
        while page.get('next') and len(tracks) < limit:
            page = sp.next(page)
            tracks.extend(page.get('items') or [])
    images = collection.get('images') or []
    cover = images[0].get('url') if images else None
    normalized = []
    for track in tracks[:limit]:
        title = str(track.get('name') or '').strip()
        if not title or track.get('type', 'track') != 'track':
            continue
        artists = []
        for artist in track.get('artists') or []:
            name = artist.get('name')
            if name and name not in artists:
                artists.append(name)
        album_images = (track.get('album') or {}).get('images') or []
        joined = ', '.join(artists)
        normalized.append({
            'title': f'{title} — {joined}' if joined else title,
            'search_query': f'{title} {joined}'.strip(),
            'artists': artists,
            'duration_ms': track.get('duration_ms'),
            'thumbnail': album_images[0].get('url') if album_images else cover,
        })
    return {'kind': kind, 'name': str(collection.get('name') or 'Spotify import').strip(),
            'cover': cover, 'total': len(normalized), 'tracks': normalized}


_STEM_WORDS = [
    ('vocals', ('acapella', 'a cappella', 'vocal', 'vox', 'voice', 'topline')),
    ('drums', ('drum', 'perc', 'kick', 'hats', 'hihat', 'snare')),
    ('bass', ('bass', 'sub', '808')),
    ('guitar', ('guitar', 'gtr')),
    ('piano', ('piano', 'keys', 'rhodes', 'organ')),
    ('other', ('other', 'melod', 'music', 'synth', 'lead', 'pad', 'fx', 'chord', 'harmony')),
]


def guess_stem_name(filename: str) -> Optional[str]:
    """Map a stem-pack file name ("Song (Drums).wav", "03 BASS.flac") to a stem."""
    name = re.sub(r'[_\-.()\[\]]+', ' ', os.path.splitext(os.path.basename(filename))[0].lower())
    if 'instrumental' in name:
        return None  # a full instrumental is not a single stem
    for stem, words in _STEM_WORDS:
        if any(re.search(r'(?<![a-z])' + re.escape(word), name) for word in words):
            return stem
    return None


def _title_only(title: str, artist: Optional[str]) -> str:
    suffix = f' — {artist}' if artist else ''
    return title[: -len(suffix)] if suffix and title.endswith(suffix) else title


def _seconds(value) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(sum(int(p) * 60 ** i for i, p in enumerate(reversed(str(value).split(':')))))
    except (TypeError, ValueError):
        return None


def _name_from_path(path: str):
    stem = os.path.splitext(os.path.basename(path))[0]
    if ' - ' in stem:
        artist, title = stem.split(' - ', 1)
        return title.strip(), artist.strip()
    return stem, None


def transcode(source: str, target: str):
    """Browser-decodable AAC, 44.1 kHz stereo, written atomically."""
    partial = target + '.part.m4a'
    subprocess.run([
        'ffmpeg', '-v', 'error', '-nostdin', '-y', '-i', source, '-vn',
        '-ac', '2', '-ar', '44100', '-c:a', 'aac', '-b:a', '256k',
        '-movflags', '+faststart', partial,
    ], check=True, timeout=900, capture_output=True)
    os.replace(partial, target)


def download_audio(query: str, folder: str, track: dict) -> str:
    """Fetch the best audio stream with yt-dlp (same source as /play)."""
    import yt_dlp
    options = {
        'format': 'bestaudio/best',
        'noplaylist': True,
        'quiet': True,
        'noprogress': True,
        'no_warnings': True,
        'default_search': 'ytsearch1',
        'outtmpl': os.path.join(folder, 'source.%(ext)s'),
        'overwrites': True,
    }
    cookies = os.getenv('YTDLP_COOKIES', '').strip()
    if cookies and os.path.isfile(cookies):
        options['cookiefile'] = cookies
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(query, download=True)
        if info and info.get('entries'):
            info = next((entry for entry in info['entries'] if entry), None)
        if not info:
            raise RuntimeError('No playable result was found.')
        path = info.get('requested_downloads', [{}])[0].get('filepath') or ydl.prepare_filename(info)
    if track['title'] == track['source']['value']:  # still the raw query/link
        track['title'] = info.get('track') or info.get('title') or track['title']
        track['artist'] = track['artist'] or info.get('artist') or info.get('uploader')
    track['thumbnail'] = track['thumbnail'] or info.get('thumbnail')
    track['page_url'] = info.get('webpage_url')
    if not os.path.isfile(path):
        raise RuntimeError('The download produced no audio file.')
    return path


def _library_files() -> list:
    """The bot's local music library. music.py runs as __main__, so read it
    from there instead of importing (and re-running) the bot module."""
    music = sys.modules.get('__main__')
    if not hasattr(music, 'list_library_files'):
        return []
    try:
        return list(music.list_library_files())
    except Exception as error:
        logger.debug('Library listing failed: %s', error)
        return []


# ---------- live broadcast into a Huddle room ----------

def _live_supported() -> bool:
    return 'huddle_voice' in sys.modules and huddle.enabled()


# ---------- HTTP ----------

class DJRoutes:
    def __init__(self, ui, service: DJService):
        self.ui = ui
        self.service = service

    def _track(self, request) -> dict:
        track = self.service.library.tracks.get(request.match_info['track_id'])
        if not track:
            raise web.HTTPNotFound(text='track not found')
        return track

    async def page(self, request):
        if request.path.endswith('/dj'):
            raise web.HTTPFound(request.path + '/')
        with open(os.path.join(STATIC_DIR, 'index.html'), encoding='utf-8') as handle:
            html = handle.read()
        return web.Response(text=html, content_type='text/html',
                            headers={'Cache-Control': 'no-store'})

    async def static(self, request):
        name = request.match_info['name']
        if not re.fullmatch(r'[a-z0-9_-]+\.(js|css)', name):
            raise web.HTTPNotFound()
        path = os.path.join(STATIC_DIR, name)
        if not os.path.isfile(path):
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={'Cache-Control': 'no-cache'})

    async def capabilities(self, request):
        caps = await self.service.capabilities()
        caps['room'] = self.ui._get_allowed_guild_id(request)
        return web.json_response(caps)

    async def library(self, request):
        since = request.query.get('since')
        lib = self.service.library
        if since and since == str(self.service.version):
            return web.json_response({'version': self.service.version, 'unchanged': True})
        return web.json_response({
            'version': self.service.version,
            'tracks': [lib.public_track(t) for t in lib.tracks.values()],
            'crates': lib.crates,
        })

    async def add(self, request):
        body = await _json(request)
        try:
            result = await self.service.add_query(str(body.get('query') or ''),
                                                  bool(body.get('stems', True)))
        except (ValueError, RuntimeError) as error:
            return web.json_response({'error': str(error)}, status=400)
        except Exception as error:
            logger.warning('DJ add failed: %s', error, exc_info=True)
            return web.json_response({'error': f'Could not add that: {error}'}, status=502)
        return web.json_response(result)

    async def local_library(self, request):
        query = request.query.get('q', '').strip().lower()
        files = _library_files()
        items = []
        for path in files:
            name = os.path.basename(path)
            if query and query not in name.lower():
                continue
            items.append({'path': path, 'name': name})
            if len(items) >= 200:
                break
        return web.json_response({'files': items})

    async def add_local(self, request):
        body = await _json(request)
        path = str(body.get('path') or '')
        if path not in set(_library_files()):
            return web.json_response({'error': 'That file is not in the music library.'}, status=400)
        track = self.service.add_local(path)
        self.service.touch()
        await self.service.enqueue(track['id'], stems=bool(body.get('stems', True)))
        self.service.touch()
        return web.json_response({'added': [track['id']]})

    async def upload(self, request):
        """Upload an audio file (or a stem for an existing track) as a stream."""
        stems = True
        target_track = request.query.get('track')
        stem_name = request.query.get('stem')
        if stem_name and stem_name != 'auto' and stem_name not in STEM_NAMES:
            return web.json_response({'error': 'Unknown stem name.'}, status=400)
        if target_track and not _TRACK_ID.match(target_track):
            return web.json_response({'error': 'bad track id'}, status=400)
        if not request.content_type.startswith('multipart/'):
            return web.json_response({'error': 'Send the files as multipart/form-data.'}, status=400)
        os.makedirs(DJ_DIR, exist_ok=True)
        reader = await request.multipart()
        added, detected, skipped = [], [], []
        while True:
            part = await reader.next()
            if part is None:
                break
            if part.name == 'stems':
                stems = (await part.text()).strip() not in ('0', 'false', '')
                continue
            if part.name != 'file' or not part.filename:
                continue
            extension = os.path.splitext(part.filename)[1].lower()
            if extension not in UPLOAD_EXTENSIONS:
                return web.json_response({'error': f'{extension or "That file"} is not an audio format the booth reads.'}, status=400)
            digest = hashlib.sha1()
            with tempfile.NamedTemporaryFile(dir=DJ_DIR, delete=False, suffix=extension) as handle:
                temp_path = handle.name
                size = 0
                while True:
                    chunk = await part.read_chunk(1 << 16)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        handle.close()
                        os.unlink(temp_path)
                        return web.json_response({'error': 'File is larger than 400 MB.'}, status=413)
                    digest.update(chunk)
                    handle.write(chunk)
            try:
                if target_track and stem_name:
                    track = self.service.library.tracks.get(target_track)
                    if not track:
                        raise web.HTTPNotFound(text='track not found')
                    name = stem_name
                    if name == 'auto':
                        name = guess_stem_name(unquote(part.filename))
                        if not name:
                            skipped.append(unquote(part.filename))
                            continue
                    stems_dir = os.path.join(track_dir(target_track), 'stems')
                    os.makedirs(stems_dir, exist_ok=True)
                    await asyncio.to_thread(transcode, temp_path,
                                            os.path.join(stems_dir, f'{name}.m4a'))
                    detected.append(name)
                    names = [n for n in STEM_NAMES
                             if os.path.exists(os.path.join(stems_dir, f'{n}.m4a'))]
                    track['stems'].update(names=names, source='upload', error=None,
                                          status='ready' if len(names) >= 2 else 'partial',
                                          progress=100)
                    if target_track not in added:
                        added.append(target_track)
                else:
                    track_id = track_id_for('upload', digest.hexdigest())
                    folder = track_dir(track_id)
                    os.makedirs(folder, exist_ok=True)
                    stored = 'upload' + extension
                    shutil.move(temp_path, os.path.join(folder, stored))
                    title, artist = _name_from_path(unquote(part.filename))
                    track = self.service.library.add_track(
                        kind='upload', value=stored, title=title, artist=artist,
                        track_id=track_id)
                    track['source'] = {'kind': 'upload', 'value': stored}
                    added.append(track_id)
            finally:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
        self.service.touch()
        if not (target_track and stem_name):
            for track_id in added:
                await self.service.enqueue(track_id, stems=stems)
            self.service.touch()
        return web.json_response({'added': added, 'stems': detected, 'skipped': skipped})

    async def prepare(self, request):
        track = self._track(request)
        body = await _json(request)
        if track['job'].get('stage') == 'failed':
            track['job'].update(stage='idle', error=None)
        if body.get('resplit') and track['stems']['status'] != 'running':
            track['stems'].update(status='none', progress=0, error=None)
        await self.service.enqueue(track['id'], stems=bool(body.get('stems', True)))
        self.service.touch()
        return web.json_response({'ok': True, 'track': self.service.library.public_track(track)})

    async def prepare_crate(self, request):
        crate_id = request.match_info['crate_id']
        crate = next((c for c in self.service.library.crates if c['id'] == crate_id), None)
        if not crate:
            raise web.HTTPNotFound(text='crate not found')
        body = await _json(request)
        for track_id in crate['track_ids']:
            await self.service.enqueue(track_id, stems=bool(body.get('stems', True)))
        self.service.touch()
        return web.json_response({'ok': True, 'count': len(crate['track_ids'])})

    async def update_track(self, request):
        """Booth-side edits: detected key, beat-grid fixes, hot cues."""
        track = self._track(request)
        body = await _json(request)
        if 'key' in body:
            key = body['key'] or None
            if key is not None and not (isinstance(key, dict) and _CAMELOT.match(str(key.get('camelot', '')))):
                return web.json_response({'error': 'bad key'}, status=400)
            track['key'] = key and {'camelot': key['camelot'], 'name': str(key.get('name', ''))[:12]}
        analysis = track.get('analysis') or {}
        if 'bpm' in body:
            bpm = body['bpm']
            if bpm is not None and not (isinstance(bpm, (int, float)) and 40 <= bpm <= 250):
                return web.json_response({'error': 'bad bpm'}, status=400)
            analysis['bpm'] = round(float(bpm), 3) if bpm else None
            analysis['bpm_manual'] = True
        if 'beat_offset' in body:
            offset = body['beat_offset']
            if offset is not None and not isinstance(offset, (int, float)):
                return web.json_response({'error': 'bad offset'}, status=400)
            analysis['beat_offset'] = round(float(offset), 4) if offset is not None else None
        if 'bpm' in body or 'beat_offset' in body:
            track['analysis'] = analysis
        if 'cues' in body:
            cues = body['cues']
            if not isinstance(cues, list) or len(cues) > 8:
                return web.json_response({'error': 'bad cues'}, status=400)
            track['cues'] = [round(float(c), 3) if isinstance(c, (int, float)) else None
                             for c in cues]
        for field in ('title', 'artist'):
            if isinstance(body.get(field), str) and body[field].strip():
                track[field] = body[field].strip()[:200]
        self.service.touch()
        return web.json_response({'track': self.service.library.public_track(track)})

    async def delete_track(self, request):
        track = self._track(request)
        lib = self.service.library
        lib.tracks.pop(track['id'], None)
        for crate in lib.crates:
            if track['id'] in crate['track_ids']:
                crate['track_ids'].remove(track['id'])
        shutil.rmtree(track_dir(track['id']), ignore_errors=True)
        self.service.touch()
        return web.json_response({'ok': True})

    async def delete_crate(self, request):
        crate_id = request.match_info['crate_id']
        lib = self.service.library
        lib.crates = [c for c in lib.crates if c['id'] != crate_id]
        self.service.touch()
        return web.json_response({'ok': True})

    async def media(self, request):
        track = self._track(request)
        name = request.match_info['name']
        folder = track_dir(track['id'])
        if name == 'full':
            path = os.path.join(folder, 'full.m4a')
        elif name == 'peaks':
            path = os.path.join(folder, 'peaks.bin')
        elif name in STEM_NAMES:
            path = os.path.join(folder, 'stems', f'{name}.m4a')
        else:
            raise web.HTTPNotFound()
        if not os.path.isfile(path):
            raise web.HTTPNotFound(text='not prepared yet')
        return web.FileResponse(path, headers={'Cache-Control': 'private, max-age=86400'})

    async def live(self, request):
        """Booth master → Huddle room. Binary frames are s16le stereo 48 kHz."""
        room = request.query.get('room', '')
        allowed = self.ui._get_allowed_guild_id(request)
        if not huddle.is_huddle_id(room) or (allowed is not None and allowed != room):
            return web.json_response({'error': 'You can only go live in your own Huddle room.'}, status=403)
        if not _live_supported():
            return web.json_response({'error': 'Huddle voice publishing is not running.'}, status=503)
        import huddle_voice
        channel_id = huddle.channel_id_from(room)
        socket = web.WebSocketResponse(heartbeat=20, max_msg_size=1 << 20)
        await socket.prepare(request)
        feed = huddle_voice.attach_live(channel_id)
        logger.info('DJ booth went live in Huddle room %s', channel_id)
        try:
            await socket.send_json({'t': 'live', 'room': room})
            async for message in socket:
                if message.type == web.WSMsgType.BINARY:
                    feed.push(message.data)
                    if feed.frames_buffered() > 25:
                        await socket.send_json({'t': 'lag', 'ms': feed.frames_buffered() * 20})
                elif message.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                    break
        finally:
            huddle_voice.detach_live(channel_id, feed)
            logger.info('DJ booth left the air in Huddle room %s', channel_id)
        return socket


async def _json(request) -> dict:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def setup(app, ui, bot=None):
    """Mount the booth and its API on the dashboard's aiohttp app."""
    service = DJService(cog_getter=lambda: ui.cog if ui.bot else None)
    routes = DJRoutes(ui, service)
    app.router.add_get('/dj', routes.page)
    app.router.add_get('/dj/', routes.page)
    app.router.add_get('/dj/static/{name}', routes.static)
    app.router.add_get('/api/dj/capabilities', routes.capabilities)
    app.router.add_get('/api/dj/library', routes.library)
    app.router.add_post('/api/dj/library', routes.add)
    app.router.add_get('/api/dj/local', routes.local_library)
    app.router.add_post('/api/dj/local', routes.add_local)
    app.router.add_post('/api/dj/upload', routes.upload)
    app.router.add_post('/api/dj/tracks/{track_id}/prepare', routes.prepare)
    app.router.add_patch('/api/dj/tracks/{track_id}', routes.update_track)
    app.router.add_delete('/api/dj/tracks/{track_id}', routes.delete_track)
    app.router.add_get('/api/dj/tracks/{track_id}/media/{name}', routes.media)
    app.router.add_post('/api/dj/crates/{crate_id}/prepare', routes.prepare_crate)
    app.router.add_delete('/api/dj/crates/{crate_id}', routes.delete_crate)
    app.router.add_get('/api/dj/live', routes.live)

    async def on_startup(_app):
        await service.start()

    async def on_cleanup(_app):
        await service.stop()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return service
