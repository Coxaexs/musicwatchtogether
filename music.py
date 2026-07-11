import discord
from discord.ext import commands
from discord import app_commands
from discord.ui import View, Button
import asyncio
import yt_dlp
import os
import audioop
import subprocess
import tempfile
from collections import deque
from dataclasses import dataclass
from typing import Optional
import aiohttp
import ctypes.util
import re
import json
import random
import logging
import logging.handlers
import glob
import math
import time
from datetime import datetime, timedelta
from urllib.parse import quote

import config

# Setup musics folder for downloaded songs
BOT_DIR = os.path.dirname(os.path.abspath(__file__))
MUSICS_FOLDER = os.path.join(BOT_DIR, 'musics')
TEMP_DOWNLOAD_FOLDER = os.path.join(tempfile.gettempdir(), 'musicbot-downloads')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.handlers.RotatingFileHandler(
            'musicbot.log', maxBytes=10 * 1024 * 1024, backupCount=3, encoding='utf-8'
        ),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('MusicBot')

try:
    os.makedirs(MUSICS_FOLDER, exist_ok=True)
except PermissionError:
    logger.warning("Could not create musics directory")
except Exception as e:
    logger.warning(f"Error creating musics directory: {e}")


def get_writable_download_dir():
    """Return the first cache directory that can accept yt-dlp part files."""
    for directory in (MUSICS_FOLDER, TEMP_DOWNLOAD_FOLDER):
        try:
            os.makedirs(directory, exist_ok=True)
            probe_path = os.path.join(directory, '.write_probe')
            with open(probe_path, 'w', encoding='utf-8') as probe_file:
                probe_file.write('ok')
            os.remove(probe_path)
            return directory
        except Exception:
            continue
    return MUSICS_FOLDER


DOWNLOADS_FOLDER = get_writable_download_dir()
if DOWNLOADS_FOLDER != MUSICS_FOLDER:
    logger.warning(f"Using fallback download cache: {DOWNLOADS_FOLDER}")

LYRICSNOW_AHEAD_SECONDS = 2
IDLE_DISCONNECT_SECONDS = 300

# ---------- audio filter chains ----------
# Every source is decoded through FFmpeg, so filters are plain -af chains.
# loudnorm evens out loudness differences between YouTube rips, Spotify
# downloads and local uploads.
LOUDNORM_FILTER = 'loudnorm=I=-16:TP=-1.5:LRA=11'

AUDIO_FILTERS = {
    'bassboost': 'bass=g=10:f=110:w=0.6',
    'nightcore': 'aresample=48000,asetrate=48000*1.25',
    'slowed': 'aresample=48000,asetrate=48000*0.85',
    '8d': 'apulsator=hz=0.09',
    'karaoke': 'pan=stereo|c0=c0-c1|c1=c1-c0',
}

# Filters that change playback speed also change how long the song takes,
# which matters for crossfade fade-out timing.
FILTER_SPEED_FACTORS = {
    'nightcore': 1.25,
    'slowed': 0.85,
}

MAX_CROSSFADE_SECONDS = 10

# ---------- AutoMix (DJ-style transitions) ----------
# AutoMix overlaps the end of the playing song with the start of the next one
# (equal-power blend, like a DJ) instead of the plain fade-out/fade-in that
# /crossfade does. When both songs have a detectable steady tempo that is
# close enough, the incoming song is stretched with atempo so the beats line
# up during the blend. Analysis needs a local file, which is the normal case
# since downloads are cached as mp3; streams fall back to a plain blend.
AUTOMIX_MIN_BLEND_SECONDS = 4
AUTOMIX_MAX_BLEND_SECONDS = 15
AUTOMIX_DEFAULT_BLEND_SECONDS = 8
AUTOMIX_MIN_TRACK_SECONDS = 45     # don't blend out of very short tracks
AUTOMIX_TEMPO_TOLERANCE = 0.08     # max tempo stretch when beat-matching (8%)
AUTOMIX_ANALYSIS_RATE = 11025      # mono decode rate for tempo/silence analysis
AUTOMIX_BPM_MIN = 60.0
AUTOMIX_BPM_MAX = 200.0
AUTOMIX_BPM_MIN_CONFIDENCE = 0.30  # normalized autocorrelation peak to trust a tempo
AUTOMIX_MIN_ONSET_FLUX = 0.025     # mean log-energy rise; below this there is no beat to find

# Give up on loading a song after this long so one giant/slow download can't
# freeze the whole player (the queue just moves on to the next song)
SONG_LOAD_TIMEOUT_SECONDS = 300

# Search guard: when a plain text search matches something longer than this
# (TV episodes, 10-hour loops), prefer a shorter result from the top 5 unless
# the query clearly asked for long content
LONG_RESULT_SECONDS = 900
LONG_INTENT_KEYWORDS = (
    'mix', 'album', 'full', 'episode', 'bölüm', 'hour', 'saat', 'live',
    'concert', 'konser', 'podcast', 'compilation', 'nonstop', 'non-stop',
    'playlist', 'radio',
)


def parse_duration_to_seconds(duration: str) -> Optional[int]:
    """Parse a 'mm:ss' / 'h:mm:ss' duration string into seconds."""
    if not duration:
        return None
    duration = str(duration).strip()
    if duration.isdigit():
        return int(duration)
    parts = duration.split(':')
    if not 2 <= len(parts) <= 3 or not all(p.strip().isdigit() for p in parts):
        return None
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return seconds


def build_audio_options(filter_name: Optional[str] = None,
                        crossfade_seconds: int = 0,
                        duration_seconds: Optional[int] = None,
                        start_seconds: int = 0,
                        extra_filters: Optional[list] = None) -> str:
    """Build the FFmpeg output options string (-vn -af "...") for a song.

    The fade-out start is computed in output timestamps: after an input seek
    (-ss) the output clock restarts at 0, and speed-changing filters shrink
    or stretch the remaining time.
    """
    chain = []
    if filter_name and filter_name in AUDIO_FILTERS:
        chain.append(AUDIO_FILTERS[filter_name])
    chain.append(LOUDNORM_FILTER)
    if extra_filters:
        chain.extend(extra_filters)

    if crossfade_seconds > 0:
        chain.append(f'afade=t=in:st=0:d={crossfade_seconds}')
        if duration_seconds:
            speed = FILTER_SPEED_FACTORS.get(filter_name, 1.0)
            remaining = max(0, duration_seconds - max(0, start_seconds)) / speed
            fade_out_start = remaining - crossfade_seconds
            # Only fade out when the song is long enough for it to make sense
            if fade_out_start > crossfade_seconds:
                chain.append(f'afade=t=out:st={fade_out_start:.1f}:d={crossfade_seconds}')

    return f'-vn -af "{",".join(chain)}"'


# ---------- AutoMix analysis & mixing ----------

def _probe_duration_seconds(path: str) -> Optional[float]:
    """Real duration of an audio file via ffprobe (queue metadata can be stale)."""
    cmd = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
           '-of', 'default=noprint_wrappers=1:nokey=1', path]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return float(result.stdout.strip())
    except Exception:
        return None


def _decode_pcm_mono(path: str, start: float, duration: float,
                     rate: int = AUTOMIX_ANALYSIS_RATE) -> Optional[bytes]:
    """Decode a slice of an audio file to raw mono s16 PCM for analysis."""
    cmd = [
        'ffmpeg', '-v', 'error', '-nostdin',
        '-ss', f'{max(0.0, start):.2f}', '-t', f'{max(0.1, duration):.2f}',
        '-i', path, '-map', 'a:0', '-ac', '1', '-ar', str(rate),
        '-f', 's16le', 'pipe:1',
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=60)
    except Exception:
        return None
    data = result.stdout
    if not data or len(data) < rate:  # less than half a second of audio
        return None
    if len(data) % 2:
        data = data[:-1]
    return data


def _rms_envelope(pcm: bytes, hop_samples: int) -> list:
    """Per-window RMS levels of mono s16 PCM."""
    frame_bytes = hop_samples * 2
    return [audioop.rms(pcm[i:i + frame_bytes], 2)
            for i in range(0, len(pcm) - frame_bytes + 1, frame_bytes)]


def _estimate_bpm(pcm: bytes, rate: int = AUTOMIX_ANALYSIS_RATE):
    """Estimate tempo of mono s16 PCM via onset-energy autocorrelation.

    Returns (bpm, confidence). Confidence is the autocorrelation peak
    normalized by the onset signal's energy (0..~1): a steady dance beat
    scores high, rubato/ambient audio scores near zero. Half/double-time
    readings are acceptable here because _tempo_match_ratio treats them
    as the same groove.
    """
    hop, win = 128, 256
    total_samples = len(pcm) // 2
    if total_samples < rate * 10:  # need ~10s to lock onto a tempo
        return None, 0.0

    energies = [audioop.rms(pcm[2 * s: 2 * (s + win)], 2)
                for s in range(0, total_samples - win, hop)]

    # Onset strength: rectified rise in log energy
    onsets = [0.0]
    prev = math.log(energies[0] + 1)
    for e in energies[1:]:
        cur = math.log(e + 1)
        onsets.append(max(0.0, cur - prev))
        prev = cur
    mean_onset = sum(onsets) / len(onsets)
    if mean_onset < AUTOMIX_MIN_ONSET_FLUX:
        return None, 0.0  # too little energy movement: drone/silence, no beat
    onsets = [o - mean_onset for o in onsets]

    frames_per_sec = rate / hop
    min_lag = max(1, int(frames_per_sec * 60.0 / AUTOMIX_BPM_MAX))
    max_lag = int(frames_per_sec * 60.0 / AUTOMIX_BPM_MIN)
    if max_lag + 1 >= len(onsets):
        return None, 0.0

    count = len(onsets) - max_lag
    zero_lag = sum(o * o for o in onsets[:count]) / count
    scores = {}
    for lag in range(min_lag, max_lag + 1):
        total = 0.0
        for i in range(count):
            total += onsets[i] * onsets[i + lag]
        scores[lag] = total / count

    best_lag = max(scores, key=scores.get)
    best = scores[best_lag]
    if best <= 0 or zero_lag <= 0:
        return None, 0.0
    confidence = best / zero_lag

    # Parabolic interpolation around the peak for sub-frame lag precision
    refined = float(best_lag)
    if min_lag < best_lag < max_lag:
        y0, y1, y2 = scores[best_lag - 1], scores[best_lag], scores[best_lag + 1]
        denom = y0 - 2 * y1 + y2
        if abs(denom) > 1e-12:
            refined += 0.5 * (y0 - y2) / denom

    return 60.0 * frames_per_sec / refined, confidence


def _tempo_match_ratio(bpm_out: float, bpm_in: float) -> Optional[float]:
    """atempo ratio that beat-matches the incoming song to the outgoing one.

    Half/double-time readings count as a match (85 vs 170 BPM is the same
    groove). Returns None when no reading is within the stretch tolerance.
    """
    best = None
    for mult in (0.5, 1.0, 2.0):
        ratio = bpm_out / (bpm_in * mult)
        if abs(ratio - 1.0) <= AUTOMIX_TEMPO_TOLERANCE:
            if best is None or abs(ratio - 1.0) < abs(best - 1.0):
                best = ratio
    return best


def analyze_track_edges(path: str) -> Optional[dict]:
    """Analyze a local audio file for AutoMix transitions.

    Returns the track's real duration, where its audible content starts and
    ends (silence trimmed), and tempo estimates for the first and last ~30
    seconds. Blocking (ffmpeg + math, ~1s) - call in an executor.
    """
    duration = _probe_duration_seconds(path)
    if not duration or duration <= 0:
        return None

    rate = AUTOMIX_ANALYSIS_RATE
    head = _decode_pcm_mono(path, 0.0, min(30.0, duration))
    tail_len = min(40.0, duration)
    tail_start = max(0.0, duration - tail_len)
    tail = _decode_pcm_mono(path, tail_start, tail_len + 1.0)
    if head is None or tail is None:
        return None

    hop = 512  # ~46ms resolution is plenty for silence trimming
    head_env = _rms_envelope(head, hop)
    tail_env = _rms_envelope(tail, hop)
    if not head_env or not tail_env:
        return None
    peak = max(max(head_env), max(tail_env)) or 1
    floor = peak * 0.02  # ≈ -34dB below peak: silence / noise floor

    start_at = 0.0
    for i, level in enumerate(head_env):
        if level >= floor:
            start_at = i * hop / rate
            break

    end_at = duration
    for i in range(len(tail_env) - 1, -1, -1):
        if tail_env[i] >= floor:
            end_at = min(duration, tail_start + (i + 1) * hop / rate)
            break

    bpm_head, conf_head = _estimate_bpm(head[int(start_at * rate) * 2:])
    tail_cut = int(max(0.0, end_at - tail_start) * rate) * 2
    bpm_tail, conf_tail = _estimate_bpm(tail[:tail_cut])

    return {
        'duration': duration,
        'start_at': start_at,
        'end_at': end_at,
        'bpm_head': bpm_head if conf_head >= AUTOMIX_BPM_MIN_CONFIDENCE else None,
        'bpm_tail': bpm_tail if conf_tail >= AUTOMIX_BPM_MIN_CONFIDENCE else None,
    }


@dataclass
class AutoMixPlan:
    """How to blend the next song in when the current one is about to end."""
    song: 'Song'                     # queue entry this plan was built for
    fade_seconds: float
    file_path: Optional[str] = None  # local file for a trimmed/beat-matched start
    start_seconds: float = 0.0       # lead-in silence to skip
    atempo: float = 1.0              # tempo stretch to match the outgoing song
    bpm_out: Optional[float] = None
    bpm_in: Optional[float] = None


class AutoMixTransition(discord.AudioSource):
    """Blends the currently playing source into the next song's source.

    Swapped in live via voice_client.source, so the player thread and its
    after-callback keep running. During the blend both sources are read and
    mixed with an equal-power curve; the fade advances per frame, so pausing
    pauses the blend too. Afterwards it passes the incoming source through
    (a follow-up transition unwraps it again via active_source()).
    """

    def __init__(self, outgoing, incoming, fade_seconds: float):
        if isinstance(outgoing, AutoMixTransition):
            outgoing = outgoing.active_source()  # don't nest finished transitions
        self.outgoing = outgoing
        self.incoming = incoming
        self.total_frames = max(1, int(fade_seconds * 1000 / 20))  # 20ms frames
        self.frames_done = 0
        self._outgoing_finished = False

    def active_source(self):
        return self.incoming if self._outgoing_finished else self

    @property
    def volume(self):
        return getattr(self.incoming, 'volume', 1.0)

    @volume.setter
    def volume(self, value):
        for source in (self.incoming, self.outgoing):
            if hasattr(source, 'volume'):
                source.volume = value

    def is_opus(self) -> bool:
        return False

    def _finish_outgoing(self):
        self._outgoing_finished = True
        try:
            self.outgoing.cleanup()
        except Exception:
            pass

    def read(self) -> bytes:
        in_frame = self.incoming.read()
        if self._outgoing_finished:
            return in_frame
        out_frame = self.outgoing.read()
        if not out_frame:
            self._finish_outgoing()
            return in_frame

        self.frames_done += 1
        progress = min(1.0, self.frames_done / self.total_frames)
        faded_out = audioop.mul(out_frame, 2, math.cos(progress * math.pi / 2))
        if self.frames_done >= self.total_frames:
            self._finish_outgoing()  # blend done; drop the old song's remainder
        if not in_frame:
            return faded_out  # incoming ran dry mid-blend; let the old song carry it

        if len(in_frame) < len(faded_out):
            in_frame += b'\x00' * (len(faded_out) - len(in_frame))
        faded_in = audioop.mul(in_frame, 2, math.sin(progress * math.pi / 2))
        if len(faded_out) < len(faded_in):
            faded_out += b'\x00' * (len(faded_in) - len(faded_out))
        return audioop.add(faded_out, faded_in, 2)

    def cleanup(self):
        for source in (self.outgoing, self.incoming):
            try:
                source.cleanup()
            except Exception:
                pass


INVITE_ALLOWED_USER_IDS = {
    532622134615343124,
    604289866943037441,
    1383175146050949342,
    414360024568299522,
}
BOT_INVITE_URL = "https://discord.com/oauth2/authorize?client_id=1400583965240197230&scope=bot&permissions=1072131211201"


def load_opus():
    """Load the opus library for voice support"""
    if discord.opus.is_loaded():
        logger.info("✅ Opus already loaded")
        return True

    opus_paths = [
        '/opt/homebrew/lib/libopus.dylib',
        '/usr/local/lib/libopus.dylib',
        '/opt/homebrew/opt/opus/lib/libopus.dylib',
        '/usr/local/opt/opus/lib/libopus.dylib',
        '/usr/lib/x86_64-linux-gnu/libopus.so.0',
        '/usr/lib/aarch64-linux-gnu/libopus.so.0',
        '/usr/lib/libopus.so.0',
        '/usr/lib/libopus.so',
        ctypes.util.find_library('opus'),
    ]

    for path in opus_paths:
        if path and os.path.exists(path) if path and not path.startswith('opus') else path:
            try:
                discord.opus.load_opus(path)
                logger.info(f"✅ Opus loaded from: {path}")
                return True
            except Exception as e:
                logger.warning(f"Failed to load opus from {path}: {e}")

    try:
        discord.opus.load_opus('opus')
        logger.info("✅ Opus loaded (default)")
        return True
    except Exception as e:
        logger.error(f"Failed to load opus (default): {e}")

    logger.error("❌ Could not load Opus library!")
    return False


opus_loaded = load_opus()
if not opus_loaded:
    logger.critical("CRITICAL: Opus not loaded! Voice functionality will not work!")

try:
    import spotipy
    from spotipy.oauth2 import SpotifyClientCredentials
    SPOTIFY_AVAILABLE = bool(config.SPOTIFY_CLIENT_ID and config.SPOTIFY_CLIENT_SECRET)
    if SPOTIFY_AVAILABLE:
        sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
            client_id=config.SPOTIFY_CLIENT_ID,
            client_secret=config.SPOTIFY_CLIENT_SECRET
        ))
        logger.info("✅ Spotify API initialized")
    else:
        sp = None
        logger.info("Spotify API credentials not configured")
except ImportError:
    SPOTIFY_AVAILABLE = False
    sp = None
    logger.info("spotipy not installed (pip install spotipy for Spotify support)")


PIPED_INSTANCES = [
    'https://pipedapi.kavin.rocks',
    'https://pipedapi-us.kavin.rocks',
    'https://pipedapi.tokhmi.xyz',
    'https://api-piped.mha.fi',
    'https://piped-api.garudalinux.org',
    'https://pipedapi.smnz.de',
]


async def get_youtube_stream_piped(video_id: str) -> Optional[str]:
    """Get YouTube stream URL using Piped API with retry logic"""
    import ssl

    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE

    connector = aiohttp.TCPConnector(ssl=ssl_context, timeout=aiohttp.ClientTimeout(total=20))
    async with aiohttp.ClientSession(connector=connector) as session:
        for attempt, instance in enumerate(PIPED_INSTANCES):
            try:
                url = f"{instance}/streams/{video_id}"
                logger.info(f"Trying Piped ({attempt+1}/{len(PIPED_INSTANCES)}): {instance}")
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        try:
                            data = await resp.json()
                            audio_streams = data.get('audioStreams', [])
                            if audio_streams:
                                best = max(audio_streams, key=lambda x: x.get('bitrate', 0))
                                stream_url = best.get('url')
                                if stream_url:
                                    logger.info(f"✅ Got stream from Piped: {instance}")
                                    return stream_url
                        except Exception as e:
                            logger.info(f"Piped JSON error: {str(e)[:50]}")
                    elif resp.status == 404:
                        logger.info(f"Piped: Video not found (404) - skipping other Piped instances")
                        return None  # Video not found, don't try other Piped instances
                    else:
                        logger.info(f"Piped: HTTP {resp.status}")
            except asyncio.TimeoutError:
                logger.info(f"Piped timeout: {instance}")
            except Exception as e:
                logger.info(f"Piped error: {str(e)[:50]}")
    return None


INVIDIOUS_INSTANCES = [
    'https://invidious.jing.rocks',
    'https://iv.melmac.space',
    'https://inv.vern.cc',
    'https://yt.artemislena.eu',
    'https://invidious.protokolla.fi',
    'https://inv.riverside.rocks',
]


async def get_youtube_stream_invidious(video_id: str) -> Optional[str]:
    """Get YouTube stream URL using Invidious API with retry logic"""
    import ssl

    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE

    connector = aiohttp.TCPConnector(ssl=ssl_context, timeout=aiohttp.ClientTimeout(total=20))
    async with aiohttp.ClientSession(connector=connector) as session:
        for attempt, instance in enumerate(INVIDIOUS_INSTANCES):
            try:
                url = f"{instance}/api/v1/videos/{video_id}"
                logger.info(f"Trying Invidious ({attempt+1}/{len(INVIDIOUS_INSTANCES)}): {instance}")
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        try:
                            data = await resp.json()
                            audio_formats = [f for f in data.get('adaptiveFormats', []) if f.get('type', '').startswith('audio')]
                            if audio_formats:
                                best = max(audio_formats, key=lambda x: x.get('bitrate', 0))
                                stream_url = best.get('url')
                                if stream_url:
                                    logger.info(f"✅ Got stream from Invidious: {instance}")
                                    return stream_url
                            logger.info(f"Invidious: No audio formats found at {instance}")
                        except Exception as e:
                            logger.info(f"Invidious JSON error: {str(e)[:50]}")
                    elif resp.status == 404:
                        logger.info(f"Invidious: Video not found (404) - skipping other instances")
                        return None  # Video not found, don't try other instances
                    else:
                        logger.info(f"Invidious: HTTP {resp.status} from {instance}")
            except asyncio.TimeoutError:
                logger.info(f"Invidious timeout: {instance}")
            except Exception as e:
                logger.info(f"Invidious error from {instance}: {str(e)[:50]}")
    return None


# 24/7 Mode configuration
TWENTYFOURSEVEN_PLAYLIST_URL = "https://open.spotify.com/playlist/22vZOI67SwrqvYzA92juhR"
TWENTYFOURSEVEN_CACHE_DIR = os.path.abspath(config.MUSIC_LIBRARY_PATH or MUSICS_FOLDER)
TWENTYFOURSEVEN_ACTIVE_GUILD = None  # Only one server can use 24/7 at a time
try:
    os.makedirs(TWENTYFOURSEVEN_CACHE_DIR, exist_ok=True)
except PermissionError:
    logger.warning(f"Could not create cache directory: {TWENTYFOURSEVEN_CACHE_DIR}")
except Exception as e:
    logger.warning(f"Error creating cache directory: {e}")
# Check if song file is already downloaded
def find_cached_song(video_id: str = None):
    """Find a cached song by EXACT video ID match only.
    
    This is the most reliable method - searches for [videoID] in filenames.
    Returns matching file path or None if not found.
    
    IMPORTANT: We deliberately do NOT support title/artist matching
    because it causes false positives and plays wrong songs!
    """
    if not video_id or video_id == 'unknown':
        return None
    
    # Build library paths (bot musics + configured MUSIC_LIBRARY_PATH)
    library_paths = [MUSICS_FOLDER]
    try:
        libcfg = os.path.abspath(config.MUSIC_LIBRARY_PATH) if getattr(config, 'MUSIC_LIBRARY_PATH', None) else None
        if libcfg and libcfg not in library_paths:
            library_paths.append(libcfg)
    except Exception:
        pass

    debug = str(getattr(config, 'CACHE_LOOKUP_DEBUG', '0')) == '1'
    if debug:
        logger.debug(f"Cache lookup for video_id: {video_id}")

    # Try NEW format first: [videoID].mp3 in subdirectories (artist folders)
    # CRITICAL: Escape literal brackets in glob pattern!
    # In glob: [x] means "character class" - we need literal [ and ]
    # Escape as: [[] for literal [, and []] for literal ]
    for directory in (*library_paths, DOWNLOADS_FOLDER, TEMP_DOWNLOAD_FOLDER):
        try:
            if debug:
                logger.debug(f"  Searching in {directory}")
            recursive_pattern = os.path.join(directory, f"**/*[[]" + video_id + "[]]" + ".mp3")
            matching_files = glob.glob(recursive_pattern, recursive=True)
            if not matching_files:
                flat_pattern = os.path.join(directory, f"*[[]" + video_id + "[]]" + ".mp3")
                matching_files = glob.glob(flat_pattern)
            if matching_files:
                if debug:
                    logger.debug(f"  Found by id (new format): {matching_files[0]}")
                return matching_files[0]
        except Exception as e:
            if debug:
                logger.debug(f"  Error: {e}")
            continue

    # Try FALLBACK: old format ytdl_<videoID>.* in root (for backward compat)
    for directory in (*library_paths, DOWNLOADS_FOLDER, TEMP_DOWNLOAD_FOLDER):
        try:
            matching_files = glob.glob(os.path.join(directory, f"ytdl_{video_id}.*"))
            if matching_files:
                if debug:
                    logger.debug(f"  Found by id (old format): {matching_files[0]}")
                return matching_files[0]
        except Exception as e:
            if debug:
                logger.debug(f"  Error: {e}")
            continue

    if debug:
        logger.debug(f"  No cache found for video_id: {video_id}")
    return None

def get_autocomplete_suggestions(query: str) -> list[str]:
    if not query or len(query) < 2:
        return []
    
    query_lower = query.lower()
    suggestions = set()
    
    # 1. Search in cached lyrics files
    base_dir = os.path.dirname(os.path.abspath(__file__))
    lyrics_dir = os.path.join(base_dir, "lyrics")
    if os.path.exists(lyrics_dir):
        try:
            for entry in os.scandir(lyrics_dir):
                if entry.is_file() and entry.name.endswith(".json"):
                    normalized_query = query_lower.replace(" ", "_")
                    if normalized_query in entry.name.lower() or query_lower in entry.name.lower().replace("_", " "):
                        try:
                            with open(entry.path, 'r', encoding='utf-8') as f:
                                data = json.load(f)
                                if data and data.get('track'):
                                    suggestions.add(data['track'])
                        except Exception:
                            pass
        except Exception:
            pass
                        
    # 2. Search in local music folders
    library_paths = [os.path.join(base_dir, "musics")]
    if getattr(config, 'MUSIC_LIBRARY_PATH', None):
        library_paths.append(config.MUSIC_LIBRARY_PATH)
        
    for directory in library_paths:
        if os.path.exists(directory):
            try:
                for root, _, files in os.walk(directory):
                    for file in files:
                        if file.endswith(('.mp3', '.wav', '.ogg', '.flac', '.m4a', '.aac', '.opus', '.webm')):
                            name = os.path.splitext(file)[0]
                            name = re.sub(r'\s*-?\s*\[[0-9A-Za-z_-]{11}\]$', '', name).strip()
                            if query_lower in name.lower():
                                suggestions.add(name)
            except Exception:
                pass
                
    def sort_key(s):
        try:
            return s.lower().index(query_lower), s.lower()
        except ValueError:
            return 999, s.lower()
            
    sorted_suggestions = sorted(list(suggestions), key=sort_key)
    return sorted_suggestions[:25]


def sanitize_filename(name: str) -> str:
    """Create a filesystem-safe filename from input."""
    # Replace problematic characters and trim
    name = re.sub(r'[\\/:*?"<>|]', '', name)
    name = name.strip()
    # Collapse whitespace
    name = re.sub(r'\s+', ' ', name)
    return name[:200]


def build_download_template(info: Optional[dict], video_id: str) -> str:
    """Build a writable yt-dlp output template.

    Prefer an artist subfolder when it can be created and written to.
    Fall back to a flat filename when an existing artist folder is not writable.
    """
    info = info or {}
    artist_name = sanitize_filename(info.get('artist') or info.get('uploader') or 'Unknown Artist')
    title_name = sanitize_filename(info.get('title') or 'Unknown Title')

    artist_dir = os.path.join(DOWNLOADS_FOLDER, artist_name)
    try:
        os.makedirs(artist_dir, exist_ok=True)
        if os.access(artist_dir, os.W_OK):
            return os.path.join(artist_dir, f"{title_name} - [{video_id}].%(ext)s")
    except Exception:
        pass

    return os.path.join(DOWNLOADS_FOLDER, f"{artist_name} - {title_name} - [{video_id}].%(ext)s")


YOUTUBE_VIDEO_ID_RE = re.compile(
    r'(?:youtube\.com/(?:watch\?(?:[^#\s]*&)?v=|shorts/|embed/|live/|v/)|youtu\.be/)'
    r'([0-9A-Za-z_-]{11})'
)


def extract_youtube_video_id(url: str) -> Optional[str]:
    """Pull the 11-char video id out of any common YouTube URL form."""
    if not url:
        return None
    match = YOUTUBE_VIDEO_ID_RE.search(url)
    return match.group(1) if match else None


def normalize_youtube_url(url: str) -> str:
    """Strip playlist/share junk (list=, si=, index=...) down to a canonical watch URL."""
    video_id = extract_youtube_video_id(url)
    if video_id:
        return f"https://www.youtube.com/watch?v={video_id}"
    return url


async def fetch_oembed_info(url: str) -> Optional[dict]:
    """Fetch title/thumbnail for a YouTube or Spotify URL via oEmbed.

    Works without API keys and survives yt-dlp/Spotify API outages, so a valid
    link can always be resolved to at least a title.
    """
    if 'spotify' in url:
        oembed_url = f"https://open.spotify.com/oembed?url={quote(url, safe='')}"
    else:
        oembed_url = f"https://www.youtube.com/oembed?url={quote(url, safe='')}&format=json"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(oembed_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get('title'):
                        return {
                            'title': data['title'],
                            'thumbnail': data.get('thumbnail_url'),
                            'author': data.get('author_name'),
                        }
    except Exception as e:
        logger.info(f"oEmbed lookup failed for {url}: {str(e)[:80]}")
    return None


async def notify_media_server(file_path: str):
    """Notify Navidrome or Lidarr to scan library if configured (best-effort)."""
    async with aiohttp.ClientSession() as session:
        # Navidrome
        if getattr(config, 'NAVIDROME_URL', None):
            url = config.NAVIDROME_URL.rstrip('/')
            headers = {}
            if getattr(config, 'NAVIDROME_API_KEY', None):
                headers['X-Api-Key'] = config.NAVIDROME_API_KEY
                headers['Authorization'] = f"Bearer {config.NAVIDROME_API_KEY}"

            try:
                scan_url = f"{url}/api/v1/scan"
                async with session.post(scan_url, headers=headers, timeout=15) as resp:
                    if resp.status in (200, 204):
                        logger.info(f"Requested Navidrome scan successfully for {file_path}")
                    else:
                        logger.warning(f"Navidrome scan returned {resp.status} for {file_path}")
            except Exception as e:
                logger.warning(f"Navidrome scan request failed: {e}")

        # Lidarr (best-effort). Try the common command endpoint.
        if getattr(config, 'LIDARR_URL', None):
            lidarr_base = config.LIDARR_URL.rstrip('/')
            headers = {}
            if getattr(config, 'LIDARR_API_KEY', None):
                headers['X-Api-Key'] = config.LIDARR_API_KEY

            # Try /api/v1/command
            try:
                cmd_url = f"{lidarr_base}/api/v1/command"
                payload = {"name": "Rescan"}
                async with session.post(cmd_url, json=payload, headers=headers, timeout=15) as resp:
                    if resp.status in (200, 201, 202):
                        logger.info(f"Requested Lidarr rescan for {file_path} via {cmd_url}")
                    else:
                        logger.warning(f"Lidarr rescan returned {resp.status} for {file_path} via {cmd_url}")
            except Exception as e:
                logger.warning(f"Lidarr rescan request failed: {e}")


# yt-dlp configuration - download audio to avoid streaming 403 errors
YTDL_FORMAT_OPTIONS = {
    'format': 'bestaudio/best',
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': False,
    'no_warnings': False,
    'default_search': 'auto',
    'source_address': '0.0.0.0',
    # Use a flat ytdl_<id> template in the downloads folder to avoid
    # permission failures when artist subfolders exist but aren't writable.
    'outtmpl': os.path.join(DOWNLOADS_FOLDER, 'ytdl_%(id)s.%(ext)s'),
    'sleep_interval': 3,
    'max_sleep_interval': 10,
    'extractor_retries': 5,
    'fragment_retries': 5,
    'skip_unavailable_fragments': True,
    'postprocessors': [{
        'key': 'FFmpegExtractAudio',
        'preferredcodec': 'mp3',
        'preferredquality': '192',
    }],
    'http_headers': {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    },
}

FFMPEG_OPTIONS = {
    'options': '-vn'
}
# Separate extractor for getting info only
YTDL_SEARCH_OPTIONS = {
    'format': 'bestaudio/best',
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',
    'skip_download': True,
    'extract_flat': 'in_playlist',
}

# Extractor for playlists
YTDL_PLAYLIST_OPTIONS = {
    'format': 'bestaudio/best',
    'noplaylist': False,  # Enable playlist extraction
    'nocheckcertificate': True,
    'ignoreerrors': True,  # Continue on errors
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',
    'skip_download': True,
    'extract_flat': 'in_playlist',
}

ytdl = yt_dlp.YoutubeDL(YTDL_FORMAT_OPTIONS)
ytdl_search = yt_dlp.YoutubeDL(YTDL_SEARCH_OPTIONS)
ytdl_playlist = yt_dlp.YoutubeDL(YTDL_PLAYLIST_OPTIONS)


@dataclass
class Song:
    """Represents a song in the queue"""
    title: str
    url: str  # webpage URL for YouTube, file path for local
    duration: str
    requester: discord.Member
    source_type: str  # 'youtube', 'spotify', 'local'
    thumbnail: Optional[str] = None
    artist: Optional[str] = None
    album: Optional[str] = None
    genres: tuple[str, ...] = ()
    played_at: Optional[int] = None


class YTDLSource(discord.PCMVolumeTransformer):
    """Audio source using yt-dlp"""
    
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title')
        self.url = data.get('webpage_url')

    @classmethod
    async def from_url(cls, url, *, loop=None, stream=True, audio_options=None):
        """Create an audio source from a URL - tries yt-dlp CLI first with 3 retries on unavailable"""
        loop = loop or asyncio.get_event_loop()
        if audio_options is None:
            audio_options = build_audio_options()
        
        # Extract video ID from URL
        video_id_match = extract_youtube_video_id(url)
        video_id = video_id_match if video_id_match else 'unknown'
        
        # Check if song is already cached by VIDEO ID ONLY (most reliable method)
        cached_file = find_cached_song(video_id=video_id)
        
        if cached_file:
            logger.info(f"🎵 Found cached: {os.path.basename(cached_file)}")
            print(f"🎵 Found cached: {os.path.basename(cached_file)}")
            # Get metadata (best-effort; the cached audio is enough to play)
            try:
                data = await loop.run_in_executor(
                    None,
                    lambda: ytdl_search.extract_info(url, download=False)
                )
                if 'entries' in data:
                    data = data['entries'][0] if data['entries'] else None
            except Exception:
                data = None

            if not data:
                title = os.path.splitext(os.path.basename(cached_file))[0]
                title = re.sub(r'\s*-?\s*\[[0-9A-Za-z_-]{11}\]$', '', title).strip()
                data = {'title': title or 'Unknown', 'webpage_url': url}

            source = discord.FFmpegPCMAudio(cached_file, options=audio_options)
            return cls(source, data=data)

        # Try download up to 3 times if video appears unavailable (false positives happen)
        max_retries = 3
        for retry_attempt in range(max_retries):
            try:
                return await cls._try_download(url, loop, video_id, video_id_match, audio_options)
            except Exception as e:
                error_str = str(e).lower()
                if 'unavailable' in error_str or 'not available' in error_str:
                    if retry_attempt < max_retries - 1:
                        wait_time = 3 + ((retry_attempt * 3)/2)  # 3s, 5s, 7s delays
                        print(f"⏳ Video unavailable, retrying in {wait_time}s... (attempt {retry_attempt+2}/{max_retries})")
                        logger.info(f"Retry {retry_attempt+1}/{max_retries} for {video_id}")
                        await asyncio.sleep(wait_time)
                        continue
                    else:
                        # All retries exhausted
                        raise
                else:
                    raise
        
        raise Exception("All YouTube download methods failed")
    
    @classmethod
    async def _try_download(cls, url, loop, video_id, video_id_match, audio_options=None):
        """Internal method for download attempt"""
        if audio_options is None:
            audio_options = build_audio_options()
        # Resolve metadata before downloading so we can choose a safe,
        # organized output path for this specific song.
        data = None
        try:
            data = await loop.run_in_executor(
                None,
                lambda: ytdl_search.extract_info(url, download=False)
            )
            if 'entries' in data:
                data = data['entries'][0] if data['entries'] else None
        except Exception as e:
            error_str = str(e).lower()
            if 'unavailable' in error_str or 'not available' in error_str:
                logger.error(f"❌ Video unavailable: {video_id} - {e}")
                raise Exception(f"Video unavailable (age-restricted, regional block, or deleted)")
            data = None

        download_template = build_download_template(data, video_id)
        
        # Try yt-dlp CLI first (works on Linux)
        logger.info(f"🎵 Downloading with yt-dlp CLI: {video_id}")
        print(f"🎵 Downloading with yt-dlp CLI: {video_id}")
        try:
            import subprocess
            import sys
            
            # Use venv's Python to ensure we get the latest yt-dlp version
            venv_python = os.path.join(BOT_DIR, 'env', 'bin', 'python3')
            python_executable = venv_python if os.path.exists(venv_python) else sys.executable
            
            # Run yt-dlp using python -m for proper environment handling
            cmd = [
                python_executable,
                '-m', 'yt_dlp',
                '-f', 'bestaudio/best',
                '-x',
                '--audio-format', 'mp3',
                '--audio-quality', '192',
                '-R', '3',
                '--socket-timeout', '30',
                '--user-agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                '-o', download_template,
                url
            ]
            
            print(f"  Running yt-dlp...")
            result = await loop.run_in_executor(
                None,
                lambda: subprocess.run(cmd, capture_output=True, text=True, timeout=90)
            )
            
            # Check for video unavailable errors
            stderr_lower = result.stderr.lower()
            if 'unavailable' in stderr_lower or 'not available' in stderr_lower:
                logger.error(f"❌ CLI: Video unavailable: {video_id}")
                raise Exception(f"Video unavailable")
            
            # Check if file was created regardless of return code (yt-dlp returns warnings as non-zero)
            file_glob = glob.escape(download_template.replace('.%(ext)s', '')) + '.*'
            matching_files = glob.glob(file_glob)
            if matching_files:
                filename = matching_files[0]
                logger.info(f"✅ Downloaded: {filename}")
                print(f"✅ Downloaded: {filename}")

                if not data:
                    title = os.path.splitext(os.path.basename(filename))[0]
                    title = re.sub(r'\s*-?\s*\[[0-9A-Za-z_-]{11}\]$', '', title).strip()
                    data = {'title': title or 'Unknown', 'webpage_url': url}
                source = discord.FFmpegPCMAudio(filename, options=audio_options)
                return cls(source, data=data)
            else:
                if result.returncode != 0:
                    stderr_msg = result.stderr[:200] if result.stderr else 'Unknown error'
                    print(f"   CLI error: {stderr_msg}")
        except Exception as e:
            error_str = str(e).lower()
            if 'unavailable' in error_str:
                raise
            print(f"   CLI error: {e}")

        # Fallback: Use yt-dlp's Python API directly before giving up to proxies
        try:
            print(f"🔁 Trying yt-dlp Python API: {video_id}")
            api_opts = dict(YTDL_FORMAT_OPTIONS)
            api_opts['outtmpl'] = download_template
            ytdl_instance = yt_dlp.YoutubeDL(api_opts)
            data = await loop.run_in_executor(
                None,
                lambda: ytdl_instance.extract_info(url, download=True)
            )

            if 'entries' in data:
                data = data['entries'][0] if data['entries'] else None

            if data:
                api_cached_file = find_cached_song(video_id)
                if api_cached_file:
                    print(f"✅ Downloaded via API: {api_cached_file}")
                    logger.info(f"Downloaded via API: {api_cached_file}")
                    source = discord.FFmpegPCMAudio(api_cached_file, options=audio_options)
                    return cls(source, data=data)
        except Exception as e:
            error_str = str(e).lower()
            if 'unavailable' in error_str or 'not available' in error_str:
                logger.error(f"❌ API: Video unavailable: {video_id}")
                raise Exception(f"Video unavailable")
            print(f"   API error: {error_str[:100]}")
        
        # Fallback: Try Piped/Invidious proxies only if yt-dlp didn't already fail with "unavailable"
        if video_id_match:
            print(f"🔍 Trying YouTube proxies...")
            
            # Try Piped
            stream_url = await get_youtube_stream_piped(video_id)
            
            # Try Invidious if Piped fails
            if not stream_url:
                stream_url = await get_youtube_stream_invidious(video_id)
            
            if stream_url:
                # Get video info for metadata
                try:
                    data = await loop.run_in_executor(
                        None,
                        lambda: ytdl_search.extract_info(url, download=False)
                    )
                    
                    if 'entries' in data:
                        data = data['entries'][0] if data['entries'] else None
                    
                    if data:
                        # Stream directly from proxy URL
                        print(f"✅ Streaming from proxy")
                        logger.info(f"Streaming from proxy: {video_id}")
                        source = discord.FFmpegPCMAudio(
                            stream_url,
                            before_options='-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
                            options=audio_options
                        )
                        return cls(source, data=data)
                except Exception as e:
                    print(f"Proxy metadata error: {e}")
            else:
                logger.warning(f"Proxies unavailable for video: {video_id}")
        
        raise Exception("All YouTube download methods failed")


class MusicControlView(View):
    """Interactive button controls for music player"""
    
    def __init__(self, bot, guild_id):
        super().__init__(timeout=None)  # Persistent buttons
        self.bot = bot
        self.guild_id = guild_id
    
    def get_player(self):
        """Get the music player for this guild"""
        cog = self.bot.get_cog('MusicCog')
        if cog:
            return cog.get_player(self.bot.get_guild(self.guild_id))
        return None
    
    @discord.ui.button(label="⏮️", style=discord.ButtonStyle.secondary, custom_id="previous", row=0)
    async def previous_button(self, interaction: discord.Interaction, button: Button):
        player = self.get_player()
        if not player:
            await interaction.response.send_message("❌ No player found!", ephemeral=True)
            return

        # history[0] is the current song, so "previous" is history[1]
        history = list(player.history)
        previous = None
        if player.current and history and history[0].url == player.current.url:
            previous = history[1] if len(history) > 1 else None
        elif history:
            previous = history[0]

        if not previous:
            await interaction.response.send_message("❌ No previous song in history!", ephemeral=True)
            return

        voice_client = interaction.guild.voice_client
        if not voice_client or not voice_client.is_connected():
            await interaction.response.send_message("❌ Not connected to voice!", ephemeral=True)
            return

        # Requeue: previous first, then the interrupted current song
        if player.current:
            player.queue.appendleft(player.current)
        player.queue.appendleft(previous)
        player.preloaded_sources.clear()
        player.loop = False

        await interaction.response.send_message(f"⏮️ Going back to **{previous.title}**")
        if voice_client.is_playing() or voice_client.is_paused():
            voice_client.stop()  # after_playing advances to the requeued previous song
        else:
            player.last_message_channel = interaction.channel
            await player.play_next()
    
    @discord.ui.button(label="⏯️", style=discord.ButtonStyle.primary, custom_id="pause_resume", row=0)
    async def pause_resume_button(self, interaction: discord.Interaction, button: Button):
        voice_client = interaction.guild.voice_client
        if not voice_client:
            await interaction.response.send_message("❌ Not connected to voice!", ephemeral=True)
            return

        player = self.get_player()
        
        if voice_client.is_playing():
            if player:
                player.mark_paused()
            voice_client.pause()
            await interaction.response.send_message("⏸️ Paused!", ephemeral=True)
        elif voice_client.is_paused():
            if player:
                player.mark_resumed()
            voice_client.resume()
            await interaction.response.send_message("▶️ Resumed!", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Nothing is playing!", ephemeral=True)
    
    @discord.ui.button(label="⏭️", style=discord.ButtonStyle.secondary, custom_id="skip", row=0)
    async def skip_button(self, interaction: discord.Interaction, button: Button):
        voice_client = interaction.guild.voice_client
        if not voice_client or not voice_client.is_playing():
            await interaction.response.send_message("❌ Nothing is playing!", ephemeral=True)
            return

        cog = self.bot.get_cog('MusicCog')
        if cog:
            should_skip, vote_message = cog.evaluate_skip_vote(interaction.user, interaction.guild)
            if not should_skip:
                await interaction.response.send_message(vote_message, ephemeral=vote_message.startswith("❌"))
                return

        player = self.get_player()
        if player:
            player.loop = False

        await interaction.response.defer()
        voice_client.stop()
        await asyncio.sleep(0.5)
        
        if player and player.current:
            cog = self.bot.get_cog('MusicCog')
            embed = cog.create_now_playing_embed(player.current)
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send("⏭️ Skipped! No more songs.")
    
    @discord.ui.button(label="🔊", style=discord.ButtonStyle.secondary, custom_id="volume_up", row=1)
    async def volume_up_button(self, interaction: discord.Interaction, button: Button):
        player = self.get_player()
        if not player:
            await interaction.response.send_message("❌ No player found!", ephemeral=True)
            return
        
        new_volume = min(player.volume + 0.1, 1.0)
        player.volume = new_volume
        
        voice_client = interaction.guild.voice_client
        if voice_client and voice_client.source:
            voice_client.source.volume = new_volume
        
        await interaction.response.send_message(f"🔊 Volume: {int(new_volume * 100)}%", ephemeral=True)
    
    @discord.ui.button(label="🔉", style=discord.ButtonStyle.secondary, custom_id="volume_down", row=1)
    async def volume_down_button(self, interaction: discord.Interaction, button: Button):
        player = self.get_player()
        if not player:
            await interaction.response.send_message("❌ No player found!", ephemeral=True)
            return
        
        new_volume = max(player.volume - 0.1, 0.0)
        player.volume = new_volume
        
        voice_client = interaction.guild.voice_client
        if voice_client and voice_client.source:
            voice_client.source.volume = new_volume
        
        await interaction.response.send_message(f"🔉 Volume: {int(new_volume * 100)}%", ephemeral=True)
    
    @discord.ui.button(label="🔀", style=discord.ButtonStyle.secondary, custom_id="shuffle", row=2)
    async def shuffle_button(self, interaction: discord.Interaction, button: Button):
        import random
        player = self.get_player()
        if not player:
            await interaction.response.send_message("❌ No player found!", ephemeral=True)
            return
        
        if len(player.queue) < 2:
            await interaction.response.send_message("❌ Not enough songs to shuffle!", ephemeral=True)
            return
        
        queue_list = list(player.queue)
        random.shuffle(queue_list)
        player.queue = deque(queue_list)
        player.preloaded_sources.clear()  # Clear preloaded cache since queue order changed
        
        # Preload the new next song
        asyncio.create_task(player.preload_next_song())
        
        await interaction.response.send_message("🔀 Queue shuffled!", ephemeral=True)
    
    @discord.ui.button(label="✕", style=discord.ButtonStyle.danger, custom_id="stop", row=0)
    async def stop_button(self, interaction: discord.Interaction, button: Button):
        player = self.get_player()
        if player:
            player.queue.clear()
            player.current = None
            player.loop = False
            player.loop_queue = False
            player.pending_playlist = None
            player.preloaded_sources.clear()  # Clear preloaded cache
        
        voice_client = interaction.guild.voice_client
        if voice_client:
            voice_client.stop()
        
        await interaction.response.send_message("⏹️ Stopped!", ephemeral=True)
    
    @discord.ui.button(label="❤️", style=discord.ButtonStyle.secondary, custom_id="favorite", row=1)
    async def favorite_button(self, interaction: discord.Interaction, button: Button):
        player = self.get_player()
        if not player or not player.current:
            await interaction.response.send_message("❌ Nothing is playing!", ephemeral=True)
            return

        cog = self.bot.get_cog('MusicCog')
        if not cog:
            await interaction.response.send_message("❌ No player found!", ephemeral=True)
            return

        added, title = cog.toggle_favorite(interaction.user.id, player.current)
        if added:
            await interaction.response.send_message(f"❤️ Added **{title}** to your favorites! See them with `/favorites list`", ephemeral=True)
        else:
            await interaction.response.send_message(f"💔 Removed **{title}** from your favorites.", ephemeral=True)

    @discord.ui.button(label="🎤", style=discord.ButtonStyle.secondary, custom_id="lyrics", row=1)
    async def lyrics_button(self, interaction: discord.Interaction, button: Button):
        player = self.get_player()
        cog = self.bot.get_cog('MusicCog')
        if not player or not player.current or not cog:
            await interaction.response.send_message("❌ Nothing is playing!", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        query = cog._clean_lyrics_query(player.current.title)
        lyrics_data = await cog._fetch_synced_lyrics(query)
        if not lyrics_data or not lyrics_data.get('lines'):
            await interaction.followup.send(
                f"🎤 No synced lyrics found for **{player.current.title}**.", ephemeral=True
            )
            return
        text = "\n".join(line for _, line in lyrics_data['lines'] if line.strip())
        embed = discord.Embed(
            title=f"🎤 {lyrics_data.get('track') or player.current.title}",
            description=text[:3900] + ("\n…" if len(text) > 3900 else ""),
            color=discord.Color.from_rgb(124, 92, 255),
        )
        embed.set_footer(text=lyrics_data.get('artist') or "Synced lyrics")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="📜", style=discord.ButtonStyle.secondary, custom_id="queue", row=1)
    async def queue_button(self, interaction: discord.Interaction, button: Button):
        player = self.get_player()
        if not player:
            await interaction.response.send_message("❌ No player found!", ephemeral=True)
            return
        
        if not player.current and not player.queue:
            await interaction.response.send_message("📭 Queue is empty!", ephemeral=True)
            return
        
        embed = discord.Embed(title="🎶 Music Queue", color=discord.Color.blurple())
        
        if player.current:
            embed.add_field(
                name="Now Playing",
                value=f"**{player.current.title}** [{player.current.duration}]",
                inline=False
            )

        embed.add_field(
            name="Summary",
            value=f"{1 if player.current else 0} playing • {len(player.queue)} upcoming",
            inline=False
        )
        
        if player.queue:
            queue_list = []
            for i, song in enumerate(list(player.queue)[:10], 1):
                queue_list.append(f"`{i}.` **{song.title}** [{song.duration}]")
            
            if len(player.queue) > 10:
                queue_list.append(f"\n*...and {len(player.queue) - 10} more*")
            
            embed.add_field(name="Up Next", value="\n".join(queue_list), inline=False)
        
        status = []
        if player.loop:
            status.append("🔂 Loop: Song")
        if player.loop_queue:
            status.append("🔁 Loop: Queue")
        if status:
            embed.set_footer(text=" | ".join(status))
        
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="🔁", style=discord.ButtonStyle.secondary, custom_id="loop_cycle", row=2)
    async def loop_button(self, interaction: discord.Interaction, button: Button):
        player = self.get_player()
        if not player:
            await interaction.response.send_message("❌ No player found!", ephemeral=True)
            return
        if not player.loop and not player.loop_queue:
            player.loop, label = True, "song"
        elif player.loop:
            player.loop = False
            player.loop_queue, label = True, "queue"
        else:
            player.loop_queue, label = False, "off"
        await interaction.response.send_message(f"🔁 Loop: **{label}**", ephemeral=True)

    @discord.ui.button(label="✨", style=discord.ButtonStyle.secondary, custom_id="smart_autoplay", row=2)
    async def autoplay_button(self, interaction: discord.Interaction, button: Button):
        player = self.get_player()
        if not player:
            await interaction.response.send_message("❌ No player found!", ephemeral=True)
            return
        player.autoplay = not player.autoplay
        state = "on — artist, album and genre radio" if player.autoplay else "off"
        await interaction.response.send_message(f"✨ Smart Autoplay **{state}**", ephemeral=True)

    @discord.ui.button(label="🎛️", style=discord.ButtonStyle.secondary, custom_id="automix_toggle", row=2)
    async def automix_button(self, interaction: discord.Interaction, button: Button):
        player = self.get_player()
        if not player:
            await interaction.response.send_message("❌ No player found!", ephemeral=True)
            return
        player.automix_enabled = not player.automix_enabled
        player.preloaded_sources.clear()
        if player.automix_enabled:
            player.schedule_automix()
        else:
            player.cancel_automix()
        await interaction.response.send_message(
            f"🎛️ AutoMix **{'on' if player.automix_enabled else 'off'}**", ephemeral=True
        )


class HistoryReplaySelect(discord.ui.Select):
    def __init__(self, cog, guild_id: int, songs: list[Song]):
        self.cog = cog
        self.guild_id = guild_id
        self.songs = songs
        options = [
            discord.SelectOption(
                label=f"{index}. {song.title}"[:100],
                value=str(index - 1),
                description=f"{song.artist or 'Recently played'} • {song.duration}"[:100],
                emoji="➕",
            )
            for index, song in enumerate(songs, 1)
        ]
        super().__init__(placeholder="Add a recent track to the queue…", options=options)

    async def callback(self, interaction: discord.Interaction):
        song = self.songs[int(self.values[0])]
        player = self.cog.get_player(interaction.guild)
        player.queue.append(song)
        player.last_message_channel = interaction.channel
        vc = interaction.guild.voice_client
        started = bool(vc and vc.is_connected() and not vc.is_playing() and not vc.is_paused())
        await interaction.response.send_message(
            f"➕ Added **{song.title}** to the queue.", ephemeral=True
        )
        if started:
            await player.play_next()


class HistoryReplayView(View):
    def __init__(self, cog, guild_id: int, songs: list[Song]):
        super().__init__(timeout=180)
        self.add_item(HistoryReplaySelect(cog, guild_id, songs))


class ServerInviteSelect(discord.ui.Select):
    def __init__(self, cog, guild_options: list[discord.SelectOption]):
        super().__init__(placeholder="hmmm", min_values=1, max_values=1, options=guild_options)
        self.cog = cog

    async def callback(self, interaction: discord.Interaction):
        guild_id = int(self.values[0])
        guild = self.cog.bot.get_guild(guild_id)

        if not guild:
            await interaction.response.send_message("❌ That server is no longer available.")
            return

        try:
            invite = await self.cog.create_server_invite(guild)
        except Exception as e:
            logger.warning(f"Invite creation failed for {guild.name}: {e}")
            await interaction.response.send_message(f"❌ I could not create an invite for **{guild.name}**.")
            return

        if not invite:
            await interaction.response.send_message(f"❌ No inviteable channel was found in **{guild.name}**.")
            return

        self.disabled = True
        await interaction.response.edit_message(
            content=f"Invite for **{guild.name}**: {invite.url}",
            view=self.view,
        )


class ServerInviteView(View):
    def __init__(self, cog, guild_options: list[discord.SelectOption]):
        super().__init__(timeout=300)
        self.add_item(ServerInviteSelect(cog, guild_options))


class SendMessageChannelSelect(discord.ui.Select):
    def __init__(self, cog, guild: discord.Guild, message_text: str, channel_options: list[discord.SelectOption]):
        super().__init__(placeholder="Select a channel...", min_values=1, max_values=1, options=channel_options)
        self.cog = cog
        self.guild = guild
        self.message_text = message_text

    async def callback(self, interaction: discord.Interaction):
        channel_id = int(self.values[0])
        channel = self.guild.get_channel(channel_id)

        if not channel:
            await interaction.response.edit_message(content=f"❌ That channel is no longer available in **{self.guild.name}**.", view=self.view)
            return

        try:
            await channel.send(self.message_text)
        except Exception as e:
            logger.warning(f"Failed to send message to {self.guild.name}#{getattr(channel, 'name', channel_id)}: {e}")
            await interaction.response.edit_message(
                content=f"❌ I could not send the message to **#{channel.name}** in **{self.guild.name}**.",
                view=self.view,
            )
            return

        for item in self.view.children:
            item.disabled = True

        await interaction.response.edit_message(
            content=f"✅ Sent your message to **#{channel.name}** in **{self.guild.name}**.",
            view=self.view,
        )


class SendMessageChannelView(View):
    def __init__(self, cog, guild: discord.Guild, message_text: str, channel_options: list[discord.SelectOption]):
        super().__init__(timeout=300)
        self.add_item(SendMessageChannelSelect(cog, guild, message_text, channel_options))


class SendMessageGuildSelect(discord.ui.Select):
    def __init__(self, cog, message_text: str, guild_options: list[discord.SelectOption]):
        super().__init__(placeholder="Select a server...", min_values=1, max_values=1, options=guild_options)
        self.cog = cog
        self.message_text = message_text

    async def callback(self, interaction: discord.Interaction):
        guild_id = int(self.values[0])
        guild = self.cog.bot.get_guild(guild_id)

        if not guild:
            await interaction.response.edit_message(content="❌ That server is no longer available.", view=self.view)
            return

        channel_options = []
        for channel in self.cog._get_sendmessage_channels(guild)[:25]:
            channel_options.append(
                discord.SelectOption(
                    label=channel.name[:100],
                    value=str(channel.id),
                    description=f"#{channel.name}",
                )
            )

        if not channel_options:
            await interaction.response.edit_message(
                content=f"❌ I could not find any channel I can send messages to in **{guild.name}**.",
                view=self.view,
            )
            return

        self.disabled = True
        await interaction.response.edit_message(
            content=f"Select a channel in **{guild.name}** to send your message.",
            view=SendMessageChannelView(self.cog, guild, self.message_text, channel_options),
        )


class SendMessageGuildView(View):
    def __init__(self, cog, message_text: str, guild_options: list[discord.SelectOption]):
        super().__init__(timeout=300)
        self.add_item(SendMessageGuildSelect(cog, message_text, guild_options))


class SearchResultSelect(discord.ui.Select):
    def __init__(self, cog, entries, requester):
        self.cog = cog
        self.entries = entries
        self.requester = requester

        options = []
        for i, entry in enumerate(entries):
            title = (entry.get('title') or 'Unknown')[:95]
            uploader = entry.get('uploader') or entry.get('channel') or ''
            duration = cog.format_duration(entry.get('duration') or 0)
            description = f"{duration} • {uploader}"[:100]
            options.append(discord.SelectOption(label=title, value=str(i), description=description))

        super().__init__(placeholder="Pick a song to queue…", options=options)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message("❌ Only the person who searched can pick!", ephemeral=True)
            return

        entry = self.entries[int(self.values[0])]
        await interaction.response.defer()

        if not await self.cog.ensure_voice(interaction):
            return

        video_id = entry.get('id')
        url = entry.get('url') or entry.get('webpage_url') or (f"https://www.youtube.com/watch?v={video_id}" if video_id else None)
        if not url:
            await interaction.followup.send("❌ Couldn't resolve that result, sorry!", ephemeral=True)
            return

        player = self.cog.get_player(interaction.guild)
        player.last_message_channel = interaction.channel

        song = Song(
            title=entry.get('title') or 'Unknown',
            url=url,
            duration=self.cog.format_duration(entry.get('duration') or 0),
            requester=interaction.user,
            source_type='youtube',
            thumbnail=(entry.get('thumbnails') or [{}])[-1].get('url') if entry.get('thumbnails') else None
        )
        player.queue.append(song)

        # Disable the picker so it can't be used twice
        self.disabled = True
        try:
            await interaction.edit_original_response(view=self.view)
        except Exception:
            pass

        vc = interaction.guild.voice_client
        if vc and not vc.is_playing() and not vc.is_paused():
            await player.play_next()
            await interaction.followup.send(f"▶️ Playing **{song.title}**!")
        else:
            await interaction.followup.send(f"🎵 Added **{song.title}** to queue (position {len(player.queue)})")


class SearchResultView(View):
    def __init__(self, cog, entries, requester):
        super().__init__(timeout=120)
        self.add_item(SearchResultSelect(cog, entries, requester))


class MusicPlayer:
    """Music player for a guild"""
    
    def __init__(self, bot, guild):
        self.bot = bot
        self.guild = guild
        self.queue = deque()
        self.current: Optional[Song] = None
        self.volume = 0.5
        self.loop = False
        self.loop_queue = False
        self.pending_playlist = None  # For just-in-time playlist loading
        self.preloaded_sources = {}  # Cache for pre-downloaded audio sources
        self._preload_task = None  # Background preload task
        self._play_next_running = False
        self.is_247_mode = False  # 24/7 mode flag
        self.twentyfourseven_songs = []  # Cached 24/7 playlist songs
        self.song_started_at: Optional[float] = None
        self.paused_started_at: Optional[float] = None
        self.total_paused_seconds = 0.0
        self.current_song_key: Optional[str] = None
        self.last_message_channel: Optional[discord.TextChannel] = None  # For sending now playing updates
        self.idle_disconnect_task: Optional[asyncio.Task] = None
        self.nowplaying_message: Optional[discord.Message] = None  # Last auto-posted now-playing message
        self._suppress_after = False  # Set during /seek so after_playing doesn't advance the queue
        self.autoplay = False  # Keep playing random library songs when the queue runs out
        self.history = deque(maxlen=25)  # Recently played songs, newest first
        self.audio_filter: Optional[str] = None  # Active /filter preset name
        self.crossfade_seconds = 0  # Fade in/out duration between songs (0 = off)
        self.skip_votes = set()  # User IDs that voted to skip the current song
        self.karaoke_mode = False  # Vocal removal + live lyrics on every song
        self.automix_enabled = False  # DJ-style overlapping, beat-matched transitions
        self.automix_blend_seconds = AUTOMIX_DEFAULT_BLEND_SECONDS
        self._automix_task: Optional[asyncio.Task] = None  # Per-song transition watcher
        self._automix_speed = 1.0  # atempo AutoMix applied to the current song
        self._automix_file_offset = 0.0  # File position where the current source started

    def build_source_options(self, song: Optional[Song] = None, start_seconds: int = 0) -> str:
        """FFmpeg output options for this guild's active filter/crossfade settings."""
        duration_seconds = parse_duration_to_seconds(song.duration) if song else None
        # AutoMix blends live; a baked-in afade would fight the mixer
        crossfade = 0 if self.automix_enabled else self.crossfade_seconds
        return build_audio_options(
            filter_name=self.audio_filter,
            crossfade_seconds=crossfade,
            duration_seconds=duration_seconds,
            start_seconds=start_seconds,
        )

    def reset_playback_clock(self):
        self.song_started_at = None
        self.paused_started_at = None
        self.total_paused_seconds = 0.0
        self.current_song_key = None

    def cancel_idle_disconnect(self):
        task = self.idle_disconnect_task
        if task and not task.done():
            task.cancel()
        self.idle_disconnect_task = None

    async def _idle_disconnect_after_timeout(self):
        try:
            await asyncio.sleep(IDLE_DISCONNECT_SECONDS)

            voice_client = self.guild.voice_client
            if not voice_client or not voice_client.is_connected():
                return
            if voice_client.is_playing() or voice_client.is_paused():
                return
            if self.current or self.queue or self.pending_playlist or self.is_247_mode:
                return

            logger.info(f"Idle timeout reached in {self.guild.name}, disconnecting after {IDLE_DISCONNECT_SECONDS}s of inactivity")
            self.reset_playback_clock()
            await voice_client.disconnect(force=False)
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.warning(f"Idle disconnect task failed for {self.guild.name}: {e}")
        finally:
            if self.idle_disconnect_task is asyncio.current_task():
                self.idle_disconnect_task = None

    def schedule_idle_disconnect(self):
        if self.is_247_mode:
            return

        voice_client = self.guild.voice_client
        if not voice_client or not voice_client.is_connected():
            return
        if voice_client.is_playing() or voice_client.is_paused():
            return
        if self.queue or self.pending_playlist:
            return
        if self.idle_disconnect_task and not self.idle_disconnect_task.done():
            return

        self.idle_disconnect_task = asyncio.create_task(self._idle_disconnect_after_timeout())

    def mark_paused(self):
        if self.song_started_at and self.paused_started_at is None:
            self.paused_started_at = time.monotonic()

    def mark_resumed(self):
        if self.song_started_at and self.paused_started_at is not None:
            self.total_paused_seconds += max(0.0, time.monotonic() - self.paused_started_at)
            self.paused_started_at = None

    def get_playback_position_seconds(self) -> float:
        if not self.song_started_at:
            return 0.0

        now = time.monotonic()
        paused_so_far = self.total_paused_seconds
        if self.paused_started_at is not None:
            paused_so_far += max(0.0, now - self.paused_started_at)

        return max(0.0, now - self.song_started_at - paused_so_far)

    async def preload_next_song(self):
        """Preload the next song in queue while current is playing"""
        if not self.queue:
            return
        
        # Get the next song without removing it
        next_song = self.queue[0]
        song_key = f"{next_song.url}_{id(next_song)}"
        
        # Skip if already preloaded
        if song_key in self.preloaded_sources:
            return
        
        try:
            print(f"🔄 Preloading: {next_song.title}")
            
            if next_song.source_type == 'local':
                # Local files don't need preloading
                return
            elif next_song.source_type == 'spotify' and next_song.url.startswith('spotify:search:'):
                # Spotify song - search on YouTube first
                search_query = next_song.url.replace('spotify:search:', '')
                cog = self.bot.get_cog('MusicCog')
                if cog:
                    yt_song = await cog.process_youtube(search_query, next_song.requester)
                    if yt_song:
                        # Update the song in queue
                        next_song.url = yt_song.url
                        next_song.title = yt_song.title
                        next_song.duration = yt_song.duration
                        next_song.thumbnail = yt_song.thumbnail
                        next_song.artist = yt_song.artist or next_song.artist
                        next_song.album = yt_song.album or next_song.album
                        next_song.genres = yt_song.genres or next_song.genres
                
                # Now preload the stream
                source = await YTDLSource.from_url(
                    next_song.url,
                    loop=self.bot.loop,
                    stream=True,
                    audio_options=self.build_source_options(next_song)
                )
                self.preloaded_sources[song_key] = source
                print(f"✅ Preloaded: {next_song.title}")
            else:
                # YouTube - preload stream
                source = await YTDLSource.from_url(
                    next_song.url,
                    loop=self.bot.loop,
                    stream=True,
                    audio_options=self.build_source_options(next_song)
                )
                self.preloaded_sources[song_key] = source
                print(f"✅ Preloaded: {next_song.title}")
                
        except Exception as e:
            print(f"Preload error: {e}")

    # ---------- AutoMix ----------

    def _resolve_local_file(self, song: Optional[Song]) -> Optional[str]:
        """Local audio file for a song, when one exists (upload or download cache)."""
        if not song:
            return None
        if song.source_type == 'local':
            return song.url if os.path.exists(song.url) else None
        video_id = extract_youtube_video_id(song.url)
        return find_cached_song(video_id) if video_id else None

    def cancel_automix(self):
        task = self._automix_task
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()
        self._automix_task = None

    def schedule_automix(self):
        """(Re)start the transition watcher for the song that is playing now."""
        self.cancel_automix()
        if self.automix_enabled:
            self._automix_task = asyncio.create_task(self._automix_watcher())

    async def _automix_top_up_queue(self):
        """Refill an empty queue early (playlist/24-7/autoplay) so AutoMix has a next song."""
        try:
            if not self.queue and self.pending_playlist:
                await self.load_next_from_playlist()
            if not self.queue and self.is_247_mode and self.twentyfourseven_songs:
                try:
                    random.shuffle(self.twentyfourseven_songs)
                except Exception:
                    pass
                for song in self.twentyfourseven_songs:
                    self.queue.append(song)
            if not self.queue and self.autoplay and not self.is_247_mode:
                pick = await self._pick_autoplay_song()
                if pick:
                    self.queue.append(pick)
        except Exception as e:
            logger.debug(f"AutoMix queue top-up failed: {e}")

    async def _automix_watcher(self):
        """Watch the current song and hand playback to AutoMix near its end.

        Analyzes the playing file and the next queued file in the background
        (silence trim + tempo estimate), then calls play_next() with an
        AutoMixPlan right when the blend should start.
        """
        try:
            song = self.current
            song_key = self.current_song_key
            if not song or not self.automix_enabled or self.loop:
                return

            await asyncio.sleep(2.0)  # let playback settle and preloading start

            out_path = self._resolve_local_file(song)
            if not out_path:
                return  # can't know a stream's true ending; play it out normally
            out_info = await self.bot.loop.run_in_executor(None, analyze_track_edges, out_path)
            if not out_info or self.current is not song or self.current_song_key != song_key:
                return

            # Position math is in file time: the source may have started mid-file
            # (-ss) and speed filters/atempo consume the file faster than wall time
            speed = FILTER_SPEED_FACTORS.get(self.audio_filter, 1.0) * self._automix_speed
            blend = float(self.automix_blend_seconds)
            effective_blend = min(blend, 6.0)
            end_at = out_info['end_at']
            if end_at - self._automix_file_offset < AUTOMIX_MIN_TRACK_SECONDS:
                return  # too short to be worth blending out of
            trigger_at = end_at - effective_blend * speed - 0.2

            in_info = None
            in_path = None
            analyzed_song = None
            preload_kicked = False

            while True:
                if (self.current is not song or self.current_song_key != song_key
                        or not self.automix_enabled or self.loop):
                    return
                voice_client = self.guild.voice_client
                if not voice_client or not voice_client.is_connected():
                    return
                if voice_client.is_paused():
                    await asyncio.sleep(0.5)
                    continue
                if not voice_client.is_playing():
                    return

                position = self.get_playback_position_seconds()
                file_pos = self._automix_file_offset + max(0.0, position - self._automix_file_offset) * speed
                remaining = (trigger_at - file_pos) / speed

                # Line up the next song early so there is something to blend into
                if remaining < 45:
                    if not self.queue:
                        await self._automix_top_up_queue()
                        if self.current is not song or self.current_song_key != song_key:
                            return
                    next_song = self.queue[0] if self.queue else None
                    if (next_song and not preload_kicked
                            and not self._resolve_local_file(next_song)):
                        preload_kicked = True
                        asyncio.create_task(self.preload_next_song())

                # Analyze the upcoming song once its file lands in the cache
                next_song = self.queue[0] if self.queue else None
                if next_song is not None and next_song is not analyzed_song and remaining > 6:
                    path = self._resolve_local_file(next_song)
                    if path:
                        info = await self.bot.loop.run_in_executor(None, analyze_track_edges, path)
                        analyzed_song = next_song
                        in_path = path
                        in_info = info
                        # Use the longer requested blend only when both tracks
                        # have compatible, trustworthy tempos. Otherwise the
                        # shorter transition avoids a long vocal-on-vocal wash.
                        if info and out_info.get('bpm_tail') and info.get('bpm_head'):
                            ratio = _tempo_match_ratio(out_info['bpm_tail'], info['bpm_head'])
                            if ratio:
                                effective_blend = blend
                                trigger_at = end_at - effective_blend * speed - 0.2
                        continue  # analysis took time; recompute the position first

                if remaining <= 0.05:
                    break
                await asyncio.sleep(min(1.0, max(0.05, remaining - 0.03)))

            next_song = self.queue[0] if self.queue else None
            if next_song is None:
                return

            # A long blind overlap can sound muddy when tempo analysis is not
            # available. Keep the user's full blend for confidently matched
            # tracks and use a shorter, cleaner handoff otherwise.
            plan = AutoMixPlan(song=next_song, fade_seconds=effective_blend)
            if in_info and analyzed_song is next_song and in_path:
                plan.file_path = in_path
                plan.start_seconds = in_info['start_at']
                plan.bpm_out = out_info.get('bpm_tail')
                plan.bpm_in = in_info.get('bpm_head')
                if plan.bpm_out and plan.bpm_in:
                    ratio = _tempo_match_ratio(plan.bpm_out, plan.bpm_in)
                    if ratio:
                        plan.atempo = ratio
            await self.play_next(automix_plan=plan)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"AutoMix watcher error: {e}", exc_info=True)

    async def play_next(self, automix_plan: Optional[AutoMixPlan] = None):
        """Play the next song in queue"""
        if self._play_next_running:
            logger.info("play_next already running; skipping duplicate call")
            return

        self._play_next_running = True
        try:
            if self.loop and self.current:
                self.queue.appendleft(self.current)
            elif self.loop_queue and self.current:
                self.queue.append(self.current)

            # Load next song from pending playlist if queue is empty
            if not self.queue and self.pending_playlist:
                await self.load_next_from_playlist()

            # 24/7 mode: reload playlist if queue is empty
            if not self.queue and self.is_247_mode and self.twentyfourseven_songs:
                logger.info("🔄 24/7 mode: Reloading playlist...")
                # Shuffle each time we reload to keep playback varied
                try:
                    random.shuffle(self.twentyfourseven_songs)
                except Exception:
                    pass
                for song in self.twentyfourseven_songs:
                    self.queue.append(song)

            # Autoplay: keep the music going with a random song from the local library
            if not self.queue and self.autoplay and not self.is_247_mode:
                song = await self._pick_autoplay_song()
                if song:
                    logger.info(f"🎶 Smart autoplay picked: {song.title}")
                    self.queue.append(song)

            if not self.queue:
                self.current = None
                self.preloaded_sources.clear()  # Clear preload cache
                self.cancel_automix()
                self.reset_playback_clock()
                self.schedule_idle_disconnect()
                logger.info("Queue is empty, nothing to play")
                return

            self.cancel_idle_disconnect()
            self.current = self.queue.popleft()
            song_key = f"{self.current.url}_{id(self.current)}"

            # Safety check
            if not self.current:
                logger.error("ERROR: Popped None from queue!")
                await self.play_next()
                return

            # New song: previous skip votes no longer apply
            self.skip_votes.clear()

            # Record in history (skip immediate repeats from /loop)
            if not self.history or self.history[0].url != self.current.url:
                self.current.played_at = int(time.time())
                self.history.appendleft(self.current)

            # Record in persistent listening stats (skip bot-requested plays
            # so 24/7 mode and autoplay don't drown out real requests)
            requester = self.current.requester
            if requester is not None and not getattr(requester, 'bot', False):
                cog = self.bot.get_cog('MusicCog')
                if cog:
                    cog.log_play(self.guild.id, self.current, getattr(self.last_message_channel, 'id', None))

            # Load next song in background while current plays
            if self.pending_playlist:
                asyncio.create_task(self.load_next_from_playlist())

            # Prefetch lyrics in background
            if self.current:
                cog = self.bot.get_cog('MusicCog')
                if cog:
                    asyncio.create_task(cog._fetch_synced_lyrics(self.current.title))

            logger.info(f"Playing from queue: {self.current.title} (type: {self.current.source_type})")

            voice_client = self.guild.voice_client
            if not voice_client:
                logger.error("❌ No voice client! Bot is not connected to a voice channel.")
                return

            if not voice_client.is_connected():
                logger.error("❌ Voice client exists but is not connected!")
                return

            try:
                # AutoMix handoff: the previous song is still playing and the
                # watcher asked us to blend this one in on top of it
                handoff = None
                if (automix_plan and automix_plan.song is self.current
                        and voice_client.is_playing() and voice_client.source is not None):
                    handoff = automix_plan

                if handoff and handoff.file_path:
                    # Open the analyzed local file directly: skip lead-in
                    # silence and stretch tempo to beat-match the outgoing song
                    extra_filters = []
                    if abs(handoff.atempo - 1.0) >= 0.003:
                        extra_filters.append(f'atempo={handoff.atempo:.4f}')
                    before = f'-ss {handoff.start_seconds:.2f}' if handoff.start_seconds > 0.05 else None
                    raw = discord.FFmpegPCMAudio(
                        handoff.file_path,
                        before_options=before,
                        options=build_audio_options(filter_name=self.audio_filter,
                                                    extra_filters=extra_filters),
                    )
                    source = discord.PCMVolumeTransformer(raw, volume=self.volume)
                    stale = self.preloaded_sources.pop(song_key, None)  # built for a normal start
                    if stale:
                        try:
                            stale.cleanup()
                        except Exception:
                            pass
                # Check if we have a preloaded source
                elif song_key in self.preloaded_sources:
                    print(f"🚀 Using preloaded source for: {self.current.title}")
                    source = self.preloaded_sources.pop(song_key)
                    source.volume = self.volume
                elif self.current.source_type == 'local':
                    # Local file - direct FFmpeg
                    source = discord.FFmpegPCMAudio(self.current.url, options=self.build_source_options(self.current))
                    source = discord.PCMVolumeTransformer(source, volume=self.volume)
                elif self.current.source_type == 'spotify' and self.current.url.startswith('spotify:search:'):
                    # Spotify song needs to be searched on YouTube first
                    search_query = self.current.url.replace('spotify:search:', '')
                    print(f"🔍 Searching YouTube for: {search_query}")

                    # Get the cog to use process_youtube
                    cog = self.bot.get_cog('MusicCog')
                    if cog:
                        yt_song = await cog.process_youtube(search_query, self.current.requester)
                        if yt_song:
                            # Update current song with actual YouTube URL
                            self.current.url = yt_song.url
                            self.current.title = yt_song.title
                            self.current.duration = yt_song.duration
                            self.current.thumbnail = yt_song.thumbnail
                            self.current.artist = yt_song.artist or self.current.artist
                            self.current.album = yt_song.album or self.current.album
                            self.current.genres = yt_song.genres or self.current.genres

                    # Now get the stream
                    source = await asyncio.wait_for(
                        YTDLSource.from_url(
                            self.current.url,
                            loop=self.bot.loop,
                            stream=True,
                            audio_options=self.build_source_options(self.current)
                        ),
                        timeout=SONG_LOAD_TIMEOUT_SECONDS
                    )
                    source.volume = self.volume
                else:
                    # YouTube/Spotify - use yt-dlp to get stream
                    source = await asyncio.wait_for(
                        YTDLSource.from_url(
                            self.current.url,
                            loop=self.bot.loop,
                            stream=True,
                            audio_options=self.build_source_options(self.current)
                        ),
                        timeout=SONG_LOAD_TIMEOUT_SECONDS
                    )
                    source.volume = self.volume

                def after_playing(error):
                    if self._suppress_after:
                        # A /seek is restarting the same song; don't advance the queue
                        return
                    if error:
                        logger.error(f"Player error: {error}", exc_info=error)
                    else:
                        logger.info("Song finished playing normally")
                    coro = self.play_next()
                    fut = asyncio.run_coroutine_threadsafe(coro, self.bot.loop)
                    try:
                        fut.result()
                    except Exception as e:
                        logger.error(f"Error in play_next: {e}", exc_info=True)

                started_at_offset = 0.0
                self._automix_speed = 1.0
                if handoff:
                    transition = AutoMixTransition(voice_client.source, source, handoff.fade_seconds)
                    swapped = False
                    try:
                        voice_client.source = transition
                        swapped = voice_client.is_playing()
                    except Exception as e:
                        logger.debug(f"AutoMix source swap failed: {e}")
                    if swapped:
                        if handoff.file_path:
                            started_at_offset = handoff.start_seconds
                            self._automix_speed = handoff.atempo
                        if handoff.bpm_out and handoff.bpm_in and abs(handoff.atempo - 1.0) >= 0.003:
                            logger.info(
                                f"🎧 AutoMix: beat-matched blend into {self.current.title} "
                                f"({handoff.bpm_in:.0f}→{handoff.bpm_in * handoff.atempo:.0f} BPM "
                                f"to match {handoff.bpm_out:.0f})")
                        else:
                            logger.info(f"🎧 AutoMix: blending into {self.current.title}")
                    else:
                        # The old song ended in the race window; start normally
                        voice_client.play(transition.active_source(), after=after_playing)
                else:
                    voice_client.play(source, after=after_playing)
                self.song_started_at = time.monotonic() - started_at_offset
                self._automix_file_offset = started_at_offset
                self.paused_started_at = None
                self.total_paused_seconds = 0.0
                self.current_song_key = song_key
                logger.info(f"▶️ Now playing: {self.current.title}")

                # Send now playing embed with lyrics to the last message channel
                if self.last_message_channel:
                    cog = self.bot.get_cog('MusicCog')
                    if cog:
                        asyncio.create_task(cog._send_now_playing_update(self))

                # Start preloading the next song in background
                if self.queue:
                    asyncio.create_task(self.preload_next_song())

                # Watch for the end of this song to blend the next one in
                self.schedule_automix()

            except Exception as e:
                print(f"Error playing song: {e}")
                import traceback
                traceback.print_exc()
                if "Already playing audio" not in str(e):
                    # Tell the channel instead of failing silently
                    if self.last_message_channel and self.current:
                        reason = "it took too long to load" if isinstance(e, asyncio.TimeoutError) else "it couldn't be loaded"
                        skipped_title = self.current.title
                        async def notify_skip():
                            try:
                                await self.last_message_channel.send(f"⚠️ Skipping **{skipped_title}** - {reason}.")
                            except Exception:
                                pass
                        asyncio.create_task(notify_skip())
                    asyncio.create_task(self.play_next())
        finally:
            self._play_next_running = False
    
    async def _pick_autoplay_song(self) -> Optional[Song]:
        """Pick a context-aware follow-up, falling back to the local library."""
        cog = self.bot.get_cog('MusicCog')
        if cog:
            has_artist_context = bool(cog._song_artist(self.current))
            try:
                recommendation = await cog.pick_autoplay_recommendation(self)
                if recommendation:
                    return recommendation
            except Exception as e:
                logger.debug(f"Smart autoplay recommendation failed: {e}")
            # If we know the artist, silence is preferable to an unrelated
            # random jump. The local random fallback is only for old/unknown
            # files that carry no usable music context.
            if has_artist_context:
                return None
        return self._pick_random_library_song()

    def _pick_random_library_song(self) -> Optional[Song]:
        """Pick a random downloaded song from the musics folder for autoplay."""
        try:
            # Bias toward songs people favorited so autoplay feels curated
            cog = self.bot.get_cog('MusicCog')
            if cog and random.random() < 0.4:
                favorite = cog.pick_random_favorite(self.guild, exclude_urls={s.url for s in self.history})
                if favorite:
                    logger.info(f"🎲 Autoplay picked a favorite: {favorite.title}")
                    return favorite

            files = glob.glob(os.path.join(MUSICS_FOLDER, '**', '*.mp3'), recursive=True)
            if not files:
                return None
            # Avoid repeating what was just played when the library is big enough
            recent_urls = {song.url for song in self.history}
            fresh = [f for f in files if f not in recent_urls]
            path = random.choice(fresh or files)
            title = os.path.splitext(os.path.basename(path))[0]
            title = re.sub(r'\s*-?\s*\[[0-9A-Za-z_-]{11}\]$', '', title).strip()
            return Song(
                title=title or 'Unknown',
                url=path,
                duration="Unknown",
                requester=self.guild.me,
                source_type='local'
            )
        except Exception as e:
            logger.warning(f"Autoplay pick failed: {e}")
            return None

    async def seek_to(self, position_seconds: int) -> bool:
        """Restart the current song at the given position. Returns True on success.

        Only works when the audio exists as a local file (cached download or
        uploaded file) - proxy streams can't be re-opened reliably.
        """
        if not self.current:
            return False

        voice_client = self.guild.voice_client
        if not voice_client or not voice_client.is_connected():
            return False

        # Locate the audio file for the current song
        if self.current.source_type == 'local':
            file_path = self.current.url if os.path.exists(self.current.url) else None
        else:
            file_path = find_cached_song(extract_youtube_video_id(self.current.url))

        if not file_path:
            return False

        self._suppress_after = True
        try:
            if voice_client.is_playing() or voice_client.is_paused():
                voice_client.stop()
                # Give the player thread a moment to fire its (suppressed) callback
                await asyncio.sleep(0.5)

            source = discord.FFmpegPCMAudio(
                file_path,
                before_options=f'-ss {max(0, position_seconds)}',
                options=self.build_source_options(self.current, start_seconds=max(0, position_seconds))
            )
            source = discord.PCMVolumeTransformer(source, volume=self.volume)

            def after_seek(error):
                if self._suppress_after:
                    return
                if error:
                    logger.error(f"Player error after seek: {error}", exc_info=error)
                else:
                    logger.info("Song finished playing normally (after seek)")
                fut = asyncio.run_coroutine_threadsafe(self.play_next(), self.bot.loop)
                try:
                    fut.result()
                except Exception as e:
                    logger.error(f"Error in play_next after seek: {e}", exc_info=True)

            voice_client.play(source, after=after_seek)

            # Rewind the playback clock so the progress bar matches the new position
            self.song_started_at = time.monotonic() - max(0, position_seconds)
            self.paused_started_at = None
            self.total_paused_seconds = 0.0
            # The rebuilt source has no AutoMix atempo and starts at the seek point
            self._automix_speed = 1.0
            self._automix_file_offset = max(0, position_seconds)
            self.schedule_automix()
            return True
        except Exception as e:
            logger.error(f"Seek failed: {e}", exc_info=True)
            return False
        finally:
            self._suppress_after = False

    async def load_next_from_playlist(self):
        """Load the next song from pending playlist"""
        if not self.pending_playlist:
            return
        
        try:
            playlist = self.pending_playlist
            idx = playlist['current_index']
            
            # Check if Spotify or YouTube
            if playlist.get('is_spotify'):
                tracks = playlist['tracks']
                if idx >= len(tracks):
                    # No more tracks
                    self.pending_playlist = None
                    return
                
                item = tracks[idx]
                if playlist['is_album']:
                    track, artist_name = item
                    search_query = f"{track['name']} {artist_name}"
                else:
                    track = item.get('track')
                    if not track or not track.get('name'):
                        playlist['current_index'] += 1
                        return
                    search_query = f"{track['name']} {track['artists'][0]['name']}"
                
                print(f"🔍 Loading next Spotify song: {search_query}")
                cog = self.bot.get_cog('MusicCog')
                if cog:
                    song = await cog.process_youtube(search_query, playlist['requester'])
                    if song:
                        song.source_type = 'spotify'
                        self.queue.append(song)
                        print(f"  ✅ Added: {song.title}")
            
            else:
                # YouTube playlist
                entries = playlist['entries']
                if idx >= len(entries):
                    # No more entries
                    self.pending_playlist = None
                    return
                
                entry = entries[idx]
                if not entry:
                    playlist['current_index'] += 1
                    return
                
                video_id = entry.get('id')
                title = entry.get('title', 'Unknown')
                duration = entry.get('duration', 0)
                video_url = entry.get('webpage_url') or entry.get('url') or f"https://www.youtube.com/watch?v={video_id}"
                
                # Import to get format_duration
                cog = self.bot.get_cog('MusicCog')
                duration_str = cog.format_duration(duration) if cog else "Unknown"
                
                song = Song(
                    title=title,
                    url=video_url,
                    duration=duration_str,
                    requester=playlist['requester'],
                    source_type='youtube',
                    thumbnail=entry.get('thumbnail')
                )
                self.queue.append(song)
                print(f"🔍 Loading next YouTube song: {title}")
            
            # Increment index for next time
            playlist['current_index'] += 1
            
        except Exception as e:
            print(f"Error loading next playlist song: {e}")
            import traceback
            traceback.print_exc()


class FakeInteractionResponse:
    def __init__(self, ctx):
        self.ctx = ctx
        self._is_done = False
    
    def is_done(self):
        return self._is_done
        
    async def send_message(self, content=None, embed=None, ephemeral=False):
        self._is_done = True
        if content or embed:
            await self.ctx.send(content=content, embed=embed)
        
    async def defer(self, ephemeral=False):
        self._is_done = True
        await self.ctx.typing()

class FakeFollowup:
    def __init__(self, ctx):
        self.ctx = ctx
        
    async def send(self, content=None, embed=None, ephemeral=False):
        await self.ctx.send(content=content, embed=embed)

class FakeInteraction:
    def __init__(self, ctx):
        self.ctx = ctx
        self.user = ctx.author
        self.guild = ctx.guild
        self.channel = ctx.channel
        self.response = FakeInteractionResponse(ctx)
        self.followup = FakeFollowup(ctx)


class MusicCog(commands.Cog):
    """Music commands cog"""
    
    def __init__(self, bot):
        self.bot = bot
        self.players = {}
        self.lyricsnow_tasks = {}
        self.nowplaying_tasks = {}
        self._state_saver_task = None
        self._wrapped_task = None
        self._state_restored = False
        self._last_saved_state = None
        self._autoplay_station_cache = {}
    
    def get_player(self, guild) -> MusicPlayer:
        if guild.id not in self.players:
            self.players[guild.id] = MusicPlayer(self.bot, guild)
        return self.players[guild.id]

    @staticmethod
    def _artist_key(value: Optional[str]) -> str:
        """Normalize artist/channel names for recommendation matching."""
        value = (value or '').lower()
        value = re.sub(r'(?:official|vevo|topic)(?:\s+(?:music|artist|channel))*$', '', value.strip())
        value = re.sub(r'\b(official|vevo|topic|music)\b', ' ', value)
        return re.sub(r'[^a-z0-9\u00c0-\u024f]+', '', value)

    def _song_artist(self, song: Optional[Song]) -> Optional[str]:
        if not song:
            return None
        if song.artist and self._artist_key(song.artist):
            return re.sub(
                r'\s*(?:[-–|]\s*)?(?:topic|official|vevo)(?:\s+(?:music|artist|channel))*\s*$',
                '', song.artist, flags=re.IGNORECASE
            ).strip()
        if song.source_type == 'local' and song.url:
            parent = os.path.basename(os.path.dirname(song.url))
            if parent and parent not in {os.path.basename(MUSICS_FOLDER), os.path.basename(DOWNLOADS_FOLDER)}:
                return parent
        # Most music uploads use "Artist - Track". This is deliberately only
        # a last resort because metadata and artist folders are more reliable.
        parts = re.split(r'\s+[-–—|]\s+', song.title or '', maxsplit=1)
        return parts[0].strip() if len(parts) == 2 else None

    async def _autoplay_station(self, artist: str) -> list[dict]:
        """Build an artist radio from Deezer's public catalogue, cached for 6h."""
        key = self._artist_key(artist)
        cached = self._autoplay_station_cache.get(key)
        if cached and cached[0] > time.time():
            return cached[1]

        timeout = aiohttp.ClientTimeout(total=10)
        station = []
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    f"https://api.deezer.com/search/artist?q={quote(artist)}&limit=8"
                ) as response:
                    if response.status != 200:
                        return []
                    matches = (await response.json()).get('data') or []
                if not matches:
                    return []
                seed = min(matches, key=lambda item: (
                    self._artist_key(item.get('name')) != key,
                    -int(item.get('nb_fan') or 0),
                ))

                async with session.get(
                    f"https://api.deezer.com/artist/{seed['id']}/related?limit=8"
                ) as response:
                    related = (await response.json()).get('data') or [] if response.status == 200 else []

                # Favor the current artist while still making roughly half of
                # the station genuinely adjacent artists.
                artists = [seed] * 2 + related[:6]
                unique_artists = {item['id']: item for item in artists}.values()

                async def top_tracks(item):
                    try:
                        async with session.get(
                            f"https://api.deezer.com/artist/{item['id']}/top?limit=12"
                        ) as response:
                            data = (await response.json()).get('data') or [] if response.status == 200 else []
                        return [{
                            'title': track.get('title_short') or track.get('title'),
                            'artist': (track.get('artist') or {}).get('name') or item.get('name'),
                            'album': (track.get('album') or {}).get('title'),
                            'related': item['id'] != seed['id'],
                        } for track in data]
                    except Exception:
                        return []

                groups = await asyncio.gather(*(top_tracks(item) for item in unique_artists))
                station = [track for group in groups for track in group if track.get('title')]
        except Exception as e:
            logger.debug(f"Could not build autoplay station for {artist}: {e}")

        self._autoplay_station_cache[key] = (
            time.time() + (6 * 3600 if station else 5 * 60), station
        )
        return station

    async def pick_autoplay_recommendation(self, player: MusicPlayer) -> Optional[Song]:
        """Choose the next track by artist/album affinity instead of at random."""
        seed = player.current or (player.history[0] if player.history else None)
        artist = self._song_artist(seed)
        if not artist:
            return None

        station = await self._autoplay_station(artist)
        seed_key = self._artist_key(artist)
        related_keys = {self._artist_key(item.get('artist')) for item in station}
        recent_urls = {song.url for song in player.history}
        recent_titles = {
            re.sub(r'\W+', '', (song.title or '').lower()) for song in player.history
        }

        # Reuse relevant downloads first: same artist/album wins, followed by
        # artists from the related-artist station. Unrelated library tracks are
        # intentionally not candidates here.
        local_candidates = []
        roots = {MUSICS_FOLDER, DOWNLOADS_FOLDER}
        for root in roots:
            for path in glob.glob(os.path.join(root, '**', '*.mp3'), recursive=True):
                if path in recent_urls:
                    continue
                folder_artist = os.path.basename(os.path.dirname(path))
                candidate_key = self._artist_key(folder_artist)
                score = 0
                if candidate_key == seed_key:
                    score = 100
                elif candidate_key in related_keys:
                    score = 65
                if seed and seed.album and self._artist_key(seed.album) in self._artist_key(path):
                    score += 35
                if score:
                    local_candidates.append((score, path, folder_artist))

        if local_candidates:
            local_candidates.sort(key=lambda item: item[0], reverse=True)
            score, path, folder_artist = random.choice(local_candidates[:10])
            title = os.path.splitext(os.path.basename(path))[0]
            title = re.sub(r'\s*-?\s*\[[0-9A-Za-z_-]{11}\]$', '', title).strip()
            logger.info(f"🎶 Autoplay local affinity {score}: {folder_artist} - {title}")
            return Song(title=title or 'Unknown', url=path, duration='Unknown',
                        requester=player.guild.me, source_type='local', artist=folder_artist)

        fresh = [item for item in station
                 if re.sub(r'\W+', '', item['title'].lower()) not in recent_titles]
        if not fresh:
            # Catalogue outages still get a same-artist fallback; never jump
            # to a random artist just because related-artist lookup failed.
            for suffix in ('official audio', 'deep cut official audio'):
                song = await self.process_youtube(f"{artist} {suffix}", player.guild.me)
                if song and song.url not in recent_urls:
                    song.artist = artist
                    return song
            return None
        # 60/40 current-vs-related keeps an artist radio coherent without
        # getting stuck playing an entire discography.
        same_artist = [item for item in fresh if self._artist_key(item.get('artist')) == seed_key]
        related = [item for item in fresh if self._artist_key(item.get('artist')) != seed_key]
        album_key = self._artist_key(seed.album) if seed and seed.album else ''
        same_album = [item for item in fresh
                      if album_key and self._artist_key(item.get('album')) == album_key]
        if same_album and random.random() < 0.35:
            pool = same_album
        else:
            pool = same_artist if same_artist and (not related or random.random() < 0.60) else related
        pick = random.choice(pool or fresh)
        song = await self.process_youtube(
            f"{pick['artist']} - {pick['title']} official audio", player.guild.me
        )
        if song:
            song.artist = pick['artist']
            song.album = pick.get('album')
            logger.info(f"🎶 Autoplay radio: {pick['artist']} - {pick['title']}")
        return song

    def _get_invite_channel(self, guild: discord.Guild):
        bot_member = guild.get_member(self.bot.user.id) if self.bot.user else None
        if not bot_member:
            bot_member = guild.me

        if not bot_member:
            return None

        channel_candidates = []
        if guild.system_channel:
            channel_candidates.append(guild.system_channel)
        channel_candidates.extend(guild.text_channels)
        channel_candidates.extend(guild.voice_channels)
        channel_candidates.extend(getattr(guild, 'stage_channels', []))

        seen_channel_ids = set()
        for channel in channel_candidates:
            if not channel or channel.id in seen_channel_ids:
                continue
            seen_channel_ids.add(channel.id)

            permissions = channel.permissions_for(bot_member)
            if permissions.view_channel and permissions.create_instant_invite:
                return channel

        return None

    async def create_server_invite(self, guild: discord.Guild):
        channel = self._get_invite_channel(guild)
        if not channel:
            return None

        return await channel.create_invite(
            max_age=0,
            max_uses=0,
            unique=True,
            reason="Requested via /serverinvite",
        )

    def _build_invite_view(self) -> tuple[Optional[ServerInviteView], int, int]:
        inviteable_guilds = []

        for guild in sorted(self.bot.guilds, key=lambda item: item.name.lower()):
            if self._get_invite_channel(guild):
                inviteable_guilds.append(guild)

        if not inviteable_guilds:
            return None, 0, 0

        options = []
        for guild in inviteable_guilds[:25]:
            label = guild.name[:100]
            member_count = guild.member_count if guild.member_count is not None else "?"
            options.append(
                discord.SelectOption(
                    label=label,
                    value=str(guild.id),
                    description=f"{member_count} members",
                )
            )

        return ServerInviteView(self, options), len(options), len(inviteable_guilds)

    def _get_sendmessage_channels(self, guild: discord.Guild):
        bot_member = guild.get_member(self.bot.user.id) if self.bot.user else None
        if not bot_member:
            bot_member = guild.me

        if not bot_member:
            return []

        channel_candidates = []
        channel_candidates.extend(guild.text_channels)

        seen_channel_ids = set()
        sendable_channels = []
        for channel in channel_candidates:
            if not channel or channel.id in seen_channel_ids:
                continue
            seen_channel_ids.add(channel.id)

            permissions = channel.permissions_for(bot_member)
            if permissions.view_channel and permissions.send_messages:
                sendable_channels.append(channel)

        return sendable_channels

    def _build_sendmessage_view(self, message_text: str) -> tuple[Optional[SendMessageGuildView], int, int]:
        sendable_guilds = []

        for guild in sorted(self.bot.guilds, key=lambda item: item.name.lower()):
            if self._get_sendmessage_channels(guild):
                sendable_guilds.append(guild)

        if not sendable_guilds:
            return None, 0, 0

        options = []
        for guild in sendable_guilds[:25]:
            label = guild.name[:100]
            channel_count = len(self._get_sendmessage_channels(guild))
            options.append(
                discord.SelectOption(
                    label=label,
                    value=str(guild.id),
                    description=f"{channel_count} sendable channels",
                )
            )

        return SendMessageGuildView(self, message_text, options), len(options), len(sendable_guilds)
    
    async def ensure_voice(self, interaction: discord.Interaction) -> bool:
        async def send_error(msg):
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)

        if not interaction.user.voice:
            await send_error("❌ You need to be in a voice channel!")
            return False
        
        # Check bot permissions in the voice channel
        channel = interaction.user.voice.channel
        bot_member = interaction.guild.me
        permissions = channel.permissions_for(bot_member)
        
        logger.info(f"Voice channel permissions - Connect: {permissions.connect}, Speak: {permissions.speak}, Use VAD: {permissions.use_voice_activation}")
        
        if not permissions.connect:
            logger.error("Bot lacks CONNECT permission in voice channel!")
            await send_error("❌ I don't have permission to connect to that voice channel!")
            return False
        
        if not permissions.speak:
            logger.error("Bot lacks SPEAK permission in voice channel!")
            await send_error("❌ I don't have permission to speak in that voice channel!")
            return False
        
        try:
            if not interaction.guild.voice_client:
                logger.info(f"Connecting to voice channel: {channel.name}")
                # Connect without self_deaf first - simpler
                await channel.connect(timeout=20.0, reconnect=True)
                logger.info(f"✅ Successfully connected to {channel.name}")
                # Small delay to ensure connection is stable
                await asyncio.sleep(1)
            elif interaction.guild.voice_client.channel != channel:
                logger.info(f"Moving to voice channel: {channel.name}")
                await interaction.guild.voice_client.move_to(channel)
                logger.info(f"✅ Successfully moved to {channel.name}")
        except asyncio.TimeoutError:
            logger.error("Failed to connect to voice channel: Connection timeout")
            await send_error("❌ Connection timeout!")
            return False
        except discord.errors.ConnectionClosed as e:
            if e.code == 4017:
                logger.error("Error 4017: Server session invalid")
                logger.error("This may be caused by:")
                logger.error("1. Discord server voice region issues - try changing server region")
                logger.error("2. Discord.py version incompatibility - try: pip install -U discord.py")
                logger.error("3. Bot token might need to be regenerated in Developer Portal")
            else:
                logger.error(f"Voice connection closed with code {e.code}: {e}", exc_info=True)
            return False
        except Exception as e:
            logger.error(f"Failed to connect to voice channel: {e}", exc_info=True)
            await send_error("❌ Failed to connect to voice channel!")
            return False
        
        return True

    @app_commands.command(name="play", description="Play a song from YouTube, Spotify, or upload a local file")
    @app_commands.describe(
        query="YouTube/Spotify URL or search query",
        file="Upload an audio file (mp3, wav, ogg, flac, etc.)"
    )
    async def play(self, interaction: discord.Interaction, query: str, file: discord.Attachment = None):
        await interaction.response.defer()
        if not await self.ensure_voice(interaction):
            return
        
        player = self.get_player(interaction.guild)
        player.last_message_channel = interaction.channel  # Store channel for now playing updates
        
        try:
            songs_added = []
            
            # Local file upload
            if file:
                ext = os.path.splitext(file.filename)[1].lower()
                if ext not in config.SUPPORTED_FORMATS:
                    await interaction.followup.send(f" Unsupported format. Supported: {', '.join(config.SUPPORTED_FORMATS)}")
                    return
                
                temp_dir = tempfile.gettempdir()
                filepath = os.path.join(temp_dir, f"discord_music_{interaction.guild.id}_{file.filename}")
                
                async with aiohttp.ClientSession() as session:
                    async with session.get(file.url) as resp:
                        with open(filepath, 'wb') as f:
                            f.write(await resp.read())
                
                song = Song(
                    title=file.filename,
                    url=filepath,
                    duration="Unknown",
                    requester=interaction.user,
                    source_type='local'
                )
                songs_added.append(song)
            
            # Spotify URL
            elif query and ('spotify.com' in query or 'spotify:' in query):
                # Check if it's a playlist or album (these need API credentials)
                if 'playlist' in query or 'album' in query:
                    if not SPOTIFY_AVAILABLE:
                        await interaction.followup.send(" Spotify playlists need API credentials. Please add SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET to your .env file. Single track links work without them.")
                        return
                    all_songs, total_count = await self.process_spotify_playlist_fast(query, interaction.user)
                    songs_added = all_songs
                    if not songs_added:
                        await interaction.followup.send("❌ Couldn't load that Spotify playlist. Note: Spotify blocks API access to its own editorial/algorithmic playlists (Daily Mix, Discover Weekly, etc.) - user-created playlists work fine.")
                        return
                else:
                    # Single track (works even without API credentials via oEmbed)
                    songs_added = await self.process_spotify(query, interaction.user)
            
            # YouTube URL or search
            elif query:
                print(f"Processing query: {query}")
                # Check if it's a playlist URL
                if 'list=' in query:
                    print("Detected playlist URL")
                    # Load ALL songs to queue immediately (metadata only), download on-demand
                    all_songs, total_count = await self.process_youtube_playlist_fast(query, interaction.user)
                    songs_added = all_songs
                else:
                    song = await self.process_youtube(query, interaction.user)
                    print(f"Got song: {song}")
                    if song:
                        songs_added.append(song)
                    else:
                        print("Song was None!")
            
            else:
                await interaction.followup.send(" Please provide a URL, search query, or upload a file!")
                return
            
            if not songs_added:
                await interaction.followup.send("❌ No songs found! If this was a valid link, YouTube may be temporarily blocking lookups - try again in a moment.")
                return
            
            # Add to queue
            for song in songs_added:
                player.queue.append(song)
            
            # Start playing if not already
            vc = interaction.guild.voice_client
            if vc and not vc.is_playing() and not vc.is_paused():
                if player._play_next_running:
                    # Another song is still downloading; this one is queued behind it
                    await interaction.followup.send("🎵 Added to queue - the current song is still loading, it'll play in order!")
                else:
                    await player.play_next()
                    if player.current:
                        await interaction.followup.send("▶️ Started playing!", ephemeral=True)
                    else:
                        await interaction.followup.send("❌ Failed to play the song.")
            else:
                if len(songs_added) == 1:
                    embed = discord.Embed(
                        title="🎵 Added to Queue",
                        description=f"**{songs_added[0].title}**",
                        color=discord.Color.green()
                    )
                    embed.add_field(name="Position", value=str(len(player.queue)))
                    embed.add_field(
                        name="Queue Status",
                        value=f"{1 if player.current else 0} playing • {len(player.queue)} upcoming",
                        inline=False
                    )
                else:
                    embed = discord.Embed(
                        title="🎵 Added to Queue",
                        description=f"Added **{len(songs_added)}** songs",
                        color=discord.Color.green()
                    )
                    # Check if there's a background task loading more
                    if hasattr(player, '_loading_playlist') and player._loading_playlist:
                        embed.set_footer(text="⏳ Loading more songs in background...")
                await interaction.followup.send(embed=embed)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {str(e)}")
            print(f"Play error: {e}")
            import traceback
            traceback.print_exc()

    @play.autocomplete('query')
    async def play_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        suggestions = get_autocomplete_suggestions(current)
        return [
            app_commands.Choice(name=name[:100], value=name[:100])
            for name in suggestions
        ]

    @app_commands.command(name="playnext", description="Play a song next in queue")
    @app_commands.describe(
        query="YouTube/Spotify URL or search query",
        file="Upload an audio file (mp3, wav, ogg, flac, etc.)"
    )
    async def playnext(self, interaction: discord.Interaction, query: str, file: discord.Attachment = None):
        await interaction.response.defer()
        if not await self.ensure_voice(interaction):
            return

        player = self.get_player(interaction.guild)
        player.last_message_channel = interaction.channel

        try:
            songs_added = []

            if file:
                ext = os.path.splitext(file.filename)[1].lower()
                if ext not in config.SUPPORTED_FORMATS:
                    await interaction.followup.send(f" Unsupported format. Supported: {', '.join(config.SUPPORTED_FORMATS)}")
                    return

                temp_dir = tempfile.gettempdir()
                filepath = os.path.join(temp_dir, f"discord_music_{interaction.guild.id}_{file.filename}")

                async with aiohttp.ClientSession() as session:
                    async with session.get(file.url) as resp:
                        with open(filepath, 'wb') as f:
                            f.write(await resp.read())

                song = Song(
                    title=file.filename,
                    url=filepath,
                    duration="Unknown",
                    requester=interaction.user,
                    source_type='local'
                )
                songs_added.append(song)

            elif query and ('spotify.com' in query or 'spotify:' in query):
                if 'playlist' in query or 'album' in query:
                    if not SPOTIFY_AVAILABLE:
                        await interaction.followup.send(" Spotify playlists need API credentials. Please add SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET to your .env file. Single track links work without them.")
                        return
                    all_songs, total_count = await self.process_spotify_playlist_fast(query, interaction.user)
                    songs_added = all_songs
                else:
                    songs_added = await self.process_spotify(query, interaction.user)

            elif query:
                print(f"Processing playnext query: {query}")
                if 'list=' in query:
                    print("Detected playlist URL for playnext")
                    all_songs, total_count = await self.process_youtube_playlist_fast(query, interaction.user)
                    songs_added = all_songs
                else:
                    song = await self.process_youtube(query, interaction.user)
                    print(f"Got playnext song: {song}")
                    if song:
                        songs_added.append(song)
                    else:
                        print("Playnext song was None!")
            else:
                await interaction.followup.send(" Please provide a URL, search query, or upload a file!")
                return

            if not songs_added:
                await interaction.followup.send("❌ No songs found! If this was a valid link, YouTube may be temporarily blocking lookups - try again in a moment.")
                return

            for song in reversed(songs_added):
                player.queue.appendleft(song)

            vc = interaction.guild.voice_client
            if vc and not vc.is_playing() and not vc.is_paused():
                if player._play_next_running:
                    # Another song is still downloading; this one plays right after it
                    await interaction.followup.send("🎵 Queued next - the current song is still loading, it'll play right after!")
                else:
                    await player.play_next()
                    if player.current:
                        await interaction.followup.send("▶️ Started playing!", ephemeral=True)
                    else:
                        await interaction.followup.send("❌ Failed to play the song.")
            else:
                if len(songs_added) == 1:
                    embed = discord.Embed(
                        title="⏭️ Added to Play Next",
                        description=f"**{songs_added[0].title}**",
                        color=discord.Color.green()
                    )
                    embed.add_field(name="Position", value="1", inline=True)
                    embed.add_field(
                        name="Queue Status",
                        value=f"{1 if player.current else 0} playing • {len(player.queue)} upcoming",
                        inline=False
                    )
                else:
                    embed = discord.Embed(
                        title="⏭️ Added to Play Next",
                        description=f"Added **{len(songs_added)}** songs",
                        color=discord.Color.green()
                    )
                    if hasattr(player, '_loading_playlist') and player._loading_playlist:
                        embed.set_footer(text="⏳ Loading more songs in background...")
                await interaction.followup.send(embed=embed)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {str(e)}")
            print(f"Playnext error: {e}")
            import traceback
            traceback.print_exc()

    @playnext.autocomplete('query')
    async def playnext_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        suggestions = get_autocomplete_suggestions(current)
        return [
            app_commands.Choice(name=name[:100], value=name[:100])
            for name in suggestions
        ]

    def _pick_search_entry(self, query: str, entries: list) -> Optional[dict]:
        """Pick the best search result, avoiding accidental TV episodes / 10-hour loops.

        Keeps the top result unless it's suspiciously long and the query didn't
        ask for long content, in which case the first reasonably-sized result
        from the top 5 wins.
        """
        entries = [e for e in entries if e]
        if not entries:
            return None

        first = entries[0]
        first_duration = first.get('duration') or 0
        if first_duration <= LONG_RESULT_SECONDS:
            return first

        query_lower = query.lower()
        if any(keyword in query_lower for keyword in LONG_INTENT_KEYWORDS):
            return first

        for entry in entries[1:]:
            duration = entry.get('duration') or 0
            if 0 < duration <= LONG_RESULT_SECONDS:
                logger.info(
                    f"Search guard: '{query}' top result was {first_duration}s "
                    f"({first.get('title')!r}), picked {entry.get('title')!r} instead"
                )
                return entry

        return first

    async def process_youtube(self, query: str, requester: discord.Member) -> Optional[Song]:
        """Process YouTube URL or search query - just get metadata"""
        loop = asyncio.get_event_loop()

        is_url = query.startswith('http')
        direct_video_id = extract_youtube_video_id(query) if is_url else None
        if direct_video_id:
            # Canonical watch URL: drops list=/si=/index= junk that breaks lookups
            query = normalize_youtube_url(query)
        # Top-5 search so the guard below can dodge absurdly long top results
        search_query = query if is_url else f"ytsearch5:{query}"

        last_error = None
        for attempt in range(3):
            try:
                print(f"Searching for: {query} (attempt {attempt + 1}/3)")
                data = await loop.run_in_executor(
                    None,
                    lambda: ytdl_search.extract_info(search_query, download=False)
                )

                if data and 'entries' in data:
                    if is_url:
                        data = data['entries'][0] if data['entries'] else None
                    else:
                        data = self._pick_search_entry(query, data['entries'] or [])

                if data:
                    title = data.get('title', 'Unknown')
                    url = data.get('webpage_url') or data.get('original_url') or data.get('url') or query
                    raw_genres = data.get('genres') or data.get('categories') or []
                    if isinstance(raw_genres, str):
                        raw_genres = [raw_genres]
                    artist_name = data.get('artist') or data.get('uploader') or data.get('channel')
                    if artist_name:
                        artist_name = re.sub(
                            r'\s*(?:[-–|]\s*)?(?:topic|official|vevo)(?:\s+(?:music|artist|channel))*\s*$',
                            '', artist_name, flags=re.IGNORECASE
                        ).strip()
                    print(f"Found: {title} -> {url}")
                    return Song(
                        title=title,
                        url=url,
                        duration=self.format_duration(data.get('duration', 0)),
                        requester=requester,
                        source_type='youtube',
                        thumbnail=data.get('thumbnail'),
                        artist=artist_name,
                        album=data.get('album'),
                        genres=tuple(str(genre) for genre in raw_genres[:8])
                    )

                print("No results from yt-dlp")
            except Exception as e:
                last_error = e
                error_str = str(e).lower()
                print(f"YouTube processing error (attempt {attempt + 1}): {e}")
                # Permanent failures - no point retrying
                if 'unavailable' in error_str or 'private video' in error_str or 'removed' in error_str:
                    break
            if attempt < 2:
                await asyncio.sleep(1.5 * (attempt + 1))

        # Direct video link but metadata lookup kept failing: resolve the title via
        # oEmbed so the link still queues; the download stage has its own fallbacks.
        if direct_video_id:
            oembed = await fetch_oembed_info(query)
            if oembed:
                logger.info(f"Resolved via oEmbed fallback: {oembed['title']}")
                return Song(
                    title=oembed['title'],
                    url=query,
                    duration="Unknown",
                    requester=requester,
                    source_type='youtube',
                    thumbnail=oembed.get('thumbnail')
                )

        if last_error:
            logger.error(f"process_youtube gave up on '{query}': {last_error}")
        return None
    
    async def process_youtube_playlist_fast(self, url: str, requester: discord.Member) -> tuple[list[Song], int]:
        """Add all playlist songs to queue with metadata only - download happens on-demand"""
        loop = asyncio.get_event_loop()
        songs = []
        total_count = 0

        # YouTube Mix/Radio "playlists" (list=RD...) are auto-generated and can't be
        # extracted as playlists - play the linked video instead.
        video_id = extract_youtube_video_id(url)
        if video_id and ('list=RD' in url or 'start_radio' in url):
            print("Mix/Radio link detected - playing the single video")
            song = await self.process_youtube(url, requester)
            if song:
                songs.append(song)
            return songs, len(songs)

        try:
            print(f"Loading playlist metadata: {url}")
            data = await loop.run_in_executor(
                None,
                lambda: ytdl_playlist.extract_info(url, download=False)
            )

            if not data:
                raise Exception("Playlist extraction returned no data")

            if 'entries' not in data:
                song = await self.process_youtube(url, requester)
                if song:
                    songs.append(song)
                return songs, 1
            
            entries = data.get('entries', [])
            total_count = len(entries)
            playlist_title = data.get('title', 'Playlist')
            print(f"Found playlist: {playlist_title} with {total_count} videos")
            
            # Add all songs to queue (metadata only, up to 50)
            for entry in entries[:50]:
                if not entry:
                    continue
                
                try:
                    video_id = entry.get('id')
                    title = entry.get('title', 'Unknown')
                    duration = entry.get('duration', 0)
                    video_url = entry.get('webpage_url') or entry.get('url') or f"https://www.youtube.com/watch?v={video_id}"
                    
                    song = Song(
                        title=title,
                        url=video_url,
                        duration=self.format_duration(duration),
                        requester=requester,
                        source_type='youtube',
                        thumbnail=entry.get('thumbnail')
                    )
                    songs.append(song)
                except Exception as e:
                    print(f"Error processing entry: {e}")
                    continue
            
            print(f"✅ Added {len(songs)} songs to queue (will download on-demand)")

        except Exception as e:
            print(f"Playlist processing error: {e}")

        # Playlist extraction failed but the link also points at a specific video:
        # queue that video rather than reporting nothing found.
        if not songs and video_id:
            print("Playlist empty/failed - falling back to the linked video")
            song = await self.process_youtube(url, requester)
            if song:
                songs.append(song)
                total_count = 1

        return songs, total_count
    
    async def process_spotify_playlist_fast(self, url: str, requester: discord.Member) -> tuple[list[Song], int]:
        """Add all Spotify playlist songs to queue with track names - search happens on-demand"""
        songs = []
        total_count = 0
        
        if not SPOTIFY_AVAILABLE:
            return songs, 0
        
        try:
            print(f"Loading Spotify playlist metadata: {url}")
            
            if 'playlist' in url:
                playlist = sp.playlist(url)
                total_count = playlist['tracks']['total']
                results = playlist['tracks']
                all_tracks = results['items']
                
                # Get all tracks
                while results['next']:
                    results = sp.next(results)
                    all_tracks.extend(results['items'])
                
                print(f"  Playlist: {playlist['name']} ({total_count} tracks)")
                
                # Add all tracks to queue (metadata only, up to 50)
                for item in all_tracks[:50]:
                    track = item.get('track')
                    if track and track.get('name'):
                        # Create a special Song object that will be searched later
                        search_query = f"{track['name']} {track['artists'][0]['name']}"
                        song = Song(
                            title=search_query,  # Store search query as title temporarily
                            url=f"spotify:search:{search_query}",  # Special URL marker
                            duration="Unknown",
                            requester=requester,
                            source_type='spotify',
                            thumbnail=track.get('album', {}).get('images', [{}])[0].get('url') if track.get('album') else None
                        )
                        songs.append(song)
            
            elif 'album' in url:
                album = sp.album(url)
                total_count = album['total_tracks']
                artist_name = album['artists'][0]['name']
                
                print(f"  Album: {album['name']} ({total_count} tracks)")
                
                # Add all tracks to queue (metadata only, up to 50)
                for track in album['tracks']['items'][:50]:
                    search_query = f"{track['name']} {artist_name}"
                    song = Song(
                        title=search_query,
                        url=f"spotify:search:{search_query}",
                        duration="Unknown",
                        requester=requester,
                        source_type='spotify',
                        thumbnail=album.get('images', [{}])[0].get('url') if album.get('images') else None
                    )
                    songs.append(song)
            
            print(f"✅ Added {len(songs)} songs to queue (will search on-demand)")
            
        except Exception as e:
            print(f"Spotify playlist processing error: {e}")
        
        return songs, total_count
    
    async def process_youtube_playlist_initial(self, url: str, requester: discord.Member) -> tuple[list[Song], int, list]:
        """Process first song of YouTube playlist only, store rest for later"""
        loop = asyncio.get_event_loop()
        songs = []
        total_count = 0
        all_entries = []
        
        try:
            print(f"Processing playlist (first song only): {url}")
            data = await loop.run_in_executor(
                None,
                lambda: ytdl_playlist.extract_info(url, download=False)
            )
            
            if not data:
                return songs, 0, []
            
            if 'entries' not in data:
                song = await self.process_youtube(url, requester)
                if song:
                    songs.append(song)
                return songs, 1, []
            
            all_entries = data.get('entries', [])
            total_count = len(all_entries)
            playlist_title = data.get('title', 'Playlist')
            print(f"Found playlist: {playlist_title} with {total_count} videos")
            
            # Process only first song
            first_entry = all_entries[0] if all_entries else None
            if first_entry:
                try:
                    video_id = first_entry.get('id')
                    title = first_entry.get('title', 'Unknown')
                    duration = first_entry.get('duration', 0)
                    video_url = first_entry.get('webpage_url') or first_entry.get('url') or f"https://www.youtube.com/watch?v={video_id}"
                    
                    song = Song(
                        title=title,
                        url=video_url,
                        duration=self.format_duration(duration),
                        requester=requester,
                        source_type='youtube',
                        thumbnail=first_entry.get('thumbnail')
                    )
                    songs.append(song)
                except Exception as e:
                    print(f"Error processing entry: {e}")
            
        except Exception as e:
            print(f"Playlist processing error: {e}")
        
        return songs, total_count, all_entries
    
    async def process_youtube_playlist(self, url: str, requester: discord.Member) -> list[Song]:
        """Process YouTube playlist and return list of songs"""
        loop = asyncio.get_event_loop()
        songs = []
        
        try:
            print(f"Processing playlist: {url}")
            data = await loop.run_in_executor(
                None,
                lambda: ytdl_playlist.extract_info(url, download=False)
            )
            
            if not data:
                print("No data returned from playlist")
                return songs
            
            # Check if it's a playlist
            if 'entries' not in data:
                # Single video, not a playlist
                song = await self.process_youtube(url, requester)
                if song:
                    songs.append(song)
                return songs
            
            # Process playlist entries
            entries = data.get('entries', [])
            playlist_title = data.get('title', 'Playlist')
            print(f"Found playlist: {playlist_title} with {len(entries)} videos")
            
            # Limit to first 50 videos to avoid spam
            for entry in entries[:50]:
                if not entry:
                    continue
                
                try:
                    # Extract video info
                    video_id = entry.get('id')
                    title = entry.get('title', 'Unknown')
                    duration = entry.get('duration', 0)
                    
                    # Construct URL
                    video_url = entry.get('webpage_url') or entry.get('url') or f"https://www.youtube.com/watch?v={video_id}"
                    
                    song = Song(
                        title=title,
                        url=video_url,
                        duration=self.format_duration(duration),
                        requester=requester,
                        source_type='youtube',
                        thumbnail=entry.get('thumbnail')
                    )
                    songs.append(song)
                    
                except Exception as e:
                    print(f"Error processing playlist entry: {e}")
                    continue
            
            print(f"Successfully processed {len(songs)} songs from playlist")
            
        except Exception as e:
            print(f"Playlist processing error: {e}")
            import traceback
            traceback.print_exc()
        
        return songs

    async def process_youtube_background(self, entries: list, requester: discord.Member, player: MusicPlayer):
        """Process remaining YouTube playlist entries in background (instant - uses pre-fetched data!)"""
        try:
            print(f"🔄 Processing {len(entries)} remaining songs in background...")
            
            for entry in entries:
                if not entry:
                    continue
                
                try:
                    video_id = entry.get('id')
                    title = entry.get('title', 'Unknown')
                    duration = entry.get('duration', 0)
                    video_url = entry.get('webpage_url') or entry.get('url') or f"https://www.youtube.com/watch?v={video_id}"
                    
                    song = Song(
                        title=title,
                        url=video_url,
                        duration=self.format_duration(duration),
                        requester=requester,
                        source_type='youtube',
                        thumbnail=entry.get('thumbnail')
                    )
                    player.queue.append(song)
                    print(f"  ✅ Added: {title}")
                except Exception as e:
                    print(f"Error processing entry: {e}")
                    continue
            
            print(f"✅ Finished loading playlist ({len(player.queue)} total songs in queue)")
            
        except Exception as e:
            print(f"Background playlist loading error: {e}")
        finally:
            player._loading_playlist = False

    async def process_playlist_background(self, url: str, requester: discord.Member, player: MusicPlayer, guild: discord.Guild, source: str, total_count: int):
        """Process remaining playlist songs in background"""
        try:
            print(f"🔄 Loading remaining songs from {source} playlist in background...")
            
            if source == 'youtube':
                loop = asyncio.get_event_loop()
                data = await loop.run_in_executor(
                    None,
                    lambda: ytdl_playlist.extract_info(url, download=False)
                )
                
                if data and 'entries' in data:
                    entries = data['entries'][3:50]  # Skip first 3, limit to 50 total
                    
                    for entry in entries:
                        if not entry:
                            continue
                        
                        try:
                            video_id = entry.get('id')
                            title = entry.get('title', 'Unknown')
                            duration = entry.get('duration', 0)
                            video_url = entry.get('webpage_url') or entry.get('url') or f"https://www.youtube.com/watch?v={video_id}"
                            
                            song = Song(
                                title=title,
                                url=video_url,
                                duration=self.format_duration(duration),
                                requester=requester,
                                source_type='youtube',
                                thumbnail=entry.get('thumbnail')
                            )
                            player.queue.append(song)
                            print(f"  ✅ Added: {title}")
                        except Exception as e:
                            print(f"Error processing entry: {e}")
                            continue
            
            elif source == 'spotify':
                # Get remaining tracks from Spotify
                if 'playlist' in url:
                    playlist = sp.playlist(url)
                    results = playlist['tracks']
                    tracks = results['items']
                    
                    while results['next']:
                        results = sp.next(results)
                        tracks.extend(results['items'])
                    
                    # Process tracks starting from index 3
                    for item in tracks[3:50]:
                        track = item.get('track')
                        if track and track.get('name'):
                            search_query = f"{track['name']} {track['artists'][0]['name']}"
                            print(f"  Searching: {search_query}")
                            song = await self.process_youtube(search_query, requester)
                            if song:
                                song.source_type = 'spotify'
                                player.queue.append(song)
                            await asyncio.sleep(0.2)
                
                elif 'album' in url:
                    album = sp.album(url)
                    artist_name = album['artists'][0]['name']
                    
                    for track in album['tracks']['items'][3:50]:
                        search_query = f"{track['name']} {artist_name}"
                        print(f"  Searching: {search_query}")
                        song = await self.process_youtube(search_query, requester)
                        if song:
                            song.source_type = 'spotify'
                            player.queue.append(song)
                        await asyncio.sleep(0.2)
            
            print(f"✅ Finished loading playlist ({len(player.queue)} total songs in queue)")
            
        except Exception as e:
            print(f"Background playlist loading error: {e}")
        finally:
            player._loading_playlist = False
    
    async def process_spotify_initial(self, url: str, requester: discord.Member) -> tuple[list[Song], int, list]:
        """Process first song of Spotify playlist/album only, store rest for later"""
        songs = []
        total_count = 0
        remaining_tracks = []
        
        if not SPOTIFY_AVAILABLE:
            return songs, 0, []
        
        try:
            print(f"Processing Spotify URL (first song only): {url}")
            
            if 'playlist' in url:
                playlist = sp.playlist(url)
                total_count = playlist['tracks']['total']
                results = playlist['tracks']
                all_tracks = results['items']
                
                # Get all tracks
                while results['next']:
                    results = sp.next(results)
                    all_tracks.extend(results['items'])
                
                remaining_tracks = all_tracks[1:50]  # Store all except first
                
                print(f"  Playlist: {playlist['name']} ({total_count} tracks)")
                
                # Process only first track
                if all_tracks:
                    track = all_tracks[0].get('track')
                    if track and track.get('name'):
                        search_query = f"{track['name']} {track['artists'][0]['name']}"
                        print(f"  Searching: {search_query}")
                        song = await self.process_youtube(search_query, requester)
                        if song:
                            song.source_type = 'spotify'
                            songs.append(song)
            
            elif 'album' in url:
                album = sp.album(url)
                total_count = album['total_tracks']
                artist_name = album['artists'][0]['name']
                all_tracks = album['tracks']['items']
                
                remaining_tracks = [(track, artist_name) for track in all_tracks[1:50]]  # Store all except first
                
                print(f"  Album: {album['name']} ({total_count} tracks)")
                
                # Process only first track
                if all_tracks:
                    track = all_tracks[0]
                    search_query = f"{track['name']} {artist_name}"
                    print(f"  Searching: {search_query}")
                    song = await self.process_youtube(search_query, requester)
                    if song:
                        song.source_type = 'spotify'
                        songs.append(song)
        
        except Exception as e:
            print(f"Spotify initial processing error: {e}")
        
        return songs, total_count, remaining_tracks

    async def process_spotify(self, url: str, requester: discord.Member) -> list[Song]:
        """Process Spotify single track (API first, oEmbed fallback)"""
        songs = []
        search_query = None

        if SPOTIFY_AVAILABLE and 'track' in url:
            try:
                print(f"Processing Spotify track: {url}")
                track = sp.track(url)
                search_query = f"{track['name']} {track['artists'][0]['name']}"
                print(f"  Track: {search_query}")
            except Exception as e:
                print(f"Spotify API error, trying oEmbed fallback: {e}")

        # Fallback: resolve title straight from the link - works without API
        # credentials and for links the API refuses (region-locked, editorial, etc.)
        if not search_query:
            oembed = await fetch_oembed_info(url)
            if oembed:
                search_query = oembed['title']
                if oembed.get('author'):
                    search_query = f"{search_query} {oembed['author']}"
                print(f"  Resolved via Spotify oEmbed: {search_query}")

        if not search_query:
            print(f"❌ Could not resolve Spotify link: {url}")
            return songs

        song = await self.process_youtube(search_query, requester)
        if song:
            song.source_type = 'spotify'
            songs.append(song)

        return songs
    
    def format_duration(self, seconds) -> str:
        if not seconds:
            return "Unknown"
        seconds = int(seconds)
        hours, remainder = divmod(seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{secs:02d}"
        return f"{minutes}:{secs:02d}"

    def _is_query_match(self, query: str, track_name: str) -> bool:
        if not query or not track_name:
            return False
            
        def normalize(text):
            text = text.lower()
            replacements = {
                'ı': 'i', 'ğ': 'g', 'ü': 'u', 'ş': 's', 'ö': 'o', 'ç': 'c',
                'â': 'a', 'î': 'i', 'û': 'u'
            }
            for k, v in replacements.items():
                text = text.replace(k, v)
            text = re.sub(r'[^a-z0-9\s]', '', text)
            return re.sub(r'\s+', ' ', text).strip()
            
        norm_query = normalize(query)
        norm_track = normalize(track_name)
        
        if norm_track in norm_query or norm_query in norm_track:
            return True
            
        query_words = set(norm_query.split())
        track_words = set(norm_track.split())
        
        stop_words = {'the', 'a', 'an', 'and', 'or', 'of', 'in', 'on', 'at', 'to', 'for', 'with', 'by', 'is', 'are', 'video', 'official', 'lyrics', 'live', 'audio', 'remix', 'cover', 'türkçe', 'turkce'}
        
        query_meaningful = query_words - stop_words
        track_meaningful = track_words - stop_words
        
        if track_meaningful and query_meaningful:
            overlap = track_meaningful.intersection(query_meaningful)
            if overlap:
                return True
                
        return False

    def _clean_lyrics_query(self, query: str) -> str:
        """Strip noisy tags from titles to improve lyrics search accuracy."""
        cleaned = re.sub(r'\s*[\[(](official|lyrics?|video|audio|live|hd|4k)[^\])]*[\])]', '', query, flags=re.IGNORECASE)
        cleaned = re.sub(r'\s*[-|]\s*(official|lyrics?|video|audio|live|hd|4k)\b.*$', '', cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()
        return cleaned

    async def _fetch_lyrics(self, query: str) -> Optional[dict]:
        """Fetch lyrics from lrclib using free public search API."""
        url = f"https://lrclib.net/api/search?q={quote(query)}"
        timeout = aiohttp.ClientTimeout(total=12)

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers={'User-Agent': 'MusicBot/1.0'}) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
        except Exception:
            return None

        if not isinstance(data, list):
            return None

        for item in data:
            if not self._is_query_match(query, item.get('trackName', '')):
                continue
            lyrics = item.get('plainLyrics') or item.get('syncedLyrics')
            if not lyrics:
                continue

            # Remove sync timestamps like [01:23.45] if only synced lyrics exist.
            lyrics = re.sub(r'^\[\d{1,2}:\d{2}(?:\.\d{1,2})?\]\s*', '', lyrics, flags=re.MULTILINE).strip()
            if not lyrics:
                continue

            return {
                'track': item.get('trackName') or query,
                'artist': item.get('artistName') or 'Unknown',
                'lyrics': lyrics,
            }

        return None

    def _parse_synced_lyrics(self, synced_lyrics: str) -> list[tuple[float, str]]:
        parsed = []
        for raw_line in synced_lyrics.splitlines():
            timestamps = re.findall(r'\[(\d{1,2}):(\d{2})(?:\.(\d{1,3}))?\]', raw_line)
            text = re.sub(r'\[(\d{1,2}):(\d{2})(?:\.(\d{1,3}))?\]', '', raw_line).strip()
            if not timestamps or not text:
                continue

            for minute_str, second_str, fraction_str in timestamps:
                minute = int(minute_str)
                second = int(second_str)
                fraction = 0.0
                if fraction_str:
                    if len(fraction_str) == 3:
                        fraction = int(fraction_str) / 1000
                    elif len(fraction_str) == 2:
                        fraction = int(fraction_str) / 100
                    else:
                        fraction = int(fraction_str) / 10
                parsed.append((minute * 60 + second + fraction, text))

        parsed.sort(key=lambda item: item[0])
        return parsed

    def _get_lyrics_cache_path(self, query: str) -> str:
        sanitized = re.sub(r'[^a-zA-Z0-9_\-\s]', '', query)
        sanitized = re.sub(r'[\s_]+', '_', sanitized).strip('_')
        sanitized = sanitized[:100]
        base_dir = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(base_dir, "lyrics", f"{sanitized.lower()}.json")

    async def _fetch_synced_lyrics(self, query: str) -> Optional[dict]:
        # Check local cache first
        cache_path = self._get_lyrics_cache_path(query)
        if os.path.exists(cache_path):
            try:
                with open(cache_path, 'r', encoding='utf-8') as f:
                    cached_data = json.load(f)
                    if cached_data is None:
                        return None
                    return cached_data
            except Exception as e:
                logger.error(f"Error reading lyrics cache for {query}: {e}")

        url = f"https://lrclib.net/api/search?q={quote(query)}"
        timeout = aiohttp.ClientTimeout(total=12)

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers={'User-Agent': 'MusicBot/1.0'}) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
        except Exception:
            return None

        if not isinstance(data, list):
            return None

        result = None
        for item in data:
            if not self._is_query_match(query, item.get('trackName', '')):
                continue
            synced = item.get('syncedLyrics')
            if not synced:
                continue

            parsed_lines = self._parse_synced_lyrics(synced)
            if not parsed_lines:
                continue

            result = {
                'track': item.get('trackName') or query,
                'artist': item.get('artistName') or 'Unknown',
                'lines': parsed_lines,
            }
            break

        # Save to local cache (even if None, so we don't request it again)
        try:
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            with open(cache_path, 'w', encoding='utf-8') as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Error writing lyrics cache for {query}: {e}")

        return result

    def _build_lyricsnow_embed(self, lyrics_data: dict, current_second: float) -> discord.Embed:
        lines = lyrics_data['lines']
        current_index = 0
        for idx, (timestamp, _) in enumerate(lines):
            if timestamp <= current_second:
                current_index = idx
            else:
                break

        start_index = max(0, current_index - 2)
        end_index = min(len(lines), current_index + 3)

        output_lines = []
        for idx in range(start_index, end_index):
            text = lines[idx][1]
            if idx == current_index:
                output_lines.append(f"▶ **{text}**")
            else:
                output_lines.append(text)

        embed = discord.Embed(
            title="🎙️ LyricsNow",
            description="\n".join(output_lines),
            color=discord.Color.orange()
        )
        embed.add_field(name="Song", value=f"**{lyrics_data['track']}** - {lyrics_data['artist']}", inline=False)
        embed.set_footer(text=f"Source: lrclib.net • {int(current_second)}s")
        return embed

    def _stop_lyricsnow_task(self, guild_id: int):
        task = self.lyricsnow_tasks.pop(guild_id, None)
        if task and not task.done():
            task.cancel()

    async def _run_lyricsnow_live(self, guild_id: int, message: discord.Message, song_key: str, lyrics_data: dict):
        try:
            while True:
                await asyncio.sleep(2)

                guild = self.bot.get_guild(guild_id)
                if not guild:
                    break

                player = self.get_player(guild)
                voice_client = guild.voice_client
                if not player.current or player.current_song_key != song_key:
                    break
                if not voice_client or (not voice_client.is_playing() and not voice_client.is_paused()):
                    break

                current_second = player.get_playback_position_seconds() + LYRICSNOW_AHEAD_SECONDS
                await message.edit(embed=self._build_lyricsnow_embed(lyrics_data, current_second))
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.warning(f"lyricsnow live update stopped: {e}")
        finally:
            task = self.lyricsnow_tasks.get(guild_id)
            if task and task.done():
                self.lyricsnow_tasks.pop(guild_id, None)


    def _parse_duration_seconds(self, duration: str) -> Optional[int]:
        if not duration or duration == "Unknown":
            return None

        parts = duration.split(":")
        try:
            values = [int(part) for part in parts]
        except ValueError:
            return None

        if len(values) == 2:
            minutes, seconds = values
            return minutes * 60 + seconds
        if len(values) == 3:
            hours, minutes, seconds = values
            return hours * 3600 + minutes * 60 + seconds

        return None

    def _build_progress_bar(self, elapsed_seconds: float, total_seconds: int, bar_length: int = 14) -> str:
        if total_seconds <= 0:
            return "▬" * bar_length

        progress = max(0.0, min(1.0, elapsed_seconds / total_seconds))
        knob = int(round(progress * (bar_length - 1)))
        return "▬" * knob + "🔘" + "▬" * (bar_length - 1 - knob)

    def _add_progress_field(self, embed: discord.Embed, song: Song, player: MusicPlayer):
        elapsed_seconds = player.get_playback_position_seconds()
        elapsed_text = self.format_duration(elapsed_seconds)
        total_seconds = self._parse_duration_seconds(song.duration)

        voice_client = player.guild.voice_client
        state_emoji = "⏸️" if voice_client and voice_client.is_paused() else "▶️"

        if total_seconds:
            progress_bar = self._build_progress_bar(elapsed_seconds, total_seconds)
            remaining = max(0, total_seconds - int(elapsed_seconds))
            progress_value = (
                f"{state_emoji} {progress_bar}\n"
                f"`{elapsed_text} / {self.format_duration(total_seconds)}` • `-{self.format_duration(remaining)} left`"
            )
        else:
            progress_value = f"{state_emoji} `{elapsed_text}`"

        embed.add_field(name="\u200b", value=progress_value, inline=False)

    def create_now_playing_embed(self, song: Song, player: Optional[MusicPlayer] = None) -> discord.Embed:
        source_emoji = {'youtube': '🔴', 'spotify': '💚', 'local': '📁'}.get(song.source_type, '🎵')
        source_color = discord.Color.from_rgb(124, 92, 255)

        if song.source_type != 'local' and str(song.url).startswith('http'):
            description = f"## [{song.title}]({song.url})"
        else:
            description = f"## {song.title}"
        details = [part for part in (song.artist, song.album) if part]
        if details:
            description += "\n" + " • ".join(details)

        embed = discord.Embed(
            title="Now playing",
            description=description,
            color=source_color
        )
        if player:
            self._add_progress_field(embed, song, player)
            loop_status = "Song" if player.loop else ("Queue" if player.loop_queue else "Off")
            status = [f"🔊 {int(player.volume * 100)}%", f"🔁 {loop_status}"]
            if player.autoplay:
                status.append("✨ Smart Autoplay")

            if player.audio_filter or player.crossfade_seconds or player.automix_enabled:
                effects = []
                if player.audio_filter:
                    effects.append(player.audio_filter)
                if player.automix_enabled:
                    effects.append(f"AutoMix {player.automix_blend_seconds}s")
                elif player.crossfade_seconds:
                    effects.append(f"crossfade {player.crossfade_seconds}s")
                status.append("🎛️ " + " • ".join(effects))

            # Read cached live lyrics if available
            try:
                cache_path = self._get_lyrics_cache_path(song.title)
                if os.path.exists(cache_path):
                    with open(cache_path, 'r', encoding='utf-8') as f:
                        lyrics_data = json.load(f)
                    
                    if lyrics_data and lyrics_data.get('lines'):
                        lines = lyrics_data['lines']
                        current_second = player.get_playback_position_seconds()
                        
                        current_index = 0
                        for idx, (timestamp, _) in enumerate(lines):
                            if timestamp <= current_second:
                                current_index = idx
                            else:
                                break
                                
                        start_index = max(0, current_index - 2)
                        end_index = min(len(lines), current_index + 3)
                        
                        output_lines = []
                        for idx in range(start_index, end_index):
                            text = lines[idx][1]
                            if idx == current_index:
                                output_lines.append(f"▶ **{text}**")
                            else:
                                output_lines.append(text)
                                
                        lyrics_text = "\n".join(output_lines)
                        embed.add_field(name="🎙️ Live Lyrics", value=lyrics_text, inline=False)
            except Exception as e:
                logger.debug(f"Could not read synced lyrics for nowplaying: {e}")

            if player.queue:
                next_title = player.queue[0].title
                if len(next_title) > 70:
                    next_title = next_title[:67] + "..."
                embed.add_field(
                    name=f"Up next • {len(player.queue)} queued",
                    value=f"⏭️ {next_title}",
                    inline=False
                )
            requester = getattr(song.requester, 'display_name', None) or 'Autoplay'
            channel = getattr(getattr(player.guild, 'voice_client', None), 'channel', None)
            footer = f"{source_emoji} {requester}"
            if channel:
                footer += f" • 🔊 {channel.name}"
            footer += " • " + " • ".join(status)
            embed.set_footer(text=footer[:2048])
        else:
            embed.add_field(name="Duration", value=song.duration, inline=True)
            embed.add_field(name="Requested by", value=song.requester.mention, inline=True)

        if song.thumbnail:
            embed.set_thumbnail(url=song.thumbnail)

        return embed

    def _stop_now_playing_task(self, guild_id: int):
        task = self.nowplaying_tasks.pop(guild_id, None)
        if task and not task.done():
            task.cancel()

    async def _run_now_playing_live(self, guild_id: int, message: discord.Message, song_key: str):
        try:
            while True:
                # 5s keeps the progress bar moving without hitting Discord's
                # message-edit rate limits (1s edits queue up and lag badly)
                await asyncio.sleep(5)

                guild = self.bot.get_guild(guild_id)
                if not guild:
                    break

                player = self.get_player(guild)
                voice_client = guild.voice_client
                if not player.current or player.current_song_key != song_key:
                    break
                if not voice_client or (not voice_client.is_playing() and not voice_client.is_paused()):
                    break

                embed = self.create_now_playing_embed(player.current, player)
                await message.edit(embed=embed)
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.debug(f"Could not update now playing message: {e}")
        finally:
            task = self.nowplaying_tasks.get(guild_id)
            if task is asyncio.current_task():
                self.nowplaying_tasks.pop(guild_id, None)
    
    async def create_now_playing_with_lyrics_embed(self, song: Song, player: Optional[MusicPlayer] = None) -> discord.Embed:
        """Create a now playing embed with lyrics preview"""
        embed = self.create_now_playing_embed(song, player)

        try:
            search_query = self._clean_lyrics_query(song.title)
            lyrics_data = await self._fetch_synced_lyrics(search_query)
            if lyrics_data and lyrics_data.get('lines'):
                preview_lines = [text for _, text in lyrics_data['lines'][:3] if text.strip()]
                if preview_lines:
                    preview = "\n".join(f"*{line}*" for line in preview_lines)
                    embed.add_field(name="📝 Lyrics Preview", value=preview[:1024], inline=False)
        except Exception as e:
            logger.debug(f"Could not fetch lyrics for now playing: {e}")

        return embed

    async def _send_now_playing_update(self, player: MusicPlayer):
        """Send now playing embed with lyrics to the last message channel"""
        try:
            if not player.current or not player.last_message_channel:
                return

            self._stop_now_playing_task(player.guild.id)

            # Remove the previous auto-posted now-playing message so the
            # channel doesn't fill up with stale players
            old_message = getattr(player, 'nowplaying_message', None)
            if old_message:
                try:
                    await old_message.delete()
                except Exception:
                    pass
                player.nowplaying_message = None

            embed = await self.create_now_playing_with_lyrics_embed(player.current, player)
            view = MusicControlView(self.bot, player.guild.id)
            message = await player.last_message_channel.send(embed=embed, view=view)
            player.nowplaying_message = message
            task = asyncio.create_task(self._run_now_playing_live(player.guild.id, message, player.current_song_key))
            self.nowplaying_tasks[player.guild.id] = task

            # Karaoke mode follows every new song with fresh live lyrics
            if player.karaoke_mode:
                asyncio.create_task(self._start_lyricsnow_for_current(player))
        except Exception as e:
            logger.debug(f"Could not send now playing update: {e}")

    def evaluate_skip_vote(self, user: discord.Member, guild: discord.Guild) -> tuple[bool, str]:
        """Vote-skip gate: returns (should_skip_now, status_message).

        DJ role, server managers, the song's requester, and small channels
        (fewer than 3 listeners) skip instantly; everyone else needs a
        majority of the current listeners to vote.
        """
        player = self.get_player(guild)
        voice_client = guild.voice_client
        if not voice_client or not voice_client.is_connected():
            return False, "❌ Not connected to voice!"

        if not user.voice or user.voice.channel != voice_client.channel:
            return False, "❌ You need to be in the voice channel to skip!"

        humans = [m for m in voice_client.channel.members if not m.bot]
        dj_role_name = getattr(config, 'DJ_ROLE_NAME', 'DJ').lower()
        is_privileged = (
            user.guild_permissions.manage_guild
            or user.guild_permissions.administrator
            or any(role.name.lower() == dj_role_name for role in user.roles)
            or (player.current and player.current.requester and player.current.requester.id == user.id)
        )
        if is_privileged or len(humans) < 3:
            return True, ""

        player.skip_votes.add(user.id)
        listener_ids = {m.id for m in humans}
        votes = len(player.skip_votes & listener_ids)
        needed = len(humans) // 2 + 1
        if votes >= needed:
            return True, ""

        return False, f"🗳️ Skip vote added: **{votes}/{needed}** needed. (DJ role or the song's requester can skip instantly.)"

    @app_commands.command(name="skip", description="Skip the current song (may start a vote)")
    async def skip(self, interaction: discord.Interaction):
        if not interaction.guild.voice_client or not interaction.guild.voice_client.is_playing():
            await interaction.response.send_message("❌ Nothing is playing!", ephemeral=True)
            return

        should_skip, vote_message = self.evaluate_skip_vote(interaction.user, interaction.guild)
        if not should_skip:
            await interaction.response.send_message(vote_message, ephemeral=vote_message.startswith("❌"))
            return

        player = self.get_player(interaction.guild)
        player.loop = False
        
        # Store the channel for now playing updates
        player.last_message_channel = interaction.channel
        
        # Defer the response since we need to wait for the next song to start
        await interaction.response.defer()
        
        # Stop current song (this will trigger play_next)
        interaction.guild.voice_client.stop()
        
        # Wait a moment for the next song to start playing
        await asyncio.sleep(0.5)
        
        if player.current:
            await interaction.followup.send("⏭️ Skipped!", ephemeral=True)
        else:
            await interaction.followup.send("⏭️ Skipped! No more songs in queue.")

    @app_commands.command(name="stop", description="Stop playback and clear the queue")
    async def stop(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild)
        self._stop_now_playing_task(interaction.guild.id)
        player.queue.clear()
        player.current = None
        player.loop = False
        player.loop_queue = False
        player.preloaded_sources.clear()  # Clear preloaded cache
        player.reset_playback_clock()
        self._stop_lyricsnow_task(interaction.guild.id)
        
        if interaction.guild.voice_client:
            interaction.guild.voice_client.stop()
        
        await interaction.response.send_message("⏹️ Stopped and cleared queue!")

    @app_commands.command(name="pause", description="Pause the current song")
    async def pause(self, interaction: discord.Interaction):
        if interaction.guild.voice_client and interaction.guild.voice_client.is_playing():
            player = self.get_player(interaction.guild)
            player.mark_paused()
            interaction.guild.voice_client.pause()
            await interaction.response.send_message("⏸️ Paused!")
        else:
            await interaction.response.send_message("❌ Nothing is playing!", ephemeral=True)

    @app_commands.command(name="resume", description="Resume the paused song")
    async def resume(self, interaction: discord.Interaction):
        if interaction.guild.voice_client and interaction.guild.voice_client.is_paused():
            player = self.get_player(interaction.guild)
            player.mark_resumed()
            interaction.guild.voice_client.resume()
            await interaction.response.send_message("▶️ Resumed!")
        else:
            await interaction.response.send_message("❌ Nothing is paused!", ephemeral=True)

    @app_commands.command(name="queue", description="Show the current queue")
    async def queue(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild)
        
        if not player.current and not player.queue:
            await interaction.response.send_message("📭 Queue is empty!", ephemeral=True)
            return
        
        embed = discord.Embed(title="🎶 Music Queue", color=discord.Color.blurple())
        
        if player.current:
            embed.add_field(
                name="Now Playing",
                value=f"**{player.current.title}** [{player.current.duration}]",
                inline=False
            )
        
        if player.queue:
            queue_list = []
            for i, song in enumerate(list(player.queue)[:10], 1):
                queue_list.append(f"`{i}.` **{song.title}** [{song.duration}]")
            
            if len(player.queue) > 10:
                queue_list.append(f"\n*...and {len(player.queue) - 10} more*")
            
            embed.add_field(name="Up Next", value="\n".join(queue_list), inline=False)
        
        status = []
        if player.loop:
            status.append("🔂 Loop: Song")
        if player.loop_queue:
            status.append("🔁 Loop: Queue")
        if status:
            embed.set_footer(text=" | ".join(status))
        
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="nowplaying", description="Show the currently playing song")
    async def nowplaying(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild)
        
        if not player.current:
            await interaction.response.send_message("❌ Nothing is playing!", ephemeral=True)
            return

        await interaction.response.defer()
        self._stop_now_playing_task(interaction.guild.id)
        embed = await self.create_now_playing_with_lyrics_embed(player.current, player)
        view = MusicControlView(self.bot, interaction.guild.id)
        await interaction.edit_original_response(embed=embed, view=view)

        message = await interaction.original_response()
        task = asyncio.create_task(self._run_now_playing_live(interaction.guild.id, message, player.current_song_key))
        self.nowplaying_tasks[interaction.guild.id] = task

    def _parse_seek_time(self, value: str) -> Optional[int]:
        """Parse '90', '1:30' or '1:02:03' into seconds."""
        value = value.strip()
        if value.isdigit():
            return int(value)
        return self._parse_duration_seconds(value)

    @app_commands.command(name="seek", description="Jump to a position in the current song")
    @app_commands.describe(position="Position like 90, 1:30 or 1:02:03")
    async def seek(self, interaction: discord.Interaction, position: str):
        player = self.get_player(interaction.guild)

        if not player.current:
            await interaction.response.send_message("❌ Nothing is playing!", ephemeral=True)
            return

        seconds = self._parse_seek_time(position)
        if seconds is None:
            await interaction.response.send_message("❌ Invalid time. Use seconds (`90`) or `mm:ss` (`1:30`).", ephemeral=True)
            return

        total = self._parse_duration_seconds(player.current.duration)
        if total and seconds >= total:
            await interaction.response.send_message(f"❌ That's past the end of the song ({player.current.duration}).", ephemeral=True)
            return

        await interaction.response.defer()
        if await player.seek_to(seconds):
            await interaction.followup.send(f"⏩ Jumped to `{self.format_duration(seconds)}` in **{player.current.title}**")
        else:
            await interaction.followup.send("❌ Can't seek in this song (only downloaded/local tracks support seeking).")

    @app_commands.command(name="replay", description="Restart the current song from the beginning")
    async def replay(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild)

        if not player.current:
            await interaction.response.send_message("❌ Nothing is playing!", ephemeral=True)
            return

        await interaction.response.defer()
        if await player.seek_to(0):
            await interaction.followup.send(f"🔄 Restarted **{player.current.title}**")
        else:
            await interaction.followup.send("❌ Can't replay this song (only downloaded/local tracks support it).")

    @app_commands.command(name="autoplay", description="Toggle smart artist/album radio when the queue is empty")
    async def autoplay(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild)
        player.autoplay = not player.autoplay

        if not player.autoplay:
            await interaction.response.send_message("✨ Smart Autoplay is **off**.")
            return

        await interaction.response.send_message(
            "✨ Smart Autoplay is **on** - when the queue runs out, I'll continue with "
            "the same artist, album, or closely related artists."
        )

        # If we're sitting idle in voice, start playing right away
        vc = interaction.guild.voice_client
        if vc and vc.is_connected() and not vc.is_playing() and not vc.is_paused() and not player.queue:
            player.last_message_channel = interaction.channel
            await player.play_next()

    @app_commands.command(name="history", description="Show recently played songs")
    async def history(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild)

        if not player.history:
            await interaction.response.send_message("❌ Nothing has been played yet!", ephemeral=True)
            return

        songs = list(player.history)[:10]
        lines = []
        for i, song in enumerate(songs, 1):
            title = song.title if len(song.title) <= 60 else song.title[:57] + "..."
            when = f" • <t:{song.played_at}:R>" if song.played_at else ""
            artist = f" — {song.artist}" if song.artist and self._artist_key(song.artist) not in self._artist_key(title) else ""
            lines.append(f"**{i}.** {title}{artist}\n　`{song.duration}`{when}")

        embed = discord.Embed(
            title="🕘 Your listening history",
            description="\n\n".join(lines),
            color=discord.Color.from_rgb(124, 92, 255)
        )
        unique = len({song.url for song in player.history})
        embed.set_author(name=f"{len(player.history)} tracks played • {unique} unique")
        embed.set_footer(text="Choose a track below to put it back in the queue")
        await interaction.response.send_message(
            embed=embed, view=HistoryReplayView(self, interaction.guild.id, songs)
        )

    @app_commands.command(name="search", description="Search YouTube and pick from the top 5 results")
    @app_commands.describe(query="What to search for")
    async def search(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer()
        loop = asyncio.get_event_loop()

        try:
            data = await loop.run_in_executor(
                None,
                lambda: ytdl_playlist.extract_info(f"ytsearch5:{query}", download=False)
            )
        except Exception as e:
            logger.error(f"/search failed for {query!r}: {e}")
            await interaction.followup.send("❌ Search failed - YouTube may be blocking lookups, try again in a moment.")
            return

        entries = [e for e in (data.get('entries') or []) if e][:5] if data else []
        if not entries:
            await interaction.followup.send(f"❌ No results for **{query}**.")
            return

        lines = []
        for i, entry in enumerate(entries, 1):
            title = (entry.get('title') or 'Unknown')
            title = title if len(title) <= 70 else title[:67] + "..."
            duration = self.format_duration(entry.get('duration') or 0)
            lines.append(f"`{i}.` **{title}** [{duration}]")

        embed = discord.Embed(
            title=f"🔎 Results for \"{query}\"",
            description="\n".join(lines),
            color=discord.Color.blurple()
        )
        embed.set_footer(text="Pick one from the menu below • expires in 2 minutes")
        await interaction.followup.send(embed=embed, view=SearchResultView(self, entries, interaction.user))

    # ---------- saved playlists ----------

    PLAYLISTS_FILE = os.path.join(BOT_DIR, 'playlists.json')

    def _read_playlists(self) -> dict:
        try:
            with open(self.PLAYLISTS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _write_playlists(self, data: dict):
        with open(self.PLAYLISTS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    playlist = app_commands.Group(name="playlist", description="Save and load the queue as a named playlist")

    @playlist.command(name="save", description="Save the current song + queue as a playlist")
    @app_commands.describe(name="Name for the playlist")
    async def playlist_save(self, interaction: discord.Interaction, name: str):
        player = self.get_player(interaction.guild)
        songs = ([player.current] if player.current else []) + list(player.queue)
        # Local uploads live in temp folders that won't survive, so skip them
        songs = [s for s in songs if s.source_type != 'local'][:200]

        if not songs:
            await interaction.response.send_message("❌ Nothing to save - the queue is empty!", ephemeral=True)
            return

        name = name.strip()[:50]
        data = self._read_playlists()
        guild_lists = data.setdefault(str(interaction.guild.id), {})
        guild_lists[name] = [
            {
                'title': s.title,
                'url': s.url,
                'duration': s.duration,
                'source_type': s.source_type,
                'thumbnail': s.thumbnail,
            }
            for s in songs
        ]
        self._write_playlists(data)

        await interaction.response.send_message(f"💾 Saved **{name}** with **{len(songs)}** songs! Load it anytime with `/playlist load {name}`")

    async def _playlist_name_autocomplete(self, interaction: discord.Interaction, current: str):
        guild_lists = self._read_playlists().get(str(interaction.guild.id), {})
        names = [n for n in guild_lists if current.lower() in n.lower()]
        return [app_commands.Choice(name=n, value=n) for n in sorted(names)[:25]]

    @playlist.command(name="load", description="Load a saved playlist into the queue")
    @app_commands.describe(name="Playlist to load", shuffle="Shuffle the songs while loading")
    @app_commands.autocomplete(name=_playlist_name_autocomplete)
    async def playlist_load(self, interaction: discord.Interaction, name: str, shuffle: bool = False):
        guild_lists = self._read_playlists().get(str(interaction.guild.id), {})
        entries = guild_lists.get(name)
        if entries is None:
            await interaction.response.send_message(f"❌ No playlist named **{name}**. See `/playlist list`.", ephemeral=True)
            return

        await interaction.response.defer()
        if not await self.ensure_voice(interaction):
            return

        player = self.get_player(interaction.guild)
        player.last_message_channel = interaction.channel

        if shuffle:
            entries = list(entries)
            random.shuffle(entries)

        for entry in entries:
            player.queue.append(Song(
                title=entry.get('title', 'Unknown'),
                url=entry['url'],
                duration=entry.get('duration', 'Unknown'),
                requester=interaction.user,
                source_type=entry.get('source_type', 'youtube'),
                thumbnail=entry.get('thumbnail'),
            ))

        vc = interaction.guild.voice_client
        started = False
        if vc and not vc.is_playing() and not vc.is_paused():
            await player.play_next()
            started = True

        embed = discord.Embed(
            title="📂 Playlist Loaded",
            description=f"Queued **{len(entries)}** songs from **{name}**" + (" (shuffled)" if shuffle else ""),
            color=discord.Color.green()
        )
        if started and player.current:
            embed.set_footer(text=f"▶️ Now playing: {player.current.title}")
        await interaction.followup.send(embed=embed)

    @playlist.command(name="list", description="Show saved playlists for this server")
    async def playlist_list(self, interaction: discord.Interaction):
        guild_lists = self._read_playlists().get(str(interaction.guild.id), {})
        if not guild_lists:
            await interaction.response.send_message("📭 No saved playlists yet - build a queue and use `/playlist save`!", ephemeral=True)
            return

        lines = [f"• **{name}** ({len(songs)} songs)" for name, songs in sorted(guild_lists.items())]
        embed = discord.Embed(
            title="💾 Saved Playlists",
            description="\n".join(lines[:25]),
            color=discord.Color.blurple()
        )
        await interaction.response.send_message(embed=embed)

    @playlist.command(name="delete", description="Delete a saved playlist")
    @app_commands.describe(name="Playlist to delete")
    @app_commands.autocomplete(name=_playlist_name_autocomplete)
    async def playlist_delete(self, interaction: discord.Interaction, name: str):
        data = self._read_playlists()
        guild_lists = data.get(str(interaction.guild.id), {})
        if name not in guild_lists:
            await interaction.response.send_message(f"❌ No playlist named **{name}**.", ephemeral=True)
            return

        del guild_lists[name]
        self._write_playlists(data)
        await interaction.response.send_message(f"🗑️ Deleted playlist **{name}**.")

    # ---------- favorites ----------

    FAVORITES_FILE = os.path.join(BOT_DIR, 'favorites.json')

    def _read_favorites(self) -> dict:
        try:
            with open(self.FAVORITES_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _write_favorites(self, data: dict):
        with open(self.FAVORITES_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def toggle_favorite(self, user_id: int, song: Song) -> tuple[bool, str]:
        """Add or remove a song from a user's favorites. Returns (added, title)."""
        data = self._read_favorites()
        favorites = data.setdefault(str(user_id), [])

        for i, entry in enumerate(favorites):
            if entry.get('url') == song.url:
                favorites.pop(i)
                self._write_favorites(data)
                return False, song.title

        favorites.append({
            'title': song.title,
            'url': song.url,
            'duration': song.duration,
            'source_type': song.source_type,
            'thumbnail': song.thumbnail,
        })
        self._write_favorites(data)
        return True, song.title

    def _playable_favorite(self, entry: dict) -> bool:
        # Local uploads live in temp folders that may be gone by now
        if entry.get('source_type') == 'local':
            return os.path.exists(entry.get('url', ''))
        return True

    def pick_random_favorite(self, guild: discord.Guild, exclude_urls: Optional[set] = None) -> Optional[Song]:
        """Random song from anyone's favorites, used to bias autoplay."""
        data = self._read_favorites()
        entries = [e for favs in data.values() for e in favs if self._playable_favorite(e)]
        if exclude_urls:
            fresh = [e for e in entries if e.get('url') not in exclude_urls]
            entries = fresh or entries
        if not entries:
            return None

        entry = random.choice(entries)
        return Song(
            title=entry.get('title', 'Unknown'),
            url=entry['url'],
            duration=entry.get('duration', 'Unknown'),
            requester=guild.me,
            source_type=entry.get('source_type', 'youtube'),
            thumbnail=entry.get('thumbnail'),
        )

    favorites = app_commands.Group(name="favorites", description="Your liked songs (also the ❤️ button on the player)")

    @favorites.command(name="add", description="Add the current song to your favorites")
    async def favorites_add(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild)
        if not player.current:
            await interaction.response.send_message("❌ Nothing is playing!", ephemeral=True)
            return

        added, title = self.toggle_favorite(interaction.user.id, player.current)
        if added:
            await interaction.response.send_message(f"❤️ Added **{title}** to your favorites!", ephemeral=True)
        else:
            await interaction.response.send_message(f"💔 Removed **{title}** from your favorites.", ephemeral=True)

    @favorites.command(name="list", description="Show your favorite songs")
    async def favorites_list(self, interaction: discord.Interaction):
        favorites = self._read_favorites().get(str(interaction.user.id), [])
        if not favorites:
            await interaction.response.send_message("📭 No favorites yet - hit the ❤️ button while a song plays!", ephemeral=True)
            return

        lines = []
        for i, entry in enumerate(favorites[:20], 1):
            title = entry.get('title', 'Unknown')
            title = title if len(title) <= 60 else title[:57] + "..."
            lines.append(f"`{i}.` **{title}** [{entry.get('duration', '?')}]")
        if len(favorites) > 20:
            lines.append(f"\n*...and {len(favorites) - 20} more*")

        embed = discord.Embed(
            title=f"❤️ {interaction.user.display_name}'s Favorites ({len(favorites)})",
            description="\n".join(lines),
            color=discord.Color.red()
        )
        embed.set_footer(text="/favorites play queues them all • /favorites remove <number> to drop one")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @favorites.command(name="play", description="Queue all your favorite songs")
    @app_commands.describe(shuffle="Shuffle the songs while queueing")
    async def favorites_play(self, interaction: discord.Interaction, shuffle: bool = False):
        favorites = [e for e in self._read_favorites().get(str(interaction.user.id), []) if self._playable_favorite(e)]
        if not favorites:
            await interaction.response.send_message("📭 No favorites yet - hit the ❤️ button while a song plays!", ephemeral=True)
            return

        await interaction.response.defer()
        if not await self.ensure_voice(interaction):
            return

        player = self.get_player(interaction.guild)
        player.last_message_channel = interaction.channel

        if shuffle:
            favorites = list(favorites)
            random.shuffle(favorites)

        for entry in favorites:
            player.queue.append(Song(
                title=entry.get('title', 'Unknown'),
                url=entry['url'],
                duration=entry.get('duration', 'Unknown'),
                requester=interaction.user,
                source_type=entry.get('source_type', 'youtube'),
                thumbnail=entry.get('thumbnail'),
            ))

        vc = interaction.guild.voice_client
        started = False
        if vc and not vc.is_playing() and not vc.is_paused():
            await player.play_next()
            started = True

        embed = discord.Embed(
            title="❤️ Favorites Queued",
            description=f"Queued **{len(favorites)}** of your favorites" + (" (shuffled)" if shuffle else ""),
            color=discord.Color.red()
        )
        if started and player.current:
            embed.set_footer(text=f"▶️ Now playing: {player.current.title}")
        await interaction.followup.send(embed=embed)

    @favorites.command(name="remove", description="Remove a song from your favorites by its list number")
    @app_commands.describe(number="Number shown in /favorites list")
    async def favorites_remove(self, interaction: discord.Interaction, number: int):
        data = self._read_favorites()
        favorites = data.get(str(interaction.user.id), [])
        if not favorites:
            await interaction.response.send_message("📭 You have no favorites to remove.", ephemeral=True)
            return
        if number < 1 or number > len(favorites):
            await interaction.response.send_message(f"❌ Number must be between 1 and {len(favorites)}.", ephemeral=True)
            return

        removed = favorites.pop(number - 1)
        self._write_favorites(data)
        await interaction.response.send_message(f"💔 Removed **{removed.get('title', 'Unknown')}** from your favorites.", ephemeral=True)

    # ---------- listening stats ----------

    STATS_FILE = os.path.join(BOT_DIR, 'stats.jsonl')

    def log_play(self, guild_id: int, song: Song, channel_id: Optional[int] = None):
        """Append one play to the persistent stats log (JSON lines)."""
        try:
            entry = {
                'ts': int(time.time()),
                'guild_id': guild_id,
                'user_id': getattr(song.requester, 'id', None),
                'user_name': getattr(song.requester, 'display_name', 'Unknown'),
                'title': song.title,
                'url': song.url,
                'seconds': parse_duration_to_seconds(song.duration),
                'channel_id': channel_id,
            }
            with open(self.STATS_FILE, 'a', encoding='utf-8') as f:
                f.write(json.dumps(entry, ensure_ascii=False) + '\n')
        except Exception as e:
            logger.debug(f"Could not log play stats: {e}")

    @app_commands.command(name="stats", description="Listening stats: top songs, top requesters, hours played")
    @app_commands.describe(member="Show stats for a specific person instead of the whole server")
    async def stats(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        from collections import Counter

        plays = []
        try:
            with open(self.STATS_FILE, 'r', encoding='utf-8') as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if entry.get('guild_id') != interaction.guild.id:
                        continue
                    if member and entry.get('user_id') != member.id:
                        continue
                    plays.append(entry)
        except FileNotFoundError:
            pass

        if not plays:
            who = f"for {member.display_name} " if member else ""
            await interaction.response.send_message(f"📭 No listening stats {who}yet - stats start counting from now on!", ephemeral=True)
            return

        total_seconds = sum(e.get('seconds') or 0 for e in plays)
        unique_songs = len({e.get('url') for e in plays})
        top_songs = Counter(e.get('title', 'Unknown') for e in plays).most_common(5)

        def shorten(text, limit=45):
            return text if len(text) <= limit else text[:limit - 3] + "..."

        song_lines = [f"`{count}x` {shorten(title)}" for title, count in top_songs]

        title = f"📊 {member.display_name}'s Listening Stats" if member else f"📊 {interaction.guild.name} Listening Stats"
        embed = discord.Embed(title=title, color=discord.Color.blurple())
        embed.add_field(name="Total Plays", value=f"{len(plays):,}", inline=True)
        embed.add_field(name="Unique Songs", value=f"{unique_songs:,}", inline=True)
        embed.add_field(name="Hours Played", value=f"{total_seconds / 3600:.1f}h", inline=True)
        embed.add_field(name="Top Songs", value="\n".join(song_lines), inline=False)

        if not member:
            top_users = Counter(e.get('user_name', 'Unknown') for e in plays).most_common(5)
            user_lines = [f"`{count}x` {shorten(name)}" for name, count in top_users]
            embed.add_field(name="Top Requesters", value="\n".join(user_lines), inline=False)

        embed.set_footer(text="Counting since the stats feature was added • autoplay & 24/7 plays not counted")
        await interaction.response.send_message(embed=embed)

    # ---------- monthly wrapped ----------

    WRAPPED_STATE_FILE = os.path.join(BOT_DIR, 'wrapped_state.json')

    def _read_stats_range(self, guild_id: int, start_ts: int, end_ts: int) -> list:
        plays = []
        try:
            with open(self.STATS_FILE, 'r', encoding='utf-8') as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if entry.get('guild_id') != guild_id:
                        continue
                    ts = entry.get('ts') or 0
                    if start_ts <= ts < end_ts:
                        plays.append(entry)
        except FileNotFoundError:
            pass
        return plays

    def _build_wrapped_embed(self, guild: discord.Guild, start_ts: int, end_ts: int, label: str):
        """Build the monthly recap embed. Returns (embed, busiest_channel_id) or (None, None)."""
        from collections import Counter

        plays = self._read_stats_range(guild.id, start_ts, end_ts)
        if len(plays) < 5:
            return None, None

        total_seconds = sum(e.get('seconds') or 0 for e in plays)
        unique_songs = len({e.get('url') for e in plays})

        def shorten(text, limit=45):
            return text if len(text) <= limit else text[:limit - 3] + "..."

        medals = ['🥇', '🥈', '🥉', '`4.`', '`5.`']
        song_lines = [
            f"{medals[i]} {shorten(title)} ({count}x)"
            for i, (title, count) in enumerate(Counter(e.get('title', 'Unknown') for e in plays).most_common(5))
        ]
        user_lines = [
            f"{medals[i]} {shorten(name)} ({count} plays)"
            for i, (name, count) in enumerate(Counter(e.get('user_name', 'Unknown') for e in plays).most_common(5))
        ]

        embed = discord.Embed(
            title=f"🎁 {guild.name} Wrapped - {label}",
            description=f"**{len(plays):,}** plays • **{unique_songs:,}** unique songs • **{total_seconds / 3600:.1f}** hours of music",
            color=discord.Color.gold()
        )
        embed.add_field(name="🏆 Top Songs", value="\n".join(song_lines), inline=False)
        embed.add_field(name="🎧 Top Requesters", value="\n".join(user_lines), inline=False)
        embed.set_footer(text="See /stats for all-time numbers")

        channel_counter = Counter(e.get('channel_id') for e in plays if e.get('channel_id'))
        busiest_channel_id = channel_counter.most_common(1)[0][0] if channel_counter else None
        return embed, busiest_channel_id

    @app_commands.command(name="wrapped", description="Monthly recap: top songs, top requesters, hours of music")
    async def wrapped(self, interaction: discord.Interaction):
        now = datetime.now()
        month_start = datetime(now.year, now.month, 1)
        prev_start = (month_start - timedelta(days=1)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        embed, _ = self._build_wrapped_embed(
            interaction.guild,
            int(prev_start.timestamp()), int(month_start.timestamp()),
            prev_start.strftime('%B %Y')
        )
        if not embed:
            embed, _ = self._build_wrapped_embed(
                interaction.guild,
                int(month_start.timestamp()), int(time.time()) + 60,
                f"{now.strftime('%B %Y')} (so far)"
            )
        if not embed:
            await interaction.response.send_message("📭 Not enough listening data yet - check back after some more jams!", ephemeral=True)
            return

        await interaction.response.send_message(embed=embed)

    async def _wrapped_autopost_loop(self):
        """Post last month's recap to each server's busiest music channel on the 1st."""
        await self.bot.wait_until_ready()
        while True:
            try:
                now = datetime.now()
                if now.day == 1:
                    month_key = now.strftime('%Y-%m')
                    try:
                        with open(self.WRAPPED_STATE_FILE, 'r', encoding='utf-8') as f:
                            wrapped_state = json.load(f)
                    except (FileNotFoundError, json.JSONDecodeError):
                        wrapped_state = {}

                    if wrapped_state.get('last_posted') != month_key:
                        month_start = datetime(now.year, now.month, 1)
                        prev_start = (month_start - timedelta(days=1)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                        label = prev_start.strftime('%B %Y')

                        for guild in self.bot.guilds:
                            try:
                                embed, channel_id = self._build_wrapped_embed(
                                    guild, int(prev_start.timestamp()), int(month_start.timestamp()), label
                                )
                                if not embed or not channel_id:
                                    continue
                                channel = guild.get_channel(channel_id)
                                if not channel or not channel.permissions_for(guild.me).send_messages:
                                    continue
                                await channel.send(embed=embed)
                                logger.info(f"Posted {label} wrapped for {guild.name}")
                            except Exception as e:
                                logger.warning(f"Wrapped auto-post failed for {guild.name}: {e}")

                        with open(self.WRAPPED_STATE_FILE, 'w', encoding='utf-8') as f:
                            json.dump({'last_posted': month_key}, f)
            except Exception as e:
                logger.warning(f"Wrapped auto-post loop error: {e}")
            await asyncio.sleep(6 * 3600)

    # ---------- queue persistence across restarts ----------

    STATE_FILE = os.path.join(BOT_DIR, 'player_state.json')

    @staticmethod
    def _serialize_song(song: Song) -> dict:
        return {
            'title': song.title,
            'url': song.url,
            'duration': song.duration,
            'source_type': song.source_type,
            'thumbnail': song.thumbnail,
            'artist': song.artist,
            'album': song.album,
            'genres': list(song.genres),
            'played_at': song.played_at,
            'requester_id': getattr(song.requester, 'id', None),
        }

    def _deserialize_song(self, entry: dict, guild: discord.Guild) -> Song:
        requester = guild.get_member(entry.get('requester_id') or 0)
        return Song(
            title=entry.get('title', 'Unknown'),
            url=entry['url'],
            duration=entry.get('duration', 'Unknown'),
            requester=requester or guild.me,
            source_type=entry.get('source_type', 'youtube'),
            thumbnail=entry.get('thumbnail'),
            artist=entry.get('artist'),
            album=entry.get('album'),
            genres=tuple(entry.get('genres') or ()),
            played_at=entry.get('played_at'),
        )

    def snapshot_player_state(self) -> dict:
        state = {}
        for guild_id, player in self.players.items():
            try:
                vc = player.guild.voice_client
                if not vc or not vc.is_connected():
                    continue
                if player.is_247_mode:
                    continue  # 24/7 mode has its own admin commands
                if not player.current and not player.queue:
                    continue
                state[str(guild_id)] = {
                    'ts': int(time.time()),
                    'voice_channel_id': vc.channel.id,
                    'text_channel_id': getattr(player.last_message_channel, 'id', None),
                    'current': self._serialize_song(player.current) if player.current else None,
                    'position': int(player.get_playback_position_seconds()),
                    'queue': [self._serialize_song(s) for s in list(player.queue)[:300]],
                    'volume': player.volume,
                    'loop': player.loop,
                    'loop_queue': player.loop_queue,
                    'autoplay': player.autoplay,
                    'audio_filter': player.audio_filter,
                    'crossfade_seconds': player.crossfade_seconds,
                    'karaoke_mode': player.karaoke_mode,
                    'automix': player.automix_enabled,
                    'automix_blend': player.automix_blend_seconds,
                }
            except Exception as e:
                logger.debug(f"State snapshot failed for guild {guild_id}: {e}")
        return state

    async def _state_saver_loop(self):
        while True:
            try:
                await asyncio.sleep(15)
                state = self.snapshot_player_state()
                serialized = json.dumps(state, ensure_ascii=False, sort_keys=True)
                if serialized == self._last_saved_state:
                    continue
                tmp_path = self.STATE_FILE + '.tmp'
                with open(tmp_path, 'w', encoding='utf-8') as f:
                    f.write(serialized)
                os.replace(tmp_path, self.STATE_FILE)
                self._last_saved_state = serialized
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.debug(f"State saver error: {e}")

    async def restore_player_state(self):
        """Rejoin voice and resume the queue saved before the last shutdown."""
        try:
            with open(self.STATE_FILE, 'r', encoding='utf-8') as f:
                state = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return

        for guild_id_str, entry in state.items():
            try:
                # A queue from many hours ago is more surprising than helpful
                if time.time() - (entry.get('ts') or 0) > 6 * 3600:
                    continue
                guild = self.bot.get_guild(int(guild_id_str))
                if not guild or guild.voice_client:
                    continue
                voice_channel = guild.get_channel(entry.get('voice_channel_id') or 0)
                if not voice_channel:
                    continue

                player = self.get_player(guild)
                player.volume = entry.get('volume', 0.5)
                player.loop = entry.get('loop', False)
                player.loop_queue = entry.get('loop_queue', False)
                player.autoplay = entry.get('autoplay', False)
                player.audio_filter = entry.get('audio_filter')
                player.crossfade_seconds = entry.get('crossfade_seconds', 0)
                player.karaoke_mode = entry.get('karaoke_mode', False)
                player.automix_enabled = entry.get('automix', False)
                player.automix_blend_seconds = entry.get('automix_blend', AUTOMIX_DEFAULT_BLEND_SECONDS)

                text_channel = guild.get_channel(entry.get('text_channel_id') or 0)
                if text_channel:
                    player.last_message_channel = text_channel

                song_entries = ([entry['current']] if entry.get('current') else []) + (entry.get('queue') or [])
                for song_entry in song_entries:
                    # Local uploads in temp folders may be gone after a restart
                    if song_entry.get('source_type') == 'local' and not os.path.exists(song_entry.get('url', '')):
                        continue
                    try:
                        player.queue.append(self._deserialize_song(song_entry, guild))
                    except Exception:
                        continue

                if not player.queue:
                    continue

                humans = [m for m in voice_channel.members if not m.bot]
                if not humans:
                    logger.info(f"Restored {len(player.queue)} queued songs for {guild.name}, but voice is empty - not joining")
                    continue

                logger.info(f"🔄 Resuming playback in {guild.name}: {len(player.queue)} songs")
                await voice_channel.connect(timeout=20.0, reconnect=True)
                await player.play_next()

                position = entry.get('position') or 0
                if position > 20 and player.current:
                    await player.seek_to(position)

                if text_channel and player.current:
                    try:
                        await text_channel.send(
                            f"🔄 I'm back! Resuming **{player.current.title}**"
                            + (f" and {len(player.queue)} more queued song(s)" if player.queue else "") + "."
                        )
                    except Exception:
                        pass
            except Exception as e:
                logger.warning(f"Could not restore player state for guild {guild_id_str}: {e}")

    async def on_bot_ready(self):
        """Start background tasks and restore saved playback state (idempotent)."""
        if self._state_saver_task is None or self._state_saver_task.done():
            self._state_saver_task = asyncio.create_task(self._state_saver_loop())
        if self._wrapped_task is None or self._wrapped_task.done():
            self._wrapped_task = asyncio.create_task(self._wrapped_autopost_loop())
        if not self._state_restored:
            self._state_restored = True
            try:
                await self.restore_player_state()
            except Exception as e:
                logger.error(f"State restore failed: {e}", exc_info=True)

    # ---------- audio filters & crossfade ----------

    @app_commands.command(name="filter", description="Apply an audio filter to playback")
    @app_commands.describe(preset="Filter preset (Off to disable)")
    @app_commands.choices(preset=[
        app_commands.Choice(name="Bass Boost", value="bassboost"),
        app_commands.Choice(name="Nightcore", value="nightcore"),
        app_commands.Choice(name="Slowed", value="slowed"),
        app_commands.Choice(name="8D", value="8d"),
        app_commands.Choice(name="Karaoke (remove vocals)", value="karaoke"),
        app_commands.Choice(name="Off", value="off"),
    ])
    async def filter_cmd(self, interaction: discord.Interaction, preset: str):
        player = self.get_player(interaction.guild)
        player.audio_filter = None if preset == 'off' else preset
        player.preloaded_sources.clear()  # Preloads were built with the old filter

        label = {'bassboost': 'Bass Boost', 'nightcore': 'Nightcore', 'slowed': 'Slowed',
                 '8d': '8D', 'karaoke': 'Karaoke', 'off': 'Off'}.get(preset, preset)

        await interaction.response.defer()

        # Try to re-apply mid-song (works for downloaded/local tracks)
        vc = interaction.guild.voice_client
        applied_now = False
        if vc and (vc.is_playing() or vc.is_paused()) and player.current:
            position = int(player.get_playback_position_seconds())
            applied_now = await player.seek_to(position)

        if preset == 'off':
            message = "🎛️ Filters disabled" + ("!" if applied_now else " - takes effect from the next song.")
        elif applied_now:
            message = f"🎛️ Filter **{label}** applied!"
        else:
            message = f"🎛️ Filter **{label}** set - takes effect from the next song."
        await interaction.followup.send(message)

    @app_commands.command(name="crossfade", description="Fade songs in and out for smoother transitions")
    @app_commands.describe(seconds="Fade duration in seconds (0 turns it off)")
    async def crossfade(self, interaction: discord.Interaction, seconds: app_commands.Range[int, 0, MAX_CROSSFADE_SECONDS]):
        player = self.get_player(interaction.guild)
        player.crossfade_seconds = seconds
        player.preloaded_sources.clear()  # Preloads were built with the old fade settings

        if seconds:
            note = " (AutoMix is on and takes over transitions until you turn it off.)" if player.automix_enabled else ""
            await interaction.response.send_message(f"🌊 Crossfade set to **{seconds}s** - songs will fade in and out starting with the next one.{note}")
        else:
            await interaction.response.send_message("🌊 Crossfade turned off.")

    @app_commands.command(name="automix", description="DJ-style AutoMix: blend each song into the next, beat-matched when possible")
    @app_commands.describe(mode="Turn AutoMix on or off",
                           blend="How long songs overlap, in seconds (default 8)")
    @app_commands.choices(mode=[
        app_commands.Choice(name="On", value="on"),
        app_commands.Choice(name="Off", value="off"),
    ])
    async def automix(self, interaction: discord.Interaction, mode: str,
                      blend: Optional[app_commands.Range[int, AUTOMIX_MIN_BLEND_SECONDS, AUTOMIX_MAX_BLEND_SECONDS]] = None):
        player = self.get_player(interaction.guild)
        if blend is not None:
            player.automix_blend_seconds = blend
        enabled = mode == 'on'
        if player.automix_enabled != enabled:
            player.preloaded_sources.clear()  # Preloads were built with the old fade settings
        player.automix_enabled = enabled

        if enabled:
            player.schedule_automix()  # Pick up the song that is already playing
            note = " Crossfade is set aside while AutoMix is on." if player.crossfade_seconds else ""
            await interaction.response.send_message(
                f"🎧 AutoMix **on** - each song will blend into the next like a DJ set "
                f"(up to {player.automix_blend_seconds}s overlap; longer blends are used when tempos line up).{note}")
        else:
            player.cancel_automix()
            await interaction.response.send_message("🎧 AutoMix **off** - songs will play back to back again.")

    # ---------- karaoke mode ----------

    async def _start_lyricsnow_for_current(self, player: MusicPlayer):
        """Post a live synced-lyrics message for the current song (karaoke mode)."""
        channel = player.last_message_channel
        if not channel or not player.current:
            return

        search_query = player.current.title
        cleaned_query = self._clean_lyrics_query(search_query)
        candidates = [search_query]
        if cleaned_query and cleaned_query.lower() != search_query.lower():
            candidates.append(cleaned_query)

        lyrics_data = None
        for candidate in candidates:
            lyrics_data = await self._fetch_synced_lyrics(candidate)
            if lyrics_data:
                break

        try:
            if not lyrics_data:
                await channel.send(f"🎤 No synced lyrics found for **{player.current.title}** - vocals still removed, freestyle it!")
                return
            current_second = player.get_playback_position_seconds() + LYRICSNOW_AHEAD_SECONDS
            message = await channel.send(embed=self._build_lyricsnow_embed(lyrics_data, current_second))
        except Exception as e:
            logger.debug(f"Karaoke lyrics message failed: {e}")
            return

        if player.current_song_key:
            self._stop_lyricsnow_task(player.guild.id)
            task = asyncio.create_task(
                self._run_lyricsnow_live(player.guild.id, message, player.current_song_key, lyrics_data)
            )
            self.lyricsnow_tasks[player.guild.id] = task

    @app_commands.command(name="karaoke", description="Karaoke mode: remove vocals + live synced lyrics on every song")
    async def karaoke(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild)
        player.karaoke_mode = not player.karaoke_mode
        player.preloaded_sources.clear()  # Preloads were built with the old filter

        await interaction.response.defer()
        vc = interaction.guild.voice_client

        if player.karaoke_mode:
            player.audio_filter = 'karaoke'
            player.last_message_channel = interaction.channel

            applied_now = False
            if vc and (vc.is_playing() or vc.is_paused()) and player.current:
                applied_now = await player.seek_to(int(player.get_playback_position_seconds()))

            when = "now" if applied_now else "from the next song"
            await interaction.followup.send(f"🎤 **Karaoke mode ON** - vocals removed {when}, live lyrics follow every song. Run /karaoke again to stop.")

            if vc and (vc.is_playing() or vc.is_paused()) and player.current:
                await self._start_lyricsnow_for_current(player)
        else:
            if player.audio_filter == 'karaoke':
                player.audio_filter = None
            self._stop_lyricsnow_task(interaction.guild.id)

            restored_now = False
            if vc and (vc.is_playing() or vc.is_paused()) and player.current:
                restored_now = await player.seek_to(int(player.get_playback_position_seconds()))

            when = "" if restored_now else " from the next song"
            await interaction.followup.send(f"🎤 Karaoke mode off - vocals are back{when}.")

    @app_commands.command(name="removedupes", description="Remove duplicate songs from the queue")
    async def removedupes(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild)

        seen = set()
        if player.current:
            seen.add(player.current.url)

        deduped = []
        removed = 0
        for song in player.queue:
            if song.url in seen:
                removed += 1
            else:
                seen.add(song.url)
                deduped.append(song)

        if not removed:
            await interaction.response.send_message("✨ No duplicates found!", ephemeral=True)
            return

        player.queue = deque(deduped)
        player.preloaded_sources.clear()
        await interaction.response.send_message(f"🧹 Removed **{removed}** duplicate{'s' if removed != 1 else ''} from the queue!")

    @app_commands.command(name="skipto", description="Skip ahead to a specific song in the queue")
    @app_commands.describe(position="Queue position to jump to (see /queue)")
    async def skipto(self, interaction: discord.Interaction, position: int):
        player = self.get_player(interaction.guild)
        vc = interaction.guild.voice_client

        if not vc or (not vc.is_playing() and not vc.is_paused()):
            await interaction.response.send_message("❌ Nothing is playing!", ephemeral=True)
            return
        if position < 1 or position > len(player.queue):
            await interaction.response.send_message(f"❌ Position must be between 1 and {len(player.queue)}.", ephemeral=True)
            return

        for _ in range(position - 1):
            player.queue.popleft()
        target = player.queue[0]

        await interaction.response.send_message(f"⏭️ Skipping to **{target.title}** (skipped {position} song{'s' if position != 1 else ''})")
        vc.stop()  # after_playing advances to the new queue head

    @app_commands.command(name="move", description="Move a song to another position in the queue")
    @app_commands.describe(from_position="Current position of the song", to_position="Where to move it")
    async def move(self, interaction: discord.Interaction, from_position: int, to_position: int):
        player = self.get_player(interaction.guild)
        queue_length = len(player.queue)

        if queue_length < 2:
            await interaction.response.send_message("❌ Need at least 2 songs in the queue to move something.", ephemeral=True)
            return
        if not (1 <= from_position <= queue_length) or not (1 <= to_position <= queue_length):
            await interaction.response.send_message(f"❌ Positions must be between 1 and {queue_length}.", ephemeral=True)
            return
        if from_position == to_position:
            await interaction.response.send_message("❌ That song is already there!", ephemeral=True)
            return

        queue_list = list(player.queue)
        song = queue_list.pop(from_position - 1)
        queue_list.insert(to_position - 1, song)
        player.queue = deque(queue_list)

        await interaction.response.send_message(f"↕️ Moved **{song.title}** from position {from_position} to {to_position}.")

    @app_commands.command(name="grab", description="DM you the current song so you can find it later")
    async def grab(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild)

        if not player.current:
            await interaction.response.send_message("❌ Nothing is playing!", ephemeral=True)
            return

        song = player.current
        if song.source_type != 'local' and str(song.url).startswith('http'):
            description = f"**[{song.title}]({song.url})**"
        else:
            description = f"**{song.title}**"

        embed = discord.Embed(title="🎣 Song Grabbed", description=description, color=discord.Color.gold())
        embed.add_field(name="Duration", value=song.duration, inline=True)
        embed.add_field(name="Server", value=interaction.guild.name, inline=True)
        if song.thumbnail:
            embed.set_thumbnail(url=song.thumbnail)

        try:
            await interaction.user.send(embed=embed)
            await interaction.response.send_message("📬 Sent it to your DMs!", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("❌ I couldn't DM you - your privacy settings block DMs from this server.", ephemeral=True)

    @app_commands.command(name="lyrics", description="Show lyrics for the current or requested song")
    @app_commands.describe(query="Optional song name (leave empty for current song)")
    async def lyrics(self, interaction: discord.Interaction, query: Optional[str] = None):
        player = self.get_player(interaction.guild)

        if not query and not player.current:
            await interaction.response.send_message("❌ Nothing is playing. Provide a song name with /lyrics query:", ephemeral=True)
            return

        search_query = query or player.current.title
        cleaned_query = self._clean_lyrics_query(search_query)
        candidates = [search_query]
        if cleaned_query and cleaned_query.lower() != search_query.lower():
            candidates.append(cleaned_query)

        await interaction.response.defer()

        result = None
        for candidate in candidates:
            result = await self._fetch_lyrics(candidate)
            if result:
                break

        if not result:
            await interaction.followup.send(f"❌ Lyrics not found for: **{search_query}**")
            return

        lyrics_text = result['lyrics']
        max_len = 3900
        if len(lyrics_text) > max_len:
            lyrics_text = lyrics_text[:max_len].rstrip() + "\n\n... (truncated)"

        embed = discord.Embed(
            title="📜 Lyrics",
            description=lyrics_text,
            color=discord.Color.orange()
        )
        embed.add_field(
            name="Song",
            value=f"**{result['track']}** - {result['artist']}",
            inline=False
        )
        embed.set_footer(text="Source: lrclib.net")

        await interaction.followup.send(embed=embed)

    @app_commands.command(name="lyricsnow", description="Show current lyric line with 2 previous and 2 next lines")
    async def lyricsnow(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild)
        voice_client = interaction.guild.voice_client

        if not player.current or not voice_client or (not voice_client.is_playing() and not voice_client.is_paused()):
            await interaction.response.send_message("❌ Nothing is playing right now!", ephemeral=True)
            return

        search_query = player.current.title
        cleaned_query = self._clean_lyrics_query(search_query)
        candidates = [search_query]
        if cleaned_query and cleaned_query.lower() != search_query.lower():
            candidates.append(cleaned_query)

        await interaction.response.defer()

        lyrics_data = None
        for candidate in candidates:
            lyrics_data = await self._fetch_synced_lyrics(candidate)
            if lyrics_data:
                break

        if not lyrics_data:
            await interaction.followup.send(f"❌ Synced lyrics not found for: **{search_query}**")
            return

        current_second = player.get_playback_position_seconds() + LYRICSNOW_AHEAD_SECONDS
        message = await interaction.followup.send(embed=self._build_lyricsnow_embed(lyrics_data, current_second))

        if player.current_song_key:
            self._stop_lyricsnow_task(interaction.guild.id)
            task = asyncio.create_task(
                self._run_lyricsnow_live(interaction.guild.id, message, player.current_song_key, lyrics_data)
            )
            self.lyricsnow_tasks[interaction.guild.id] = task

    @app_commands.command(name="volume", description="Set the volume (0-100)")
    @app_commands.describe(level="Volume level (0-100)")
    async def volume(self, interaction: discord.Interaction, level: int):
        if level < 0 or level > 100:
            await interaction.response.send_message("❌ Volume must be between 0 and 100!", ephemeral=True)
            return
        
        player = self.get_player(interaction.guild)
        player.volume = level / 100
        
        if interaction.guild.voice_client and interaction.guild.voice_client.source:
            interaction.guild.voice_client.source.volume = player.volume
        
        await interaction.response.send_message(f"🔊 Volume set to **{level}%**")

    @app_commands.command(name="web", description="Get a temporary link to control this server's music player via web dashboard")
    async def web_cmd(self, interaction: discord.Interaction):
        import webui
        token = webui.generate_token(interaction.guild.id)
        
        # Build the url
        base_url = getattr(config, "WEB_SERVER_URL", "https://deeppixel.online")
        if base_url.endswith("/"):
            base_url = base_url[:-1]
            
        url = f"{base_url}/musicbot/?guild_id={interaction.guild.id}&token={token}"
        
        await interaction.response.send_message(
            f"🔗 **Web Player Link:** [Control Dashboard]({url})\n"
            f"*This link is unique to you and authorizes access to control the music player in this server.*",
            ephemeral=True
        )

    @app_commands.command(name="watch", description="Start a Watch Together room for this channel (synced videos + chat)")
    async def watch_cmd(self, interaction: discord.Interaction):
        import watchtogether
        url = watchtogether.get_room_link(interaction.channel.id, f"#{interaction.channel.name}", 'watch')
        embed = discord.Embed(
            title="🎬 Watch Together",
            description=(
                f"**[Click here to join the room]({url})**\n\n"
                "▶️ Play/pause/seek is synced for everyone\n"
                "➕ Add anything yt-dlp understands: YouTube, Shorts, Reels, TikTok, Twitter…\n"
                "💬 Built-in chat\n\n"
                "*Everyone in this channel can use the same link.*"
            ),
            color=0x7c6cf0,
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="reels", description="ReelsTogether: swipe through short videos in sync with friends")
    async def reels_cmd(self, interaction: discord.Interaction):
        import watchtogether
        url = watchtogether.get_room_link(interaction.channel.id, f"#{interaction.channel.name}", 'reels')
        embed = discord.Embed(
            title="📱 ReelsTogether",
            description=(
                f"**[Click here to start swiping]({url})**\n\n"
                "⬆️ Swipe up and everyone moves to the next reel\n"
                "❤️ Double-tap to like — the feed learns what you're into\n"
                "➕ Paste your own Reels / Shorts / TikToks into the feed\n\n"
                "*Works best on your phone. Everyone in this channel shares the feed.*"
            ),
            color=0xe0559a,
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="loop", description="Toggle loop mode")
    @app_commands.describe(mode="Loop mode: song, queue, or off")
    @app_commands.choices(mode=[
        app_commands.Choice(name="Song", value="song"),
        app_commands.Choice(name="Queue", value="queue"),
        app_commands.Choice(name="Off", value="off"),
    ])
    async def loop(self, interaction: discord.Interaction, mode: str):
        player = self.get_player(interaction.guild)
        
        if mode == "song":
            player.loop = True
            player.loop_queue = False
            await interaction.response.send_message("🔂 Looping current song!")
        elif mode == "queue":
            player.loop = False
            player.loop_queue = True
            await interaction.response.send_message("🔁 Looping queue!")
        else:
            player.loop = False
            player.loop_queue = False
            await interaction.response.send_message("➡️ Loop disabled!")

    @app_commands.command(name="shuffle", description="Shuffle the queue")
    async def shuffle(self, interaction: discord.Interaction):
        import random
        player = self.get_player(interaction.guild)
        
        if len(player.queue) < 2:
            await interaction.response.send_message("❌ Not enough songs to shuffle!", ephemeral=True)
            return
        
        queue_list = list(player.queue)
        random.shuffle(queue_list)
        player.queue = deque(queue_list)
        player.preloaded_sources.clear()  # Clear preloaded cache since queue order changed
        
        # Preload the new next song
        asyncio.create_task(player.preload_next_song())
        
        await interaction.response.send_message("🔀 Queue shuffled!")

    @app_commands.command(name="clear", description="Clear the queue")
    async def clear(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild)
        player.queue.clear()
        player.preloaded_sources.clear()  # Clear preloaded cache
        player.schedule_idle_disconnect()
        await interaction.response.send_message("🗑️ Queue cleared!")

    @app_commands.command(name="remove", description="Remove a song from the queue")
    @app_commands.describe(position="Position in queue to remove")
    async def remove(self, interaction: discord.Interaction, position: int):
        player = self.get_player(interaction.guild)
        
        if position < 1 or position > len(player.queue):
            await interaction.response.send_message(f"❌ Invalid position! Queue has {len(player.queue)} songs.", ephemeral=True)
            return
        
        queue_list = list(player.queue)
        removed = queue_list.pop(position - 1)
        player.queue = deque(queue_list)
        
        await interaction.response.send_message(f"🗑️ Removed **{removed.title}** from queue!")

    @app_commands.command(name="disconnect", description="Disconnect the bot from voice channel")
    async def disconnect(self, interaction: discord.Interaction):
        if interaction.guild.voice_client:
            player = self.get_player(interaction.guild)
            player.cancel_idle_disconnect()
            player.queue.clear()
            player.current = None
            player.preloaded_sources.clear()  # Clear preloaded cache
            player.reset_playback_clock()
            self._stop_lyricsnow_task(interaction.guild.id)
            await interaction.guild.voice_client.disconnect()
            await interaction.response.send_message("👋 Disconnected!")
        else:
            await interaction.response.send_message("❌ Not connected to a voice channel!", ephemeral=True)

    @app_commands.command(name="join", description="Join your voice channel")
    async def join(self, interaction: discord.Interaction):
        if not interaction.user.voice:
            await interaction.response.send_message("❌ You need to be in a voice channel!", ephemeral=True)
            return
        
        channel = interaction.user.voice.channel
        
        if interaction.guild.voice_client:
            await interaction.guild.voice_client.move_to(channel)
        else:
            await channel.connect()
        
        await interaction.response.send_message(f"🔊 Joined **{channel.name}**!")

        player = self.get_player(interaction.guild)
        player.schedule_idle_disconnect()

    @app_commands.command(name="serverinvite", description="DM a server picker so you can create an invite link")
    async def serverinvite(self, interaction: discord.Interaction   ):
        if interaction.user.id not in INVITE_ALLOWED_USER_IDS:
            await interaction.response.send_message("❌ You are not allowed to use this command.", ephemeral=interaction.guild is not None)
            return

        view, shown_count, total_count = self._build_invite_view()
        if not view:
            await interaction.response.send_message("❌ I could not find any server I can create an invite for.", ephemeral=interaction.guild is not None)
            return

        content = f"Select a server to get an invite link. Showing {shown_count} of {total_count} server(s)."

        if interaction.guild is None:
            await interaction.response.send_message(content, view=view)
            return

        try:
            await interaction.user.send(content, view=view)
        except discord.Forbidden:
            await interaction.response.send_message("❌ I could not DM you. Please enable DMs and try again.", ephemeral=True)
            return

        await interaction.response.send_message("✅ I sent you a DM with the server picker.", ephemeral=True)

    @app_commands.command(name="invite", description="Get the bot invite link")
    async def invite(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            f"Use this link to invite the bot: {BOT_INVITE_URL}",
            ephemeral=interaction.guild is not None,
        )

    @commands.command(name="sendmessage")
    async def sendmessage(self, ctx: commands.Context, *, message: Optional[str] = None):
        if ctx.author.id not in INVITE_ALLOWED_USER_IDS:
            return

        if not message:
            await ctx.send("Usage: !sendmessage <message>")
            return

        view, shown_count, total_count = self._build_sendmessage_view(message)
        if not view:
            await ctx.send("❌ I could not find any server and channel I can send messages to.")
            return

        content = f"Select a server to send your message. Showing {shown_count} of {total_count} server(s)."
        await ctx.send(content, view=view)

    @commands.command(name="play", help="Play a song from YouTube, Spotify, or upload a local file")
    async def play_text(self, ctx: commands.Context, *, query: str = None):
        if query and query.lower().startswith("query:"):
            query = query[6:].strip()
        fake_interaction = FakeInteraction(ctx)
        try:
            await self.play.callback(self, fake_interaction, query=query, file=None)
        except Exception as e:
            await ctx.send(f"❌ Error: {str(e)}")
            logger.error(f"play_text error: {e}", exc_info=True)

    @commands.command(name="playnext", help="Play a song next in queue")
    async def playnext_text(self, ctx: commands.Context, *, query: str = None):
        if query and query.lower().startswith("query:"):
            query = query[6:].strip()
        fake_interaction = FakeInteraction(ctx)
        try:
            await self.playnext.callback(self, fake_interaction, query=query, file=None)
        except Exception as e:
            await ctx.send(f"❌ Error: {str(e)}")
            logger.error(f"playnext_text error: {e}", exc_info=True)
    
    async def scan_music_directory_for_247_background(self, player: MusicPlayer, interaction: discord.Interaction, discovered_songs_set: set):
        """Background task: scan music directory and add songs to queue as they're found"""
        try:
            cache_json = os.path.join(TWENTYFOURSEVEN_CACHE_DIR, "playlist_cache.json")
            all_discovered = []
            batch_to_add = []  # Accumulate songs before shuffling and adding
            
            loop = asyncio.get_event_loop()
            
            # Scan recursively through all subdirectories
            for root, dirs, files in os.walk(TWENTYFOURSEVEN_CACHE_DIR):
                # Sort files for consistency
                for filename in sorted(files):
                    # Only process audio files
                    if not filename.lower().endswith(('.mp3', '.wav', '.flac', '.ogg', '.m4a')):
                        continue
                    
                    file_path = os.path.join(root, filename)
                    
                    # Skip if already discovered
                    if file_path in discovered_songs_set:
                        continue
                    
                    # Get metadata (non-blocking)
                    title = None
                    duration = "0:00"
                    
                    try:
                        def read_metadata():
                            from mutagen import File as MutagenFile
                            try:
                                meta = MutagenFile(file_path, easy=True)
                                if meta:
                                    t = None
                                    if 'title' in meta:
                                        t = ' '.join(meta['title']) if isinstance(meta['title'], list) else meta['title']
                                    dur = "0:00"
                                    # Properly extract duration from mutagen info object
                                    if hasattr(meta, 'info') and hasattr(meta.info, 'length'):
                                        try:
                                            seconds = int(meta.info.length)
                                            mins = seconds // 60
                                            secs = seconds % 60
                                            dur = f"{mins}:{secs:02d}"
                                        except:
                                            pass
                                    return t, dur
                            except:
                                pass
                            return None, "0:00"
                        
                        title, duration = await loop.run_in_executor(None, read_metadata)
                    except:
                        pass
                    
                    # Fall back to filename if no title metadata
                    if not title:
                        title = os.path.splitext(filename)[0]
                    
                    song_data = {
                        'title': title,
                        'url': file_path,
                        'duration': duration,
                        'source_type': 'local',
                        'thumbnail': None
                    }
                    all_discovered.append(song_data)
                    discovered_songs_set.add(file_path)
                    
                    # Create song and accumulate in batch
                    if player.is_247_mode:
                        song = Song(
                            title=title,
                            url=file_path,
                            duration=duration,
                            requester=interaction.user,
                            source_type='local',
                            thumbnail=None
                        )
                        batch_to_add.append(song)
                        logger.info(f"📀 Indexed: {title} ({duration})")
                    
                    # Add and shuffle the full queue every 50 discovered songs in 24/7 mode
                    if len(batch_to_add) >= 1000 and player.is_247_mode:
                        random.shuffle(batch_to_add)
                        for song in batch_to_add:
                            player.queue.append(song)

                        queue_list = list(player.queue)
                        random.shuffle(queue_list)
                        player.queue = deque(queue_list)
                        player.preloaded_sources.clear()
                        logger.info("🔀 24/7 mode: shuffled the full queue after 1000 discovered songs")

                        batch_to_add.clear()
                    
                    # Update cache periodically (every 10 songs)
                    if len(all_discovered) % 5 == 0:
                        try:
                            with open(cache_json, 'w') as f:
                                json.dump(all_discovered, f, indent=2)
                        except:
                            pass
                    
                    await asyncio.sleep(0.01)  # Yield to other tasks
            
            # Add any remaining songs in final batch, preserving order unless a full batch was reached
            if batch_to_add and player.is_247_mode:
                for song in batch_to_add:
                    player.queue.append(song)
            
            # Final cache update
            try:
                with open(cache_json, 'w') as f:
                    json.dump(all_discovered, f, indent=2)
            except:
                pass
            
            logger.info(f"✅ Background scan complete: {len(all_discovered)} total songs")
            
        except Exception as e:
            logger.error(f"Error in background scan: {e}")
            import traceback
            traceback.print_exc()
    
    @app_commands.command(name="247start", description="[ADMIN] Start 24/7 music mode with the preset playlist")
    @app_commands.checks.has_permissions(administrator=True)
    async def twentyfourseven_start(self, interaction: discord.Interaction):
        global TWENTYFOURSEVEN_ACTIVE_GUILD
        
        # Check if another server is using 24/7
        if TWENTYFOURSEVEN_ACTIVE_GUILD and TWENTYFOURSEVEN_ACTIVE_GUILD != interaction.guild.id:
            await interaction.response.send_message("❌ Another server is currently using 24/7 mode!", ephemeral=True)
            return
        
        player = self.get_player(interaction.guild)
        
        # Check if already in 24/7 mode
        if player.is_247_mode:
            await interaction.response.send_message("⚠️ 24/7 mode is already active!", ephemeral=True)
            return
        
        if not interaction.user.voice:
            await interaction.response.send_message("❌ You need to be in a voice channel!", ephemeral=True)
            return
        
        # RESPOND IMMEDIATELY to avoid timeout
        await interaction.response.send_message("🔄 Starting 24/7 mode...")
        
        # Do everything else in background
        asyncio.create_task(self.setup_247_mode(interaction, player))
    
    async def setup_247_mode(self, interaction: discord.Interaction, player: MusicPlayer):
        """Setup 24/7 mode - runs in background after immediate response"""
        global TWENTYFOURSEVEN_ACTIVE_GUILD
        
        try:
            # Join voice channel
            if not interaction.guild.voice_client:
                await interaction.user.voice.channel.connect()
            elif interaction.guild.voice_client.channel != interaction.user.voice.channel:
                await interaction.guild.voice_client.move_to(interaction.user.voice.channel)
            
            # Check if playlist directory exists
            if not os.path.exists(TWENTYFOURSEVEN_CACHE_DIR):
                os.makedirs(TWENTYFOURSEVEN_CACHE_DIR, exist_ok=True)
            
            # QUICKLY find the first song WITHOUT waiting for full scan
            await interaction.followup.send("🔍 Finding first song to start immediately...")
            loop = asyncio.get_event_loop()
            
            first_song_data = None
            discovered_songs_set = set()
            
            def find_first_song():
                """Blocking: find up to the first 15 songs quickly and seed playback randomly."""
                candidates = []
                for root, dirs, files in os.walk(TWENTYFOURSEVEN_CACHE_DIR):
                    for filename in sorted(files):
                        if filename.lower().endswith(('.mp3', '.wav', '.flac', '.ogg', '.m4a')):
                            file_path = os.path.join(root, filename)
                            
                            # Get metadata
                            title = None
                            duration = "0:00"
                            try:
                                from mutagen import File as MutagenFile
                                meta = MutagenFile(file_path, easy=True)
                                if meta:
                                    if 'title' in meta:
                                        title = ' '.join(meta['title']) if isinstance(meta['title'], list) else meta['title']
                                    # Properly extract duration from mutagen info object
                                    if hasattr(meta, 'info') and hasattr(meta.info, 'length'):
                                        try:
                                            seconds = int(meta.info.length)
                                            mins = seconds // 60
                                            secs = seconds % 60
                                            duration = f"{mins}:{secs:02d}"
                                        except:
                                            pass
                            except:
                                pass
                            
                            if not title:
                                title = os.path.splitext(filename)[0]
                            
                            candidates.append({
                                'title': title,
                                'url': file_path,
                                'duration': duration,
                                'source_type': 'local',
                                'thumbnail': None
                            })

                            if len(candidates) >= 15:
                                return random.choice(candidates)
                return random.choice(candidates) if candidates else None
            
            first_song_data = await loop.run_in_executor(None, find_first_song)
            
            if not first_song_data:
                await interaction.followup.send("❌ No music files found in the music directory!", ephemeral=True)
                return
            
            # Create first song and add to queue
            first_song = Song(
                title=first_song_data['title'],
                url=first_song_data['url'],
                duration=first_song_data['duration'],
                requester=interaction.user,
                source_type=first_song_data['source_type'],
                thumbnail=first_song_data.get('thumbnail')
            )
            player.queue.append(first_song)
            discovered_songs_set.add(first_song_data['url'])
            
            # Enable 24/7 mode NOW
            player.is_247_mode = True
            player.loop_queue = True
            TWENTYFOURSEVEN_ACTIVE_GUILD = interaction.guild.id
            
            # Start playing immediately
            if not interaction.guild.voice_client.is_playing():
                await player.play_next()
            
            await interaction.followup.send(f"▶️ **Now Playing:** {first_song_data['title']}\n🔄 Scanning for more songs (seeded from the first 15 found)...")
            
            # Start background scanning task (non-blocking)
            asyncio.create_task(self.scan_music_directory_for_247_background(player, interaction, discovered_songs_set))
        
        except Exception as e:
            print(f"Error in setup_247_mode: {e}")
            import traceback
            traceback.print_exc()
            try:
                await interaction.followup.send(f"❌ Error setting up 24/7 mode: {e}")
            except:
                pass
    
    @app_commands.command(name="247stop", description="[ADMIN] Stop 24/7 music mode")
    @app_commands.checks.has_permissions(administrator=True)
    async def twentyfourseven_stop(self, interaction: discord.Interaction):
        global TWENTYFOURSEVEN_ACTIVE_GUILD
        
        player = self.get_player(interaction.guild)
        
        if not player.is_247_mode:
            await interaction.response.send_message("⚠️ 24/7 mode is not active!", ephemeral=True)
            return
        
        # Disable 24/7 mode
        player.is_247_mode = False
        player.loop_queue = False
        player.twentyfourseven_songs.clear()
        player.queue.clear()
        TWENTYFOURSEVEN_ACTIVE_GUILD = None
        
        # Stop playback
        if interaction.guild.voice_client:
            interaction.guild.voice_client.stop()
        
        await interaction.response.send_message("⏹️ 24/7 mode stopped!")
    
    @app_commands.command(name="247status", description="Check 24/7 mode status")
    async def twentyfourseven_status(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild)
        
        if player.is_247_mode:
            embed = discord.Embed(
                title="🔁 24/7 Mode: Active",
                color=discord.Color.green()
            )
            embed.add_field(name="Songs in Playlist", value=str(len(player.twentyfourseven_songs)))
            embed.add_field(name="Songs in Queue", value=str(len(player.queue)))
            if interaction.guild.voice_client:
                embed.add_field(name="Channel", value=interaction.guild.voice_client.channel.mention, inline=False)
        else:
            embed = discord.Embed(
                title="⏹️ 24/7 Mode: Inactive",
                description="Use `/247start` to activate (Admin only)",
                color=discord.Color.red()
            )
        
        await interaction.response.send_message(embed=embed)


# Bot setup
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.members = True  # Required for voice connections to work properly

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    logger.info(f"🎵 {bot.user} is online!")
    logger.info(f"📡 Connected to {len(bot.guilds)} server(s)")
    
    # Check Opus status
    if discord.opus.is_loaded():
        logger.info("✅ Opus library is loaded - voice should work")
    else:
        logger.error("❌ Opus library NOT loaded - voice will NOT work!")
    
    # Check PyNaCl for voice encryption
    try:
        import nacl
        logger.info("✅ PyNaCl is installed - voice encryption ready")
    except ImportError:
        logger.error("❌ PyNaCl NOT installed! Voice will NOT work!")
        logger.error("   Install with: pip install PyNaCl")
    
    # Check FFmpeg
    try:
        import shutil
        ffmpeg_path = shutil.which('ffmpeg')
        if ffmpeg_path:
            logger.info(f"✅ FFmpeg found at: {ffmpeg_path}")
        else:
            logger.error("❌ FFmpeg not found in PATH!")
    except Exception as e:
        logger.error(f"Error checking FFmpeg: {e}")
    
    # Check intents
    logger.info(f"Intents - Voice States: {bot.intents.voice_states}, Members: {bot.intents.members}")
    
    try:
        synced = await bot.tree.sync()
        logger.info(f"✅ Synced {len(synced)} command(s)")
    except Exception as e:
        logger.error(f"❌ Failed to sync commands: {e}")
    
    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.listening,
        name="vibinnnn'"
    ))

    # Start state saver / wrapped tasks and resume any saved queue
    cog = bot.get_cog('MusicCog')
    if cog:
        await cog.on_bot_ready()

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
        
    ctx = await bot.get_context(message)
    if ctx.valid:
        await bot.invoke(ctx)

@bot.event
async def on_voice_state_update(member, before, after):
    """Handle voice state updates - disconnect if bot is alone"""
    # Ignore bot state changes
    if member.bot:
        return
    
    voice_client = member.guild.voice_client
    # Only care if bot is connected and member LEFT the bot's channel
    if voice_client and before.channel == voice_client.channel and after.channel != before.channel:
        # Count non-bot members in the voice channel
        human_members = [m for m in voice_client.channel.members if not m.bot]
        
        if len(human_members) == 0:
            logger.info(f"Bot is alone in {voice_client.channel.name}, waiting 30s before disconnecting...")
            await asyncio.sleep(30)
            
            # Recheck if still alone
            if voice_client.is_connected():
                human_members = [m for m in voice_client.channel.members if not m.bot]
                if len(human_members) == 0:
                    logger.info(f"Still alone in {voice_client.channel.name}, disconnecting...")
                    # Clean up player resources
                    cog = bot.get_cog('MusicCog')
                    if cog:
                        player = cog.players.get(member.guild.id)
                        if player:
                            player.cancel_idle_disconnect()
                            player.queue.clear()
                            player.preloaded_sources.clear()
                            player.current = None
                    try:
                        await voice_client.disconnect(force=False)
                        logger.info("✅ Disconnected successfully")
                    except Exception as e:
                        logger.error(f"Error disconnecting: {e}")
                else:
                    logger.info(f"Someone rejoined, staying connected")


async def main():
    async with bot:
        await bot.add_cog(MusicCog(bot))
        try:
            import webui
            await webui.start_web_server(bot)
        except Exception as e:
            logger.error(f"Web UI failed to start (bot will run without it): {e}")
        await bot.start(config.DISCORD_TOKEN)


if __name__ == "__main__":
    if not config.DISCORD_TOKEN:
        print("❌ Error: DISCORD_TOKEN not set in .env file!")
        exit(1)
    
    asyncio.run(main())
