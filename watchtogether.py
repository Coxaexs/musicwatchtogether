"""Watch Together + ReelsTogether for the music bot.

Mounted onto the bot's existing aiohttp app (webui.py) under /watch/:

    /watch/                page: synced video player + chat + queue
    /watch/reels           page: ReelsTogether - synced vertical swipe feed
    /watch/ws              websocket: room sync (chat, play/pause/seek, queue, swipes)
    /watch/media/{file}    downloaded video files (range requests supported)

Rooms are keyed to a Discord channel; /watch and /reels slash commands hand
out a tokenized link (deeppixel.online/watch/?room=...&token=...).

Videos are anything yt-dlp can resolve (YouTube, Shorts, Reels, TikTok,
Twitter/X, ...). They are downloaded to a local cache and served from disk so
every participant streams the exact same file.

ReelsTogether keeps a tiny per-room taste profile: likes and full watches
boost keywords from a clip's title/tags, fast skips decay them, and the next
clips are found by searching YouTube Shorts with the highest-weighted terms.
Only the newest MAX_REELS_CACHE files are kept on disk.
"""

import asyncio
import glob
import json
import logging
import os
import random
import re
import secrets
import time
from collections import deque

import yt_dlp
from aiohttp import web, WSMsgType

import config

try:
    import cobrowser
except Exception as _e:   # missing python-xlib etc. - watch still works
    cobrowser = None
    logging.getLogger('MusicBot.WatchTogether').warning(
        f"co-browser unavailable: {_e}")

logger = logging.getLogger('MusicBot.WatchTogether')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TOKENS_FILE = os.path.join(BASE_DIR, 'watch_tokens.json')
PROFILES_FILE = os.path.join(BASE_DIR, 'reels_profiles.json')
WATCH_CACHE_DIR = os.path.join(BASE_DIR, 'watch_cache')
REELS_CACHE_DIR = os.path.join(BASE_DIR, 'reels_cache')

TOKEN_TTL = 7 * 86400          # links stay valid for a week
MAX_WATCH_CACHE = 10           # downloaded files kept per cache dir
MAX_REELS_CACHE = 10
MAX_HLS_DIRS = 4               # progressive-stream folders kept
CHAT_HISTORY = 100
REELS_READY_AHEAD = 3          # keep this many reels downloaded ahead of the cursor
REELS_MAX_DURATION = 150       # seconds - skip anything longer in the shorts feed
HLS_MIN_DURATION = 8 * 60      # longer than this -> stream while downloading

SETTINGS_FILE = os.path.join(BASE_DIR, 'room_settings.json')
DEFAULT_SETTINGS = {'adblock': True, 'sponsorblock': True, 'quality': 720}
SB_CATEGORIES = ['sponsor', 'selfpromo', 'interaction']

DEFAULT_TOPICS = [
    'funny fails', 'satisfying', 'memes', 'cats', 'dogs', 'football skills',
    'cooking', 'magic tricks', 'parkour', 'gaming moments', 'science facts',
    'street food', 'car', 'basketball', 'animals',
]

STOPWORDS = {
    'the', 'and', 'for', 'with', 'this', 'that', 'you', 'your', 'shorts',
    'short', 'video', 'viral', 'reel', 'reels', 'tiktok', 'youtube', 'when',
    'what', 'how', 'why', 'his', 'her', 'him', 'she', 'they', 'them', 'its',
    'are', 'was', 'has', 'have', 'not', 'but', 'all', 'can', 'out', 'get',
    'got', 'just', 'like', 'from', 'part', 'new', 'best', 'top', 'most',
    'ever', 'omg', 'wow', 'subscribe', 'follow', 'fyp', 'foryou', 'trending',
}

URL_RE = re.compile(r'^https?://', re.IGNORECASE)
YT_ID_RE = re.compile(
    r'(?:youtube\.com/(?:watch\?(?:[^#\s]*&)?v=|shorts/|embed/|live/|v/)|youtu\.be/)'
    r'([A-Za-z0-9_-]{11})')

_search_opts = {'quiet': True, 'no_warnings': True, 'extract_flat': True,
                'nocheckcertificate': True, 'skip_download': True}
_info_opts = {'quiet': True, 'no_warnings': True, 'noplaylist': True,
              'nocheckcertificate': True, 'skip_download': True}


def _dl_opts(cache_dir, vertical=False, quality=720, sponsorblock=False):
    height = 1280 if vertical else quality
    opts = {
        'format': (f'bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]'
                   f'/best[height<={height}][ext=mp4]/best[height<={height}]/best'),
        'outtmpl': os.path.join(cache_dir, '%(id)s.%(ext)s'),
        'merge_output_format': 'mp4',
        'noplaylist': True,
        'quiet': True,
        'noprogress': True,
        'no_warnings': True,
        'restrictfilenames': True,
        'nocheckcertificate': True,
        'concurrent_fragment_downloads': 4,
    }
    if sponsorblock and not vertical:
        opts['postprocessors'] = [
            {'key': 'SponsorBlock', 'categories': SB_CATEGORIES},
            {'key': 'ModifyChapters', 'remove_sponsor_segments': SB_CATEGORIES},
        ]
    return opts


# ---------------------------------------------------------------- persistence

def _load_json(path, default):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default
    except Exception as e:
        logger.warning(f"Could not load {path}: {e}")
        return default


def _save_json(path, data):
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f)
    except Exception as e:
        logger.warning(f"Could not save {path}: {e}")


tokens = _load_json(TOKENS_FILE, {})
profiles = _load_json(PROFILES_FILE, {})
room_settings = _load_json(SETTINGS_FILE, {})


def get_settings(room_id):
    s = dict(DEFAULT_SETTINGS)
    s.update(room_settings.get(room_id, {}))
    return s


def get_room_link(channel_id, channel_name, mode):
    """Used by the /watch and /reels slash commands in music.py."""
    room_id = ('w' if mode == 'watch' else 'r') + str(channel_id)
    now = time.time()
    for k, v in list(tokens.items()):
        if now - v.get('created_at', 0) > TOKEN_TTL:
            tokens.pop(k, None)
    token = None
    for k, v in tokens.items():
        if v.get('room') == room_id:
            token = k
            break
    if not token:
        token = secrets.token_urlsafe(12)
        tokens[token] = {'room': room_id, 'name': channel_name, 'created_at': now}
        _save_json(TOKENS_FILE, tokens)
    base = getattr(config, 'WEB_SERVER_URL', 'https://deeppixel.online').rstrip('/')
    path = '/watch/' if mode == 'watch' else '/watch/reels'
    return f"{base}{path}?room={room_id}&token={token}"


def _token_room(token):
    info = tokens.get(token or '')
    if not info:
        return None
    if time.time() - info.get('created_at', 0) > TOKEN_TTL:
        tokens.pop(token, None)
        _save_json(TOKENS_FILE, tokens)
        return None
    return info


# ---------------------------------------------------------------- room model

class Room:
    def __init__(self, room_id, name):
        self.id = room_id
        self.mode = 'reels' if room_id.startswith('r') else 'watch'
        self.name = name or 'Watch Together'
        self.queue = []            # list of item dicts
        self.index = -1
        self.playing = False
        self.position = 0.0
        self.pos_ts = time.monotonic()
        self.chat = deque(maxlen=CHAT_HISTORY)
        self.sockets = {}          # ws -> {'name': str}
        self.dl_sem = asyncio.Semaphore(2)
        self.reels_task = None
        self.sync_task = None
        self._advanced_past = -1   # 'ended' debounce
        # reels profile
        prof = profiles.get(self.id, {})
        self.interests = prof.get('interests', {})
        self.seen = set(prof.get('seen', []))

    # ---- position clock ----
    def get_position(self):
        if self.playing:
            return self.position + (time.monotonic() - self.pos_ts)
        return self.position

    def set_position(self, pos, playing=None):
        self.position = max(0.0, float(pos))
        self.pos_ts = time.monotonic()
        if playing is not None:
            self.playing = playing

    def current(self):
        if 0 <= self.index < len(self.queue):
            return self.queue[self.index]
        return None

    def save_profile(self):
        profiles[self.id] = {
            'interests': self.interests,
            'seen': list(self.seen)[-500:],
        }
        _save_json(PROFILES_FILE, profiles)


rooms = {}


def _get_room(room_id, name=None):
    room = rooms.get(room_id)
    if not room:
        room = Room(room_id, name)
        rooms[room_id] = room
    elif name and room.name != name:
        room.name = name
    return room


def _item_public(item):
    return {k: item.get(k) for k in
            ('uid', 'vid', 'title', 'duration', 'thumbnail', 'status',
             'progress', 'file', 'added_by', 'uploader', 'likes', 'error',
             'embed_kind', 'embed', 'url', 'live_dl')}


def _set_embed_fallback(item, err=''):
    """Browser mode: when yt-dlp can't download, fall back to embedding."""
    url = item.get('url') or ''
    if URL_RE.match(url):
        m = YT_ID_RE.search(url)
        if m:
            item['embed_kind'] = 'youtube'
            item['embed'] = m.group(1)
        else:
            item['embed_kind'] = 'iframe'
            item['embed'] = url
        item['status'] = 'embed'
        item['error'] = str(err)[:200]
    else:
        item['status'] = 'error'
        item['error'] = str(err or 'No results found')[:200]


def _room_state(room):
    if room.mode == 'reels':
        lo = max(0, room.index - 1)
        items = room.queue[lo:room.index + REELS_READY_AHEAD + 2]
        offset = lo
    else:
        items = room.queue
        offset = 0
    return {
        't': 'state',
        'room': room.id, 'name': room.name, 'mode': room.mode,
        'index': room.index, 'offset': offset,
        'playing': room.playing, 'position': round(room.get_position(), 2),
        'queue': [_item_public(i) for i in items],
        'total': len(room.queue),
        'participants': sorted({m['name'] for m in room.sockets.values()}),
        'chat': list(room.chat)[-50:],
        'need_seed': room.mode == 'reels' and not room.interests and not room.queue,
        'browser': (cobrowser.get(room.id).public_state()
                    if cobrowser and cobrowser.get(room.id) else {'active': False}),
        'settings': get_settings(room.id),
    }


async def _broadcast(room, payload):
    dead = []
    msg = json.dumps(payload)
    for ws in list(room.sockets):
        try:
            await ws.send_str(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        room.sockets.pop(ws, None)


async def _broadcast_state(room):
    await _broadcast(room, _room_state(room))


async def _notice(room, text):
    entry = {'name': None, 'text': text, 'ts': time.time()}
    room.chat.append(entry)
    await _broadcast(room, dict(entry, t='chat'))


# ---------------------------------------------------------------- yt-dlp

def _blocking_extract(query):
    q = query if URL_RE.match(query) else f'ytsearch1:{query}'
    with yt_dlp.YoutubeDL(_info_opts) as ydl:
        info = ydl.extract_info(q, download=False)
    if info and info.get('entries') is not None:
        entries = [e for e in info['entries'] if e]
        info = entries[0] if entries else None
    return info


def _blocking_search(query, limit=10):
    with yt_dlp.YoutubeDL(_search_opts) as ydl:
        info = ydl.extract_info(f'ytsearch{limit}:{query}', download=False)
    return [e for e in (info.get('entries') or []) if e] if info else []


def _blocking_hls_formats(url, quality):
    """Resolve direct stream URLs (+headers) for the progressive HLS pipeline."""
    opts = dict(_info_opts)
    opts['format'] = (
        f'bestvideo[height<={quality}][vcodec^=avc1]+bestaudio[ext=m4a]'
        f'/bestvideo[height<={quality}]+bestaudio'
        f'/best[height<={quality}]/best')
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if info and info.get('entries') is not None:
        entries = [e for e in info['entries'] if e]
        info = entries[0] if entries else None
    if not info:
        raise RuntimeError('no formats found')
    reqs = info.get('requested_formats') or [info]
    inputs = [{'url': f['url'], 'headers': f.get('http_headers') or {}}
              for f in reqs if f.get('url')]
    if not inputs:
        raise RuntimeError('no stream url')
    vcodec = (reqs[0].get('vcodec') or '')
    acodec = (reqs[-1].get('acodec') or '')
    return {
        'inputs': inputs,
        'v_copy': vcodec.startswith(('avc1', 'h264')),
        'a_copy': acodec.startswith(('mp4a', 'aac')),
    }


def _blocking_download(url, cache_dir, vertical, progress_cb,
                       quality=720, sponsorblock=False):
    opts = _dl_opts(cache_dir, vertical, quality, sponsorblock)
    if progress_cb:
        last = {'t': 0.0}

        def hook(d):
            if d.get('status') == 'downloading':
                now = time.time()
                if now - last['t'] >= 1.0:
                    last['t'] = now
                    total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                    done = d.get('downloaded_bytes') or 0
                    if total:
                        progress_cb(int(done * 100 / total))
        opts['progress_hooks'] = [hook]
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    vid = info.get('id')
    files = sorted(glob.glob(os.path.join(cache_dir, f'{vid}.*')),
                   key=os.path.getmtime, reverse=True)
    files = [f for f in files if not f.endswith(('.part', '.ytdl'))]
    if not files:
        raise RuntimeError('download produced no file')
    return os.path.basename(files[0]), info


def _trim_cache(cache_dir, keep, protected):
    try:
        files = [f for f in glob.glob(os.path.join(cache_dir, '*.*'))
                 if not f.endswith(('.part', '.ytdl'))]
        files.sort(key=os.path.getmtime)
        removable = [f for f in files if os.path.basename(f) not in protected]
        excess = len(files) - keep
        for f in removable:
            if excess <= 0:
                break
            try:
                os.remove(f)
                excess -= 1
            except OSError:
                pass
    except Exception as e:
        logger.warning(f"cache trim failed: {e}")


def _protected_files():
    keep = set()
    for room in rooms.values():
        lo = max(0, room.index - 1)
        for item in room.queue[lo:room.index + REELS_READY_AHEAD + 3]:
            if item.get('file'):
                keep.add(item['file'])
    return keep


# ---------------------------------------------------------------- downloads

async def _download_item(room, item):
    cache_dir = REELS_CACHE_DIR if room.mode == 'reels' else WATCH_CACHE_DIR
    loop = asyncio.get_running_loop()
    async with room.dl_sem:
        if item.get('status') in ('ready', 'error'):
            return
        # cache hit?
        vid = item.get('vid')
        if vid:
            hit = [f for f in glob.glob(os.path.join(cache_dir, f'{vid}.*'))
                   if not f.endswith(('.part', '.ytdl'))]
            if hit:
                item['file'] = os.path.basename(hit[0])
                item['status'] = 'ready'
                os.utime(hit[0], None)  # refresh mtime so trim keeps it
                await _after_ready(room, item)
                return
        item['status'] = 'downloading'
        item['progress'] = 0
        await _broadcast_state(room)

        def progress_cb(pct):
            item['progress'] = pct
            loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(
                    _broadcast(room, {'t': 'progress', 'uid': item['uid'], 'pct': pct})))

        cfg = get_settings(room.id)
        try:
            try:
                fname, info = await loop.run_in_executor(
                    None, _blocking_download, item['url'], cache_dir,
                    room.mode == 'reels', progress_cb,
                    cfg['quality'], cfg['sponsorblock'])
            except Exception:
                if not cfg['sponsorblock'] or room.mode == 'reels':
                    raise
                # SponsorBlock post-processing can fail on its own; retry clean
                fname, info = await loop.run_in_executor(
                    None, _blocking_download, item['url'], cache_dir,
                    False, progress_cb, cfg['quality'], False)
            item['file'] = fname
            item['vid'] = info.get('id') or item.get('vid')
            item['title'] = info.get('title') or item.get('title')
            item['duration'] = info.get('duration') or item.get('duration')
            item['uploader'] = info.get('uploader') or item.get('uploader')
            if info.get('thumbnail'):
                item['thumbnail'] = info['thumbnail']
            item['tags'] = (info.get('tags') or [])[:10]
            item['status'] = 'ready'
        except Exception as e:
            logger.error(f"watch download failed for {item.get('url')}: {e}")
            _set_embed_fallback(item, e)
        _trim_cache(cache_dir,
                    MAX_REELS_CACHE if room.mode == 'reels' else MAX_WATCH_CACHE,
                    _protected_files())
        await _after_ready(room, item)


async def _hls_item(room, item):
    """Stream-while-downloading: ffmpeg pulls the source and writes an HLS
    playlist that becomes playable after a few segments, long before the
    whole video is on disk."""
    cfg = get_settings(room.id)
    loop = asyncio.get_running_loop()
    item['status'] = 'downloading'
    item['progress'] = 0
    item['live_dl'] = True
    await _broadcast_state(room)
    try:
        fmt = await loop.run_in_executor(
            None, _blocking_hls_formats, item['url'], cfg['quality'])
    except Exception as e:
        logger.error(f"hls format resolve failed for {item.get('url')}: {e}")
        _set_embed_fallback(item, e)
        await _after_ready(room, item)
        return

    out_dir = os.path.join(WATCH_CACHE_DIR, 'hls', item['uid'])
    os.makedirs(out_dir, exist_ok=True)
    playlist = os.path.join(out_dir, 'index.m3u8')
    cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error']
    for inp in fmt['inputs']:
        hdrs = ''.join(f'{k}: {v}\r\n' for k, v in inp['headers'].items())
        if hdrs:
            cmd += ['-headers', hdrs]
        cmd += ['-i', inp['url']]
    if len(fmt['inputs']) == 2:
        cmd += ['-map', '0:v:0', '-map', '1:a:0']
    if fmt['v_copy']:
        cmd += ['-c:v', 'copy']
    else:   # rare: source isn't h264, transcode in real time
        cmd += ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23',
                '-maxrate', '4M', '-bufsize', '8M']
    cmd += ['-c:a', 'copy'] if fmt['a_copy'] else ['-c:a', 'aac', '-b:a', '128k']
    cmd += ['-f', 'hls', '-hls_time', '4', '-hls_playlist_type', 'event',
            '-hls_segment_filename', os.path.join(out_dir, 's%06d.ts'),
            playlist]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL, start_new_session=True)

    def seg_count():
        try:
            with open(playlist, 'r') as f:
                return f.read().count('#EXTINF')
        except OSError:
            return 0

    # playable once a couple of segments exist
    for _ in range(120):
        if proc.returncode is not None or seg_count() >= 2:
            break
        await asyncio.sleep(1)
    if seg_count() < 2:
        try:
            os.killpg(os.getpgid(proc.pid), 15)
        except (ProcessLookupError, PermissionError):
            pass
        _set_embed_fallback(item, 'stream pipeline failed')
        await _after_ready(room, item)
        return
    item['file'] = f'hls/{item["uid"]}/index.m3u8'
    item['status'] = 'ready'
    await _after_ready(room, item)

    dur = item.get('duration') or 0
    while proc.returncode is None:
        await asyncio.sleep(3)
        if dur:
            pct = min(99, int(seg_count() * 4 * 100 / dur))
            if pct != item.get('progress'):
                item['progress'] = pct
                await _broadcast(room, {'t': 'progress', 'uid': item['uid'],
                                        'pct': pct})
    item['live_dl'] = False
    item['progress'] = 100
    # make sure players see a finished VOD playlist even if ffmpeg died early
    try:
        with open(playlist, 'r+') as f:
            txt = f.read()
            if '#EXT-X-ENDLIST' not in txt:
                f.write('\n#EXT-X-ENDLIST\n')
    except OSError:
        pass
    _trim_hls()
    await _broadcast_state(room)


def _trim_hls():
    root = os.path.join(WATCH_CACHE_DIR, 'hls')
    if not os.path.isdir(root):
        return
    protected = set()
    for room in rooms.values():
        lo = max(0, room.index - 1)
        for it in room.queue[lo:room.index + REELS_READY_AHEAD + 3]:
            protected.add(it['uid'])
    try:
        dirs = sorted((os.path.join(root, d) for d in os.listdir(root)),
                      key=os.path.getmtime)
        excess = len(dirs) - MAX_HLS_DIRS
        for d in dirs:
            if excess <= 0:
                break
            if os.path.basename(d) in protected:
                continue
            import shutil
            shutil.rmtree(d, ignore_errors=True)
            excess -= 1
    except Exception as e:
        logger.warning(f"hls trim failed: {e}")


async def _after_ready(room, item):
    # auto-start when nothing is playing yet and this item became playable
    if item['status'] in ('ready', 'embed') and room.current() is None:
        try:
            room.index = room.queue.index(item)
            room.set_position(0, playing=True)
        except ValueError:
            pass
    # reels: if the cursor is parked on a not-yet-ready item, this state
    # broadcast delivers the file so clients can start it
    await _broadcast_state(room)


async def _add_query(room, query, added_by, play_next=False):
    item = {
        'uid': secrets.token_hex(6), 'vid': None, 'url': query,
        'title': query if URL_RE.match(query) else f'🔎 {query}',
        'duration': None, 'thumbnail': None, 'uploader': None,
        'status': 'pending', 'progress': 0, 'file': None,
        'added_by': added_by, 'likes': 0, 'tags': [],
    }
    if play_next and 0 <= room.index < len(room.queue):
        room.queue.insert(room.index + 1, item)
    else:
        room.queue.append(item)
    await _broadcast_state(room)
    loop = asyncio.get_running_loop()
    try:
        info = await loop.run_in_executor(None, _blocking_extract, query)
    except Exception as e:
        _set_embed_fallback(item, e)
        await _after_ready(room, item)
        return
    if not info:
        _set_embed_fallback(item, 'No results found')
        await _after_ready(room, item)
        return
    item['vid'] = info.get('id')
    item['url'] = info.get('webpage_url') or info.get('url') or query
    item['title'] = info.get('title') or item['title']
    item['duration'] = info.get('duration')
    item['thumbnail'] = info.get('thumbnail')
    item['uploader'] = info.get('uploader')
    item['tags'] = (info.get('tags') or [])[:10]
    await _broadcast_state(room)
    if room.mode == 'watch' and (info.get('duration') or 0) >= HLS_MIN_DURATION:
        asyncio.create_task(_hls_item(room, item))
    else:
        asyncio.create_task(_download_item(room, item))


# ---------------------------------------------------------------- reels brain

def _tokenize(item):
    words = re.findall(r'[a-zA-ZğüşöçıİĞÜŞÖÇ0-9]{3,}', (item.get('title') or '').lower())
    terms = [w for w in words if w not in STOPWORDS][:6]
    for tag in (item.get('tags') or [])[:4]:
        tag = tag.lower().strip()
        if tag and tag not in STOPWORDS and len(tag) >= 3:
            terms.append(tag)
    return terms


def _learn(room, item, delta):
    if not item:
        return
    for term in _tokenize(item):
        w = room.interests.get(term, 0) + delta
        room.interests[term] = max(-5, min(50, w))
    # keep the profile small: drop the weakest terms
    if len(room.interests) > 80:
        for term, _w in sorted(room.interests.items(), key=lambda kv: kv[1])[:20]:
            room.interests.pop(term, None)
    room.save_profile()


def _pick_query(room):
    positive = {k: v for k, v in room.interests.items() if v > 0}
    if not positive or random.random() < 0.2:   # explore 20% of the time
        return random.choice(DEFAULT_TOPICS)
    terms = random.choices(list(positive), weights=list(positive.values()),
                           k=min(2, len(positive)))
    return ' '.join(dict.fromkeys(terms))


async def _reels_topup(room):
    ahead = len(room.queue) - (room.index + 1)
    if ahead >= REELS_READY_AHEAD:
        return
    loop = asyncio.get_running_loop()
    query = _pick_query(room)
    try:
        entries = await loop.run_in_executor(
            None, _blocking_search, f'{query} #shorts', 10)
    except Exception as e:
        logger.warning(f"reels search failed ({query!r}): {e}")
        return
    added = 0
    for e in entries:
        if added >= 2 or len(room.queue) - (room.index + 1) >= REELS_READY_AHEAD:
            break
        vid = e.get('id')
        dur = e.get('duration')
        if not vid or vid in room.seen:
            continue
        if dur and dur > REELS_MAX_DURATION:
            continue
        room.seen.add(vid)
        item = {
            'uid': secrets.token_hex(6), 'vid': vid,
            'url': e.get('url') or f'https://www.youtube.com/watch?v={vid}',
            'title': e.get('title') or 'Reel', 'duration': dur,
            'thumbnail': (e.get('thumbnails') or [{}])[-1].get('url'),
            'uploader': e.get('uploader') or e.get('channel'),
            'status': 'pending', 'progress': 0, 'file': None,
            'added_by': '✨ feed', 'likes': 0, 'tags': [],
        }
        room.queue.append(item)
        added += 1
        asyncio.create_task(_download_item(room, item))
    if added:
        room.save_profile()
        await _broadcast_state(room)


async def _reels_loop(room):
    try:
        while room.sockets:
            try:
                await _reels_topup(room)
            except Exception as e:
                logger.error(f"reels loop error: {e}", exc_info=True)
            await asyncio.sleep(4)
    finally:
        room.reels_task = None


async def _sync_loop(room):
    try:
        while room.sockets:
            await asyncio.sleep(5)
            await _broadcast(room, {
                't': 'sync', 'index': room.index,
                'playing': room.playing,
                'position': round(room.get_position(), 2),
            })
    finally:
        room.sync_task = None


# ---------------------------------------------------------------- ws handler

async def ws_handler(request):
    room_id = request.query.get('room', '')
    token = request.query.get('token', '')
    info = _token_room(token)
    if not info or info['room'] != room_id:
        raise web.HTTPUnauthorized(text='bad token')

    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    room = _get_room(room_id, info.get('name'))
    name = 'Guest'
    joined = False

    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except json.JSONDecodeError:
                continue
            t = data.get('t')

            if t == 'join':
                name = str(data.get('name') or 'Guest')[:24].strip() or 'Guest'
                room.sockets[ws] = {'name': name}
                joined = True
                if room.sync_task is None:
                    room.sync_task = asyncio.create_task(_sync_loop(room))
                if room.mode == 'reels' and room.reels_task is None and room.interests:
                    room.reels_task = asyncio.create_task(_reels_loop(room))
                await ws.send_str(json.dumps(_room_state(room)))
                await _notice(room, f'👋 {name} joined')
                await _broadcast_state(room)
                continue

            if not joined:
                continue

            if t == 'chat':
                text = str(data.get('text') or '')[:500].strip()
                if text:
                    entry = {'name': name, 'text': text, 'ts': time.time()}
                    room.chat.append(entry)
                    await _broadcast(room, dict(entry, t='chat'))

            elif t == 'play':
                sess = cobrowser.get(room.id) if cobrowser else None
                if sess:   # resuming the video ends browser mode
                    await sess.stop()
                room.set_position(data.get('pos', room.get_position()), playing=True)
                await _broadcast(room, {'t': 'sync', 'index': room.index,
                                        'playing': True,
                                        'position': round(room.position, 2),
                                        'by': name, 'action': 'play'})

            elif t == 'pause':
                room.set_position(data.get('pos', room.get_position()), playing=False)
                await _broadcast(room, {'t': 'sync', 'index': room.index,
                                        'playing': False,
                                        'position': round(room.position, 2),
                                        'by': name, 'action': 'pause'})

            elif t == 'seek':
                room.set_position(data.get('pos', 0))
                await _broadcast(room, {'t': 'sync', 'index': room.index,
                                        'playing': room.playing,
                                        'position': round(room.position, 2),
                                        'by': name, 'action': 'seek'})

            elif t == 'add':
                q = str(data.get('q') or '').strip()
                if q:
                    await _notice(room, f'➕ {name} added: {q[:80]}')
                    asyncio.create_task(_add_query(room, q, name,
                                                   bool(data.get('next'))))

            elif t == 'jump':
                idx = int(data.get('index', -1))
                if 0 <= idx < len(room.queue):
                    sess = cobrowser.get(room.id) if cobrowser else None
                    if sess:
                        await sess.stop()
                    room.index = idx
                    room._advanced_past = idx - 1
                    room.set_position(0, playing=True)
                    await _broadcast_state(room)

            elif t == 'skip':
                if room.index + 1 < len(room.queue):
                    room.index += 1
                    room.set_position(0, playing=True)
                else:
                    room.set_position(room.get_position(), playing=False)
                room._advanced_past = room.index - 1
                await _notice(room, f'⏭ {name} skipped')
                await _broadcast_state(room)

            elif t == 'remove':
                idx = int(data.get('index', -1))
                if 0 <= idx < len(room.queue) and idx != room.index:
                    room.queue.pop(idx)
                    if idx < room.index:
                        room.index -= 1
                    await _broadcast_state(room)

            elif t == 'ended':
                idx = int(data.get('index', -1))
                if idx == room.index and idx > room._advanced_past:
                    room._advanced_past = idx
                    if room.mode == 'watch':
                        if room.index + 1 < len(room.queue):
                            room.index += 1
                            room.set_position(0, playing=True)
                        else:
                            room.set_position(0, playing=False)
                        await _broadcast_state(room)

            elif t == 'seed' and room.mode == 'reels':
                text = str(data.get('text') or '').strip()[:120]
                if text:
                    for term in re.findall(r'[^\s,]{3,}', text.lower()):
                        if term not in STOPWORDS:
                            room.interests[term] = max(
                                room.interests.get(term, 0), 5)
                    room.save_profile()
                    await _notice(room, f'✨ {name} seeded the feed: {text}')
                    if room.reels_task is None:
                        room.reels_task = asyncio.create_task(_reels_loop(room))
                    await _broadcast_state(room)

            elif t == 'swipe' and room.mode == 'reels':
                cur = room.current()
                watched = float(data.get('watched') or 0)
                dur = float(data.get('dur') or 0) or (cur or {}).get('duration') or 0
                if cur and dur:
                    ratio = watched / dur
                    if ratio < 0.35:
                        _learn(room, cur, -1)
                    elif ratio >= 0.9:
                        _learn(room, cur, 1)
                if room.index + 1 < len(room.queue):
                    room.index += 1
                    room._advanced_past = room.index - 1
                    room.set_position(0, playing=True)
                await _broadcast_state(room)

            elif t == 'like' and room.mode == 'reels':
                cur = room.current()
                if cur:
                    cur['likes'] = cur.get('likes', 0) + 1
                    _learn(room, cur, 2)
                    await _broadcast(room, {'t': 'heart', 'by': name,
                                            'uid': cur['uid'],
                                            'likes': cur['likes']})

            # ---- shared co-browser ----
            elif t == 'browser_start':
                if not cobrowser:
                    await _notice(room, '❌ Browser mode is not available on this server')
                    continue
                url = str(data.get('url') or '').strip()[:500]
                if cobrowser.get(room.id):
                    if url:
                        await cobrowser.get(room.id).navigate(url)
                    continue
                room.set_position(room.get_position(), playing=False)
                await _notice(room, f'🌐 {name} is starting the shared browser…')
                await _broadcast_state(room)

                async def on_change():
                    await _broadcast_state(room)
                try:
                    await cobrowser.start(room.id, url, started_by=name,
                                          on_change=on_change,
                                          adblock=get_settings(room.id)['adblock'])
                except Exception as e:
                    await _notice(room, f'❌ Browser failed to start: {e}')
                await _broadcast_state(room)

            elif t == 'settings':
                cfg = get_settings(room.id)
                changed = []
                for k in ('adblock', 'sponsorblock'):
                    if k in data and bool(data[k]) != cfg[k]:
                        cfg[k] = bool(data[k])
                        changed.append(f"{k} {'on' if cfg[k] else 'off'}")
                if 'quality' in data:
                    try:
                        q = int(data['quality'])
                    except (TypeError, ValueError):
                        q = 0
                    if q in (360, 480, 720, 1080) and q != cfg['quality']:
                        cfg['quality'] = q
                        changed.append(f'quality {q}p')
                if changed:
                    room_settings[room.id] = cfg
                    _save_json(SETTINGS_FILE, room_settings)
                    await _notice(room, f'⚙️ {name} set ' + ', '.join(changed))
                    await _broadcast_state(room)

            elif t == 'browser_stop':
                sess = cobrowser.get(room.id) if cobrowser else None
                if sess:
                    await _notice(room, f'🌐 {name} closed the shared browser')
                    await sess.stop()

            elif t == 'browser_control':
                sess = cobrowser.get(room.id) if cobrowser else None
                if sess:
                    sess.controller = name
                    await _notice(room, f'🖱️ {name} took control of the browser')
                    await _broadcast_state(room)

            elif t == 'browser_nav':
                sess = cobrowser.get(room.id) if cobrowser else None
                if sess and sess.controller == name:
                    url = str(data.get('url') or '').strip()[:500]
                    if url:
                        await sess.navigate(url)

            elif t == 'bmouse':
                sess = cobrowser.get(room.id) if cobrowser else None
                if sess and sess.controller == name:
                    a = data.get('a', 'move')
                    x = float(data.get('x') or 0)
                    y = float(data.get('y') or 0)
                    sess.mouse(a, x, y, int(data.get('b') or 1),
                               float(data.get('dy') or 0))
                    # so everyone sees where the controller is pointing
                    if a in ('move', 'down', 'up'):
                        await _broadcast(room, {'t': 'bcursor', 'x': x, 'y': y,
                                                'down': a == 'down'})

            elif t == 'bkey':
                sess = cobrowser.get(room.id) if cobrowser else None
                if sess and sess.controller == name:
                    sess.key(data.get('a', 'down'), str(data.get('key') or ''))

            elif t == 'browser_media':
                sess = cobrowser.get(room.id) if cobrowser else None
                if sess:
                    await ws.send_str(json.dumps(
                        {'t': 'bmedia', 'items': sess.get_media(),
                         'page': sess.page_url}))

            elif t == 'browser_pick':
                sess = cobrowser.get(room.id) if cobrowser else None
                url = str(data.get('url') or '').strip()
                if sess and url:
                    await _notice(room, f'📹 {name} grabbed media from the page')
                    asyncio.create_task(_add_query(room, url, name))
    finally:
        room.sockets.pop(ws, None)
        if joined:
            await _notice(room, f'💨 {name} left')
            await _broadcast_state(room)
    return ws


# ---------------------------------------------------------------- http routes

async def watch_page(request):
    return web.Response(text=WATCH_HTML, content_type='text/html')


async def reels_page(request):
    return web.Response(text=REELS_HTML, content_type='text/html')


async def api_create(request):
    """Standalone rooms: anyone on the landing page can create one."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    mode = 'reels' if body.get('mode') == 'reels' else 'watch'
    name = str(body.get('name') or '').strip()[:40] or \
        ('📱 Reels Party' if mode == 'reels' else '🍿 Watch Party')
    room_id = ('r' if mode == 'reels' else 'w') + 'p' + secrets.token_hex(5)
    token = secrets.token_urlsafe(12)
    tokens[token] = {'room': room_id, 'name': name, 'created_at': time.time()}
    _save_json(TOKENS_FILE, tokens)
    base = getattr(config, 'WEB_SERVER_URL', 'https://deeppixel.online').rstrip('/')
    path = '/watch/' if mode == 'watch' else '/watch/reels'
    return web.json_response({'url': f'{base}{path}?room={room_id}&token={token}'})


async def media(request):
    # HLS segment requests are playlist-relative and carry no query string,
    # so the page also stores the token in a cookie
    token = request.query.get('token') or request.cookies.get('wt_token', '')
    if not _token_room(token):
        raise web.HTTPUnauthorized(text='bad token')
    rel = request.match_info['file']
    for d in (WATCH_CACHE_DIR, REELS_CACHE_DIR):
        root = os.path.realpath(d)
        path = os.path.realpath(os.path.join(d, rel))
        if not path.startswith(root + os.sep):
            continue
        if os.path.isfile(path):
            if path.endswith('.m3u8'):
                with open(path, 'r', encoding='utf-8') as f:
                    return web.Response(
                        text=f.read(),
                        content_type='application/vnd.apple.mpegurl',
                        headers={'Cache-Control': 'no-cache'})
            return web.FileResponse(path)
    raise web.HTTPNotFound()


async def static_file(request):
    fname = os.path.basename(request.match_info['file'])
    path = os.path.join(BASE_DIR, 'webstatic', fname)
    if os.path.isfile(path):
        return web.FileResponse(path)
    raise web.HTTPNotFound()


async def stream_ws(request):
    """Binary MPEG-TS stream of the room's shared browser."""
    room_id = request.query.get('room', '')
    token = request.query.get('token', '')
    info = _token_room(token)
    if not info or info['room'] != room_id:
        raise web.HTTPUnauthorized(text='bad token')
    sess = cobrowser.get(room_id) if cobrowser else None
    if not sess:
        raise web.HTTPNotFound(text='no browser session')
    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=0)
    await ws.prepare(request)
    q = sess.add_viewer(ws)
    try:
        while True:
            chunk = await q.get()
            if chunk is None:   # session ended
                break
            await ws.send_bytes(chunk)
    except (ConnectionResetError, asyncio.CancelledError, Exception):
        pass
    finally:
        sess.remove_viewer(ws)
    return ws


def setup(app, bot=None):
    import shutil
    if cobrowser:
        cobrowser.cleanup_orphans()   # kill co-browsers leaked by a prior run
    os.makedirs(WATCH_CACHE_DIR, exist_ok=True)
    os.makedirs(REELS_CACHE_DIR, exist_ok=True)
    # queues don't survive restarts, so leftover files are orphans
    for d in (WATCH_CACHE_DIR, REELS_CACHE_DIR):
        for f in glob.glob(os.path.join(d, '*')):
            try:
                if os.path.isdir(f):
                    shutil.rmtree(f, ignore_errors=True)
                else:
                    os.remove(f)
            except OSError:
                pass
    app.router.add_get('/watch/', watch_page)
    app.router.add_get('/watch/reels', reels_page)
    app.router.add_get('/watch/ws', ws_handler)
    app.router.add_get('/watch/stream', stream_ws)
    app.router.add_get('/watch/static/{file}', static_file)
    app.router.add_post('/watch/api/create', api_create)
    app.router.add_get('/watch/media/{file:.+}', media)
    logger.info("🎬 Watch Together mounted at /watch/")


# ================================================================ WATCH PAGE

WATCH_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DeepPixel Watch Together</title>
<script src="static/jsmpeg.min.js"></script>
<script src="static/hls.min.js"></script>
<style>
  :root {
    --bg:#0e0e16; --panel:#1c1c28; --panel2:#24243a; --text:#e8e8f0;
    --muted:#9a9ab0; --accent:#7c6cf0; --accent2:#4ec9a0; --danger:#e06c75;
    --radius:12px;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
       font-family:system-ui,-apple-system,'Segoe UI',sans-serif;}
  .top{display:flex;align-items:center;gap:10px;padding:12px 16px;}
  .top h1{font-size:17px;margin:0;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .top .who{display:flex;gap:6px;flex-wrap:wrap}
  .chip{background:var(--panel2);border-radius:999px;padding:3px 10px;font-size:12px;color:var(--muted)}
  .chip.me{color:var(--accent2)}
  .layout{display:grid;grid-template-columns:1fr 320px;gap:14px;padding:0 16px 16px;max-width:1400px;margin:0 auto}
  @media(max-width:900px){.layout{grid-template-columns:1fr}}
  .stage{background:#000;border-radius:var(--radius);overflow:hidden;position:relative;aspect-ratio:16/9}
  .stage video{width:100%;height:100%;background:#000}
  .stage:fullscreen{aspect-ratio:auto;border-radius:0}
  .stage:fullscreen video,.stage:fullscreen #bwrap{width:100%;height:100%}
  #fsBtn{position:absolute;top:10px;right:10px;z-index:6;background:rgba(0,0,0,.5);
         border-radius:8px;padding:5px 10px;font-size:17px;opacity:.35;transition:opacity .2s}
  #fsBtn:hover{opacity:1}
  #bcursor{position:absolute;width:22px;height:22px;left:0;top:0;z-index:7;
    pointer-events:none;transform:translate(-3px,-2px);transition:left .05s linear,top .05s linear;
    display:none;filter:drop-shadow(0 1px 2px rgba(0,0,0,.6))}
  #bcursor svg{width:100%;height:100%}
  #bcursor.click{animation:bclick .4s ease-out}
  @keyframes bclick{0%{filter:drop-shadow(0 0 0 var(--accent2))}
    50%{filter:drop-shadow(0 0 8px var(--accent2))}100%{filter:none}}
  #bcanvas.controlling{cursor:none}
  .mediaitem{display:flex;align-items:center;gap:8px;padding:7px 4px;border-bottom:1px solid #2a2a40;font-size:13px}
  .mediaitem:last-child{border-bottom:none}
  .mediaitem .mi-name{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .mediaitem .mi-kind{color:var(--muted);font-size:11px;text-transform:uppercase}
  .overlay{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
           flex-direction:column;gap:10px;background:rgba(5,5,10,.75);cursor:pointer;z-index:5;
           font-size:16px;text-align:center;padding:20px}
  .overlay .big{font-size:42px}
  .hidden{display:none!important}
  .card{background:var(--panel);border-radius:var(--radius);padding:14px;margin-top:14px}
  .addrow{display:flex;gap:8px}
  .addrow input{flex:1;background:var(--panel2);border:1px solid #333350;color:var(--text);
                border-radius:8px;padding:10px 12px;font-size:14px}
  .addrow input:focus{outline:1px solid var(--accent)}
  button{background:var(--panel2);color:var(--text);border:none;border-radius:8px;
         padding:9px 14px;cursor:pointer;font-size:14px}
  button:hover{filter:brightness(1.2)}
  button.primary{background:var(--accent)}
  .qhead{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px}
  .qhead h2{font-size:14px;margin:0;color:var(--muted)}
  .qitem{display:flex;align-items:center;gap:10px;padding:8px 6px;border-bottom:1px solid #2a2a40;
         font-size:14px;border-radius:6px}
  .qitem:last-child{border-bottom:none}
  .qitem.now{background:var(--panel2)}
  .qitem img{width:56px;height:32px;object-fit:cover;border-radius:4px;background:var(--panel2)}
  .qitem .t{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:pointer}
  .qitem .s{color:var(--muted);font-size:12px;white-space:nowrap}
  .qitem .x{color:var(--danger);cursor:pointer;padding:2px 6px}
  .chatbox{background:var(--panel);border-radius:var(--radius);display:flex;flex-direction:column;
           height:calc(100vh - 120px);min-height:420px;position:sticky;top:12px}
  @media(max-width:900px){.chatbox{height:380px;position:static}}
  .chatbox h2{font-size:14px;margin:0;padding:12px 14px;color:var(--muted);border-bottom:1px solid #2a2a40}
  .msgs{flex:1;overflow-y:auto;padding:10px 14px;display:flex;flex-direction:column;gap:6px}
  .m{font-size:14px;line-height:1.35;word-break:break-word}
  .m .n{color:var(--accent2);font-weight:600}
  .m.sys{color:var(--muted);font-size:12.5px;font-style:italic}
  .chatrow{display:flex;gap:8px;padding:10px;border-top:1px solid #2a2a40}
  .chatrow input{flex:1;background:var(--panel2);border:1px solid #333350;color:var(--text);
                 border-radius:8px;padding:9px 12px;font-size:14px}
  .toast{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);background:var(--panel2);
         border:1px solid var(--accent);padding:9px 16px;border-radius:10px;font-size:14px;
         opacity:0;transition:opacity .3s;pointer-events:none;z-index:50}
  .toast.show{opacity:1}
  .modal{position:fixed;inset:0;background:rgba(8,8,14,.94);display:flex;align-items:center;
         justify-content:center;z-index:40}
  .modal .box{background:var(--panel);padding:26px;border-radius:var(--radius);width:320px;text-align:center}
  .modal input{width:100%;margin:14px 0;background:var(--panel2);border:1px solid #333350;
               color:var(--text);border-radius:8px;padding:11px;font-size:15px;text-align:center}
  .dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--danger);margin-right:6px}
  .dot.on{background:var(--accent2)}
  .empty{color:var(--muted);text-align:center;padding:16px 0;font-size:14px}
  .setrow{display:flex;justify-content:space-between;align-items:center;margin:14px 0;gap:10px;font-size:14px}
  .setrow small{color:var(--muted);display:block}
  .setrow input[type=checkbox]{width:18px;height:18px;accent-color:var(--accent)}
  .setrow select{background:var(--panel2);color:var(--text);border:1px solid #333350;border-radius:6px;padding:6px}
</style>
</head>
<body>
<div class="top">
  <h1><span class="dot" id="dot"></span>🎬 <span id="roomName">Watch Together</span></h1>
  <div class="who" id="who"></div>
  <button class="chip" style="cursor:pointer;border:1px solid var(--accent)" onclick="copyLink()">🔗 invite</button>
  <button class="chip" style="cursor:pointer" onclick="openSettings()">⚙️</button>
</div>
<div class="layout">
  <div>
    <div class="stage">
      <video id="v" controls playsinline></video>
      <div id="bwrap" class="hidden" style="position:absolute;inset:0;background:#000">
        <canvas id="bcanvas" tabindex="0"
                style="width:100%;height:100%;display:block;outline:none"></canvas>
        <div id="bcursor"><svg viewBox="0 0 24 24"><path d="M4 2 L4 20 L9 15 L12.5 22 L15 21 L11.5 14 L18 14 Z" fill="#fff" stroke="#222" stroke-width="1.2"/></svg></div>
      </div>
      <div class="overlay hidden" id="failOvl">
        <div class="big">🚫</div>
        <div id="failText">This one couldn't be downloaded.</div>
        <div style="display:flex;gap:10px;flex-wrap:wrap;justify-content:center">
          <button class="primary" onclick="openInBrowser()">🌐 Open in shared browser</button>
          <button onclick="send({t:'skip'})">⏭ Skip it</button>
        </div>
      </div>
      <div class="overlay" id="ovl">
        <div class="big">🍿</div>
        <div id="ovlText">Click to join playback</div>
      </div>
      <button id="fsBtn" title="Fullscreen" onclick="toggleFullscreen()">⛶</button>
    </div>
    <div class="card hidden" id="bbar">
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <span style="font-size:13px;color:var(--muted)">🌐 shared browser</span>
        <span style="font-size:13px" id="bwho"></span>
        <button id="bctl" onclick="send({t:'browser_control'})">🖱 Take control</button>
        <input id="burl" placeholder="Go to URL or search…"
               style="flex:1;min-width:140px;background:var(--panel2);border:1px solid #333350;color:var(--text);border-radius:8px;padding:8px 10px;font-size:13px"
               onkeydown="if(event.key==='Enter'&&this.value.trim()){send({t:'browser_nav',url:this.value.trim()});this.value=''}">
        <button id="bmediaBtn" onclick="requestMedia()">📹 Media <span id="bmediaN"></span></button>
        <button class="danger" onclick="send({t:'browser_stop'})">✖ Close browser</button>
      </div>
      <div id="bmediaList" class="hidden" style="margin-top:10px;border-top:1px solid #2a2a40;padding-top:10px"></div>
    </div>
    <div class="card">
      <div class="addrow">
        <input id="addq" placeholder="YouTube / Shorts / Reels / TikTok / Twitter link, or search…"
               onkeydown="if(event.key==='Enter')addVid(false)">
        <button class="primary" onclick="addVid(false)">Add</button>
        <button onclick="addVid(true)">Play next</button>
        <button onclick="send({t:'skip'})">⏭</button>
        <button title="Shared browser" onclick="startBrowser()">🌐</button>
      </div>
    </div>
    <div class="card">
      <div class="qhead"><h2>📜 Up next</h2><span class="s" id="qcount"></span></div>
      <div id="queue"><div class="empty">Nothing queued — add something!</div></div>
    </div>
  </div>
  <div class="chatbox">
    <h2>💬 Chat</h2>
    <div class="msgs" id="msgs"></div>
    <div class="chatrow">
      <input id="chatq" placeholder="Say something…" maxlength="500"
             onkeydown="if(event.key==='Enter')sendChat()">
      <button class="primary" onclick="sendChat()">➤</button>
    </div>
  </div>
</div>
<div class="toast" id="toast"></div>
<div class="modal hidden" id="nameModal"><div class="box">
  <b>Pick a name</b>
  <input id="nameInput" maxlength="24" placeholder="Your name"
         onkeydown="if(event.key==='Enter')saveName()">
  <button class="primary" style="width:100%" onclick="saveName()">Join room</button>
</div></div>
<div class="modal hidden" id="settingsModal"><div class="box" style="text-align:left;width:340px">
  <b>⚙️ Room settings</b>
  <label class="setrow"><span>🛡 Adblock<small>AdGuard in the shared browser</small></span>
    <input type="checkbox" id="setAdblock"></label>
  <label class="setrow"><span>⏭ SponsorBlock<small>cut sponsor segments from downloads</small></span>
    <input type="checkbox" id="setSponsor"></label>
  <label class="setrow"><span>🎞 Max quality<small>for downloads &amp; streams</small></span>
    <select id="setQuality">
      <option value="360">360p</option><option value="480">480p</option>
      <option value="720">720p</option><option value="1080">1080p</option>
    </select></label>
  <div style="color:var(--muted);font-size:12px;margin:6px 0 12px">
    Adblock applies the next time the browser starts. Quality &amp; SponsorBlock apply to newly added videos.</div>
  <button class="primary" style="width:100%" onclick="saveSettings()">Save for this room</button>
  <button style="width:100%;margin-top:8px" onclick="document.getElementById('settingsModal').classList.add('hidden')">Cancel</button>
</div></div>
<div class="modal hidden" id="landing"><div class="box">
  <div style="font-size:44px">🍿</div>
  <b style="font-size:19px">DeepPixel Watch</b>
  <div style="color:var(--muted);font-size:13px;margin-top:6px">
    Create a room and share the link with anyone — no Discord needed.</div>
  <input id="roomNameInput" maxlength="40" placeholder="Room name (optional)"
         onkeydown="if(event.key==='Enter')createRoom('watch')">
  <button class="primary" style="width:100%" onclick="createRoom('watch')">🎬 Watch Together</button>
  <button style="width:100%;margin-top:8px" onclick="createRoom('reels')">📱 ReelsTogether</button>
</div></div>
<script>
const P = new URLSearchParams(location.search);
const ROOM = P.get('room') || '', TOKEN = P.get('token') || '';
let ws = null, st = null, myName = localStorage.getItem('wt_name') || '';
let sup = {play:0, pause:0, seek:0};  // suppress echoing remote-triggered events
let needGesture = true, syncAt = 0, endedSent = -1;
const v = document.getElementById('v');
// HLS segment requests are playlist-relative (no query), auth rides a cookie
if(TOKEN) document.cookie = 'wt_token='+encodeURIComponent(TOKEN)+'; path=/watch; SameSite=Lax; max-age=604800';

function esc(s){const d=document.createElement('div');d.textContent=s==null?'':s;return d.innerHTML}
function fmt(sec){if(sec==null||isNaN(sec))return'?:??';sec=Math.max(0,Math.floor(sec));
  const h=Math.floor(sec/3600),m=Math.floor(sec%3600/60),s=sec%60;
  return (h?h+':'+String(m).padStart(2,'0'):m)+':'+String(s).padStart(2,'0')}
function toast(t){const e=document.getElementById('toast');e.textContent=t;e.classList.add('show');
  clearTimeout(e._t);e._t=setTimeout(()=>e.classList.remove('show'),2500)}
function send(o){if(ws&&ws.readyState===1)ws.send(JSON.stringify(o))}

function saveName(){
  const n=document.getElementById('nameInput').value.trim();
  if(!n)return;
  myName=n;localStorage.setItem('wt_name',n);
  document.getElementById('nameModal').classList.add('hidden');
  connect();
}
function connect(){
  const proto = location.protocol==='https:'?'wss://':'ws://';
  ws = new WebSocket(proto+location.host+'/watch/ws?room='+encodeURIComponent(ROOM)+'&token='+encodeURIComponent(TOKEN));
  ws.onopen = ()=>{document.getElementById('dot').classList.add('on');send({t:'join',name:myName})};
  ws.onclose = ()=>{document.getElementById('dot').classList.remove('on');
    setTimeout(connect, 2500)};
  ws.onmessage = e=>{
    let d; try{d=JSON.parse(e.data)}catch(_){return}
    if(d.t==='state') applyState(d);
    else if(d.t==='sync') applySync(d);
    else if(d.t==='chat') addMsg(d);
    else if(d.t==='progress') updateProgress(d);
    else if(d.t==='bcursor') showCursor(d);
    else if(d.t==='bmedia') showMedia(d);
  };
}

function curItem(){
  if(!st) return null;
  const i = st.index - st.offset;
  return (i>=0 && i<st.queue.length) ? st.queue[i] : null;
}

function applyState(d){
  const prev = st ? curItem() : null;
  const prevUid = prev ? prev.uid : null;
  st = d;
  document.getElementById('roomName').textContent = d.name;
  document.title = d.name + ' — Watch Together';
  document.getElementById('who').innerHTML = d.participants.map(p=>
    `<span class="chip ${p===myName?'me':''}">${esc(p)}</span>`).join('');
  if(d.chat && !document.getElementById('msgs').childElementCount)
    d.chat.forEach(addMsg);
  renderQueue();
  updateBrowserView();
  const cur = curItem();
  const failOvl = document.getElementById('failOvl');
  if(browserActive()){
    failOvl.classList.add('hidden');
    document.getElementById('ovl').classList.add('hidden');
    if(!v.paused) v.pause();
  } else if(cur && (cur.status==='embed' || cur.status==='error')){
    // yt-dlp couldn't grab it: offer the shared browser
    v.removeAttribute('src'); v.load();
    document.getElementById('failText').textContent =
      (cur.title||'This one') + " couldn't be downloaded.";
    failOvl.classList.remove('hidden');
    document.getElementById('ovl').classList.add('hidden');
  } else {
    failOvl.classList.add('hidden');
    if(cur && cur.file){
      if(cur.uid !== curSrcUid){
        endedSent = -1;
        setVideoSrc(cur);
      }
    } else if(!cur){
      curSrcUid = null; destroyHls();
      v.removeAttribute('src'); v.load();
    }
  }
  applySync({index:d.index, playing:d.playing, position:d.position});
}

// ---- video source (plain file or progressive HLS while downloading) ----
let curSrcUid=null, hlsP=null;
function destroyHls(){ if(hlsP){ try{hlsP.destroy()}catch(_){} hlsP=null; } }
function setVideoSrc(cur){
  destroyHls();
  curSrcUid = cur.uid;
  const url = 'media/'+cur.file.split('/').map(encodeURIComponent).join('/')
            + '?token='+encodeURIComponent(TOKEN);
  if(cur.file.endsWith('.m3u8')
     && !v.canPlayType('application/vnd.apple.mpegurl')
     && window.Hls && Hls.isSupported()){
    hlsP = new Hls();
    hlsP.loadSource(url);
    hlsP.attachMedia(v);
  } else {
    v.src = url;   // Safari plays m3u8 natively; files play everywhere
    v.load();
  }
}
function openSettings(){
  if(!st || !st.settings) return;
  document.getElementById('setAdblock').checked = !!st.settings.adblock;
  document.getElementById('setSponsor').checked = !!st.settings.sponsorblock;
  document.getElementById('setQuality').value = String(st.settings.quality||720);
  document.getElementById('settingsModal').classList.remove('hidden');
}
function saveSettings(){
  send({t:'settings',
    adblock: document.getElementById('setAdblock').checked,
    sponsorblock: document.getElementById('setSponsor').checked,
    quality: +document.getElementById('setQuality').value});
  document.getElementById('settingsModal').classList.add('hidden');
}

// ---- shared co-browser (real Firefox streamed from the server) ----
let bPlayer=null;
function browserActive(){ return !!(st && st.browser && st.browser.active); }
function amController(){ return browserActive() && st.browser.controller===myName; }
function updateBrowserView(){
  const on = browserActive();
  document.getElementById('bwrap').classList.toggle('hidden', !on);
  document.getElementById('bbar').classList.toggle('hidden', !on);
  v.classList.toggle('hidden', on);
  if(on){
    const b = st.browser;
    document.getElementById('bwho').textContent = b.status==='starting'
      ? '⏳ starting Firefox…' : '🖱 '+(b.controller||'?')+' has control';
    const mine = amController();
    document.getElementById('bctl').style.display = mine ? 'none' : '';
    document.getElementById('burl').style.display = mine ? '' : 'none';
    document.getElementById('bcanvas').classList.toggle('controlling', mine);
    const mn = document.getElementById('bmediaN');
    if(mn) mn.textContent = b.media_count ? '('+b.media_count+')' : '';
    if(!bPlayer && b.status==='running') startStream();
  } else {
    stopStream();
    document.getElementById('bcursor').style.display='none';
    document.getElementById('bmediaList').classList.add('hidden');
  }
}
function showCursor(d){
  if(!browserActive()) return;
  const wrap=document.getElementById('bwrap'), cur=document.getElementById('bcursor');
  const r=wrap.getBoundingClientRect();
  cur.style.display='block';
  cur.style.left=(d.x*r.width)+'px';
  cur.style.top=(d.y*r.height)+'px';
  if(d.down){ cur.classList.remove('click'); void cur.offsetWidth; cur.classList.add('click'); }
}
function requestMedia(){
  send({t:'browser_media'});
  const el=document.getElementById('bmediaList');
  el.classList.remove('hidden');
  el.innerHTML='<div class="empty" style="padding:8px 0">Scanning page…</div>';
}
function showMedia(d){
  const el=document.getElementById('bmediaList');
  el.classList.remove('hidden');
  if(!d.items || !d.items.length){
    el.innerHTML='<div class="empty" style="padding:8px 0">No media detected yet — start playing something on the page, then tap 📹 again.</div>';
    return;
  }
  el.innerHTML = d.items.map(m=>
    `<div class="mediaitem"><span class="mi-kind">${esc(m.kind)}</span>`+
    `<span class="mi-name" title="${esc(m.url)}">${esc(m.name)} <span style="color:var(--muted)">${esc(m.mime)}</span></span>`+
    `<button class="primary" onclick='pickMedia(${JSON.stringify(m.url)})'>Add</button></div>`).join('');
}
function pickMedia(url){
  send({t:'browser_pick', url});
  toast('📹 Added to the queue — resolving…');
  document.getElementById('bmediaList').classList.add('hidden');
}
function startStream(){
  if(typeof JSMpeg === 'undefined'){ toast('❌ stream player failed to load'); return; }
  const proto = location.protocol==='https:'?'wss://':'ws://';
  const url = proto+location.host+'/watch/stream?room='+encodeURIComponent(ROOM)+'&token='+encodeURIComponent(TOKEN);
  bPlayer = new JSMpeg.Player(url, {
    canvas: document.getElementById('bcanvas'),
    audio: true, pauseWhenHidden: false,
    videoBufferSize: 2*1024*1024, audioBufferSize: 512*1024,
  });
}
function stopStream(){
  if(bPlayer){ try{bPlayer.destroy()}catch(_){} bPlayer=null; }
}
function startBrowser(){
  send({t:'browser_start'});
  toast('🌐 Starting the shared browser… (takes ~10s)');
}
function openInBrowser(){
  const cur = curItem();
  send({t:'browser_start', url: cur ? (cur.url||'') : ''});
  toast('🌐 Starting the shared browser… (takes ~10s)');
}

// controller input -> server -> XTEST
const bc = document.getElementById('bcanvas');
let lastMove = 0;
function bxy(e){
  const r = bc.getBoundingClientRect();
  return {x:(e.clientX-r.left)/r.width, y:(e.clientY-r.top)/r.height};
}
function bbtn(e){ return e.button===2?3:e.button===1?2:1; }
bc.addEventListener('mousemove', e=>{
  if(!amController())return;
  const now=Date.now(); if(now-lastMove<33)return; lastMove=now;
  const p=bxy(e); send({t:'bmouse',a:'move',x:p.x,y:p.y});
});
bc.addEventListener('mousedown', e=>{
  if(!amController())return; e.preventDefault(); bc.focus();
  const p=bxy(e); send({t:'bmouse',a:'down',x:p.x,y:p.y,b:bbtn(e)});
});
bc.addEventListener('mouseup', e=>{
  if(!amController())return;
  const p=bxy(e); send({t:'bmouse',a:'up',x:p.x,y:p.y,b:bbtn(e)});
});
bc.addEventListener('wheel', e=>{
  if(!amController())return; e.preventDefault();
  send({t:'bmouse',a:'wheel',dy:e.deltaY});
},{passive:false});
bc.addEventListener('contextmenu', e=>e.preventDefault());
bc.addEventListener('keydown', e=>{
  if(!amController())return; e.preventDefault();
  send({t:'bkey',a:'down',key:e.key});
});
bc.addEventListener('keyup', e=>{
  if(!amController())return; e.preventDefault();
  send({t:'bkey',a:'up',key:e.key});
});
bc.addEventListener('touchstart', e=>{
  if(!amController())return; e.preventDefault();
  const t=e.touches[0], p=bxy(t);
  send({t:'bmouse',a:'down',x:p.x,y:p.y,b:1});
},{passive:false});
bc.addEventListener('touchend', e=>{
  if(!amController())return; e.preventDefault();
  const t=e.changedTouches[0], p=bxy(t);
  send({t:'bmouse',a:'up',x:p.x,y:p.y,b:1});
},{passive:false});

function applySync(d){
  if(!st) return;
  if(d.index !== undefined && d.index !== st.index){
    // queue window may have shifted; ask nothing, next state broadcast covers it
    st.index = d.index;
  }
  st.playing = d.playing; syncAt = Date.now();
  st.position = d.position;
  if(d.by && d.action && d.by !== myName){
    toast((d.action==='play'?'▶️':d.action==='pause'?'⏸':'⏩')+' '+d.by);
  }
  if(browserActive()) return;
  const cur = curItem();
  if(cur && (cur.status==='embed' || cur.status==='error')) return;
  if(!cur || !cur.file) return;
  const target = d.position;
  if(Math.abs(v.currentTime - target) > 1.6){ sup.seek++; v.currentTime = target; }
  if(d.playing && v.paused){ sup.play++; tryPlay(); }
  if(!d.playing && !v.paused){ sup.pause++; v.pause(); }
}

function tryPlay(){
  const p = v.play();
  if(p) p.then(()=>{document.getElementById('ovl').classList.add('hidden');needGesture=false;})
        .catch(()=>{document.getElementById('ovl').classList.remove('hidden');});
}
document.getElementById('ovl').onclick = ()=>{
  needGesture=false;
  document.getElementById('ovl').classList.add('hidden');
  if(st && st.playing){ sup.play++; tryPlay(); }
};

v.addEventListener('play', ()=>{ if(sup.play>0){sup.play--;return} send({t:'play',pos:v.currentTime}) });
v.addEventListener('pause', ()=>{ if(v.ended)return; if(sup.pause>0){sup.pause--;return}
  if(v.seeking)return; send({t:'pause',pos:v.currentTime}) });
v.addEventListener('seeked', ()=>{ if(sup.seek>0){sup.seek--;return} send({t:'seek',pos:v.currentTime}) });
v.addEventListener('ended', ()=>{ if(st && endedSent!==st.index){endedSent=st.index; send({t:'ended',index:st.index})} });

function renderQueue(){
  const q = document.getElementById('queue');
  document.getElementById('qcount').textContent = st.total + ' item' + (st.total===1?'':'s');
  if(!st.queue.length){q.innerHTML='<div class="empty">Nothing queued — add something!</div>';return}
  q.innerHTML = st.queue.map((it,i)=>{
    const gi = i + st.offset;
    const now = gi===st.index;
    let status='';
    if(it.status==='pending') status='⏳';
    else if(it.status==='downloading') status='⬇️ '+(it.progress||0)+'%';
    else if(it.status==='error') status='❌';
    else if(it.status==='embed') status='🌐';
    else if(it.live_dl) status='▶️⬇ '+(it.progress||0)+'%';
    else status = fmt(it.duration);
    return `<div class="qitem ${now?'now':''}" data-uid="${it.uid}">`+
      (it.thumbnail?`<img src="${esc(it.thumbnail)}" loading="lazy">`:'<img>')+
      `<span class="t" onclick="send({t:'jump',index:${gi}})" title="${esc(it.title)}">${now?'▶ ':''}${esc(it.title)}</span>`+
      `<span class="s" data-status>${status}</span>`+
      `<span class="s">${esc(it.added_by||'')}</span>`+
      (now?'':`<span class="x" onclick="send({t:'remove',index:${gi}})">✖</span>`)+
      `</div>`;
  }).join('');
}
function updateProgress(d){
  if(!st)return;
  const it = st.queue.find(x=>x.uid===d.uid);
  if(it){it.progress=d.pct;
    const el=document.querySelector(`[data-uid="${d.uid}"] [data-status]`);
    if(el)el.textContent='⬇️ '+d.pct+'%';}
}

function addVid(next){
  const inp=document.getElementById('addq'), q=inp.value.trim();
  if(!q)return;
  send({t:'add', q, next});
  inp.value='';
  toast('⏳ Resolving…');
}
function addMsg(d){
  const box=document.getElementById('msgs');
  const el=document.createElement('div');
  if(d.name===null){el.className='m sys';el.textContent=d.text;}
  else{el.className='m';el.innerHTML=`<span class="n">${esc(d.name)}</span> ${esc(d.text)}`;}
  box.appendChild(el);
  while(box.childElementCount>120)box.removeChild(box.firstChild);
  box.scrollTop=box.scrollHeight;
}
function sendChat(){
  const inp=document.getElementById('chatq'), t=inp.value.trim();
  if(!t)return; send({t:'chat',text:t}); inp.value='';
}

// smooth drift correction between syncs
setInterval(()=>{
  if(!st || !st.playing || browserActive()) return;
  const cur = curItem();
  if(cur && (cur.status==='embed' || cur.status==='error')) return;
  if(v.paused || !v.duration) return;
  const target = st.position + (Date.now()-syncAt)/1000;
  if(Math.abs(v.currentTime-target) > 1.6){ sup.seek++; v.currentTime = target; }
}, 3000);

function copyLink(){
  const url = location.href;
  (navigator.clipboard ? navigator.clipboard.writeText(url) : Promise.reject())
    .then(()=>toast('🔗 Link copied — send it to your friends!'))
    .catch(()=>prompt('Copy this link:', url));
}
function toggleFullscreen(){
  const el = document.querySelector('.stage');
  const fsEl = document.fullscreenElement || document.webkitFullscreenElement;
  if(fsEl){
    (document.exitFullscreen || document.webkitExitFullscreen).call(document);
  } else {
    const req = el.requestFullscreen || el.webkitRequestFullscreen;
    if(req) req.call(el);
    else if(v.webkitEnterFullscreen && !browserActive()) v.webkitEnterFullscreen(); // iPhone: video only
  }
}
v.addEventListener('dblclick', e=>{ e.preventDefault(); toggleFullscreen(); });
async function createRoom(mode){
  const name=document.getElementById('roomNameInput').value.trim();
  try{
    const r=await fetch('api/create',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({mode,name})});
    const d=await r.json();
    location.href=d.url;
  }catch(e){ toast('❌ Could not create room'); }
}

if(!ROOM || !TOKEN){
  document.getElementById('landing').classList.remove('hidden');
}else if(myName){ connect(); }
else{ document.getElementById('nameModal').classList.remove('hidden');
      document.getElementById('nameInput').focus(); }
</script>
</body>
</html>
"""

# ================================================================ REELS PAGE

REELS_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>ReelsTogether</title>
<style>
  :root{--accent:#7c6cf0;--accent2:#4ec9a0}
  *{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
  html,body{margin:0;height:100%;overflow:hidden;background:#000;color:#fff;
    font-family:system-ui,-apple-system,'Segoe UI',sans-serif;overscroll-behavior:none}
  #stage{position:fixed;inset:0;display:flex;align-items:center;justify-content:center}
  video{max-width:100vw;max-height:100vh;width:auto;height:100%;object-fit:contain}
  .topbar{position:fixed;top:0;left:0;right:0;padding:12px 14px;display:flex;gap:8px;
    align-items:center;z-index:10;background:linear-gradient(rgba(0,0,0,.55),transparent)}
  .topbar .name{font-weight:600;font-size:15px;flex:1;text-shadow:0 1px 4px #000}
  .chip{background:rgba(255,255,255,.14);backdrop-filter:blur(6px);border-radius:999px;
    padding:4px 10px;font-size:12px}
  .rail{position:fixed;right:10px;bottom:110px;display:flex;flex-direction:column;gap:18px;
    align-items:center;z-index:10}
  .rbtn{width:48px;height:48px;border-radius:50%;background:rgba(255,255,255,.14);
    backdrop-filter:blur(6px);border:none;color:#fff;font-size:22px;cursor:pointer;
    display:flex;align-items:center;justify-content:center}
  .rbtn:active{transform:scale(1.15)}
  .rcount{font-size:12px;margin-top:-12px;text-shadow:0 1px 3px #000}
  .meta{position:fixed;left:14px;right:80px;bottom:88px;z-index:9;text-shadow:0 1px 4px #000}
  .meta .t{font-size:15px;font-weight:600;margin-bottom:4px;display:-webkit-box;
    -webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
  .meta .u{font-size:13px;opacity:.8}
  .chatfeed{position:fixed;left:14px;bottom:150px;right:90px;z-index:9;display:flex;
    flex-direction:column;gap:5px;pointer-events:none}
  .cmsg{background:rgba(0,0,0,.45);backdrop-filter:blur(4px);border-radius:10px;
    padding:5px 10px;font-size:13px;max-width:85%;align-self:flex-start;
    animation:fadeout 8s forwards;word-break:break-word}
  .cmsg .n{color:var(--accent2);font-weight:600}
  @keyframes fadeout{0%,80%{opacity:1}100%{opacity:0}}
  .chatrow{position:fixed;left:10px;right:70px;bottom:14px;display:flex;gap:8px;z-index:10}
  .chatrow input{flex:1;background:rgba(255,255,255,.12);backdrop-filter:blur(8px);
    border:1px solid rgba(255,255,255,.18);color:#fff;border-radius:999px;
    padding:11px 16px;font-size:14px;outline:none}
  .chatrow input::placeholder{color:rgba(255,255,255,.55)}
  .pbar{position:fixed;top:0;left:0;height:3px;background:var(--accent);z-index:12;width:0%}
  .overlay{position:fixed;inset:0;background:rgba(0,0,0,.86);z-index:20;display:flex;
    align-items:center;justify-content:center;flex-direction:column;gap:14px;text-align:center;
    padding:24px;cursor:pointer}
  .overlay .big{font-size:52px}
  .overlay input{background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.25);
    color:#fff;border-radius:12px;padding:13px 16px;font-size:16px;width:min(320px,80vw);
    text-align:center;outline:none}
  .overlay button{background:var(--accent);color:#fff;border:none;border-radius:12px;
    padding:12px 26px;font-size:15px;cursor:pointer}
  .hidden{display:none!important}
  .spin{width:40px;height:40px;border:4px solid rgba(255,255,255,.2);
    border-top-color:var(--accent);border-radius:50%;animation:spin 1s linear infinite}
  @keyframes spin{to{transform:rotate(360deg)}}
  .bigheart{position:fixed;font-size:90px;z-index:15;pointer-events:none;
    animation:heart 1s ease-out forwards}
  @keyframes heart{0%{transform:scale(.4);opacity:0}25%{transform:scale(1.15);opacity:1}
    100%{transform:scale(1) translateY(-70px);opacity:0}}
  .toast{position:fixed;top:60px;left:50%;transform:translateX(-50%);
    background:rgba(0,0,0,.7);backdrop-filter:blur(6px);padding:8px 16px;border-radius:999px;
    font-size:13px;z-index:16;opacity:0;transition:opacity .3s;pointer-events:none}
  .toast.show{opacity:1}
  .swipehint{position:fixed;bottom:170px;left:50%;transform:translateX(-50%);z-index:8;
    font-size:13px;opacity:.7;animation:bob 1.6s ease-in-out infinite;text-shadow:0 1px 4px #000}
  @keyframes bob{0%,100%{transform:translate(-50%,0)}50%{transform:translate(-50%,-8px)}}
</style>
</head>
<body>
<div class="pbar" id="pbar"></div>
<div id="stage"><video id="v" playsinline loop></video></div>
<div id="embedBox" class="hidden" style="position:fixed;inset:0;z-index:4;background:#000"></div>
<div id="embedNote" class="hidden" style="position:fixed;top:52px;left:50%;transform:translateX(-50%);
  z-index:11;background:rgba(0,0,0,.7);backdrop-filter:blur(6px);padding:6px 14px;border-radius:999px;
  font-size:12px;white-space:nowrap">🌐 browser mode — use ⬆️ to skip</div>
<div class="topbar">
  <div class="name">📱 ReelsTogether</div>
  <span class="chip" id="whoCount">👥 1</span>
</div>
<div class="rail">
  <button class="rbtn" id="likeBtn">🤍</button><div class="rcount" id="likeCount">0</div>
  <button class="rbtn" onclick="promptAdd()">➕</button>
  <button class="rbtn" onclick="copyLink()">🔗</button>
  <button class="rbtn" onclick="toggleFullscreen()">⛶</button>
  <button class="rbtn" onclick="doSwipe()">⬆️</button>
</div>
<div class="meta"><div class="t" id="mtitle"></div><div class="u" id="muser"></div></div>
<div class="chatfeed" id="chatfeed"></div>
<div class="chatrow">
  <input id="chatq" placeholder="Chat…" maxlength="200"
         onkeydown="if(event.key==='Enter')sendChat()">
</div>
<div class="swipehint" id="hint">swipe up for next ⬆</div>
<div class="toast" id="toast"></div>

<div class="overlay" id="nameOvl">
  <div class="big">📱</div><b style="font-size:20px">ReelsTogether</b>
  <div style="opacity:.75">Watch reels in sync with your friends</div>
  <input id="nameInput" maxlength="24" placeholder="Your name"
         onkeydown="if(event.key==='Enter')saveName()">
  <button onclick="saveName()">Let's go</button>
</div>
<div class="overlay hidden" id="seedOvl">
  <div class="big">✨</div><b style="font-size:19px">What do you wanna see?</b>
  <div style="opacity:.75">Seed the feed — e.g. "cats, football, cooking fails"</div>
  <input id="seedInput" maxlength="120" placeholder="topics…"
         onkeydown="if(event.key==='Enter')sendSeed()">
  <button onclick="sendSeed()">Start the feed</button>
</div>
<div class="overlay hidden" id="tapOvl"><div class="big">📱</div><div>Tap to start watching</div></div>
<div class="overlay hidden" id="loadOvl" style="background:rgba(0,0,0,.6)">
  <div class="spin"></div><div id="loadText">Finding the next reel…</div>
</div>

<script>
const P = new URLSearchParams(location.search);
const ROOM = P.get('room')||'', TOKEN = P.get('token')||'';
let ws=null, st=null, myName=localStorage.getItem('wt_name')||'';
let sup={play:0,pause:0,seek:0}, syncAt=0, swipeLock=0, watchedStart=0;
const v=document.getElementById('v');

function esc(s){const d=document.createElement('div');d.textContent=s==null?'':s;return d.innerHTML}
function send(o){if(ws&&ws.readyState===1)ws.send(JSON.stringify(o))}
function toast(t){const e=document.getElementById('toast');e.textContent=t;e.classList.add('show');
  clearTimeout(e._t);e._t=setTimeout(()=>e.classList.remove('show'),2200)}

function saveName(){
  const n=document.getElementById('nameInput').value.trim(); if(!n)return;
  myName=n; localStorage.setItem('wt_name',n);
  document.getElementById('nameOvl').classList.add('hidden');
  connect();
}
function connect(){
  const proto=location.protocol==='https:'?'wss://':'ws://';
  ws=new WebSocket(proto+location.host+'/watch/ws?room='+encodeURIComponent(ROOM)+'&token='+encodeURIComponent(TOKEN));
  ws.onopen=()=>send({t:'join',name:myName});
  ws.onclose=()=>setTimeout(connect,2500);
  ws.onmessage=e=>{
    let d; try{d=JSON.parse(e.data)}catch(_){return}
    if(d.t==='state')applyState(d);
    else if(d.t==='sync')applySync(d);
    else if(d.t==='chat')addMsg(d);
    else if(d.t==='heart')onHeart(d);
  };
}
function curItem(){
  if(!st)return null;
  const i=st.index-st.offset;
  return (i>=0&&i<st.queue.length)?st.queue[i]:null;
}
function applyState(d){
  const prev=curItem(); const prevUid=prev?prev.uid:null;
  st=d; syncAt=Date.now();
  document.getElementById('whoCount').textContent='👥 '+d.participants.length;
  document.getElementById('seedOvl').classList.toggle('hidden', !d.need_seed);
  const cur=curItem();
  if(cur&&(cur.file||cur.status==='embed')){
    document.getElementById('loadOvl').classList.add('hidden');
    if(cur.status==='embed'){
      if(cur.uid!==embedUid) showReelEmbed(cur);
    } else {
      if(embedUid) clearReelEmbed();
      if(cur.uid!==prevUid || !v.src.includes(encodeURIComponent(cur.file))){
        v.src='media/'+encodeURIComponent(cur.file)+'?token='+encodeURIComponent(TOKEN);
        v.load(); watchedStart=Date.now();
        sup.play++; tryPlay();
      }
    }
    document.getElementById('mtitle').textContent=cur.title||'';
    document.getElementById('muser').textContent=(cur.uploader?'@'+cur.uploader:'')+(cur.added_by&&cur.added_by!=='✨ feed'?'  •  added by '+cur.added_by:'');
    document.getElementById('likeCount').textContent=cur.likes||0;
    document.getElementById('likeBtn').textContent=(cur.likes||0)>0?'❤️':'🤍';
  } else if(!d.need_seed){
    if(embedUid) clearReelEmbed();
    document.getElementById('loadOvl').classList.remove('hidden');
    document.getElementById('loadText').textContent = cur&&cur.status==='downloading'
      ? 'Downloading… '+(cur.progress||0)+'%' : 'Finding the next reel…';
  }
}

// browser mode: embed clips that couldn't be downloaded
let embedUid=null;
function clearReelEmbed(){
  embedUid=null;
  const box=document.getElementById('embedBox');
  box.innerHTML=''; box.classList.add('hidden');
  document.getElementById('embedNote').classList.add('hidden');
}
function showReelEmbed(cur){
  clearReelEmbed();
  embedUid=cur.uid;
  v.pause(); v.removeAttribute('src');
  watchedStart=Date.now();
  const box=document.getElementById('embedBox');
  box.classList.remove('hidden');
  document.getElementById('embedNote').classList.remove('hidden');
  const f=document.createElement('iframe');
  f.src = cur.embed_kind==='youtube'
    ? 'https://www.youtube.com/embed/'+cur.embed+'?autoplay=1&playsinline=1&loop=1&playlist='+cur.embed
    : cur.embed;
  f.style.cssText='width:100%;height:100%;border:0';
  f.allow='autoplay; fullscreen; encrypted-media; picture-in-picture';
  f.allowFullscreen=true;
  box.appendChild(f);
}
function copyLink(){
  const url=location.href;
  (navigator.clipboard?navigator.clipboard.writeText(url):Promise.reject())
    .then(()=>toast('🔗 Link copied!'))
    .catch(()=>prompt('Copy this link:',url));
}
function toggleFullscreen(){
  const fsEl=document.fullscreenElement||document.webkitFullscreenElement;
  if(fsEl){(document.exitFullscreen||document.webkitExitFullscreen).call(document)}
  else{
    const el=document.documentElement;
    const req=el.requestFullscreen||el.webkitRequestFullscreen;
    if(req)req.call(el);
    else if(v.webkitEnterFullscreen)v.webkitEnterFullscreen(); // iPhone fallback
  }
}
function applySync(d){
  if(!st)return;
  st.playing=d.playing; st.position=d.position; syncAt=Date.now();
  if(d.index!==undefined) st.index=d.index;
  const cur=curItem();
  if(!cur||!cur.file)return;
  if(Math.abs(v.currentTime-d.position)>2 && v.duration){sup.seek++;v.currentTime=Math.min(d.position, v.duration-0.1)}
  if(d.playing&&v.paused){sup.play++;tryPlay()}
  if(!d.playing&&!v.paused){sup.pause++;v.pause()}
}
function tryPlay(){
  const p=v.play();
  if(p)p.then(()=>document.getElementById('tapOvl').classList.add('hidden'))
       .catch(()=>document.getElementById('tapOvl').classList.remove('hidden'));
}
document.getElementById('tapOvl').onclick=()=>{
  document.getElementById('tapOvl').classList.add('hidden');
  v.muted=false; sup.play++; tryPlay();
};
function sendSeed(){
  const t=document.getElementById('seedInput').value.trim(); if(!t)return;
  send({t:'seed',text:t});
  document.getElementById('seedOvl').classList.add('hidden');
  document.getElementById('loadOvl').classList.remove('hidden');
}
function doSwipe(){
  if(Date.now()-swipeLock<600)return;
  swipeLock=Date.now();
  document.getElementById('hint').style.display='none';
  send({t:'swipe', watched:(Date.now()-watchedStart)/1000, dur:v.duration||0});
}
function doLike(){
  send({t:'like'});
}
function onHeart(d){
  const cur=curItem();
  if(cur&&cur.uid===d.uid){cur.likes=d.likes;
    document.getElementById('likeCount').textContent=d.likes;
    document.getElementById('likeBtn').textContent='❤️';}
  const h=document.createElement('div');h.className='bigheart';h.textContent='❤️';
  h.style.left=(30+Math.random()*40)+'%';h.style.top=(35+Math.random()*25)+'%';
  document.body.appendChild(h);setTimeout(()=>h.remove(),1000);
  if(d.by&&d.by!==myName)toast('❤️ '+d.by);
}
document.getElementById('likeBtn').onclick=doLike;
function promptAdd(){
  const q=prompt('Paste a link (Reel / Short / TikTok / anything) or search:');
  if(q&&q.trim()){send({t:'add',q:q.trim()});toast('⏳ Adding to the feed…')}
}
function addMsg(d){
  const box=document.getElementById('chatfeed');
  const el=document.createElement('div');el.className='cmsg';
  if(d.name===null)el.innerHTML=`<i style="opacity:.8">${esc(d.text)}</i>`;
  else el.innerHTML=`<span class="n">${esc(d.name)}</span> ${esc(d.text)}`;
  box.appendChild(el);
  while(box.childElementCount>5)box.removeChild(box.firstChild);
  setTimeout(()=>{if(el.parentNode)el.remove()},8200);
}
function sendChat(){
  const i=document.getElementById('chatq'),t=i.value.trim();
  if(!t)return;send({t:'chat',text:t});i.value='';i.blur();
}

// gestures: swipe up = next, double tap = like, single tap = pause/play
let tY=null, lastTap=0;
document.getElementById('stage').addEventListener('touchstart',e=>{tY=e.touches[0].clientY},{passive:true});
document.getElementById('stage').addEventListener('touchend',e=>{
  if(tY===null)return;
  const dy=tY-e.changedTouches[0].clientY; tY=null;
  if(dy>70){doSwipe();return}
  const now=Date.now();
  if(now-lastTap<300){doLike();lastTap=0;return}
  lastTap=now;
  setTimeout(()=>{if(lastTap===now)togglePlay()},310);
},{passive:true});
document.getElementById('stage').addEventListener('click',e=>{
  if('ontouchstart' in window)return;
  const now=Date.now();
  if(now-lastTap<300){doLike();lastTap=0;return}
  lastTap=now;
  setTimeout(()=>{if(lastTap===now)togglePlay()},310);
});
window.addEventListener('wheel',e=>{if(e.deltaY>30)doSwipe()},{passive:true});
window.addEventListener('keydown',e=>{
  if(e.target.tagName==='INPUT')return;
  if(e.key==='ArrowUp'||e.key==='ArrowDown'||e.key===' '){e.preventDefault();
    if(e.key===' ')togglePlay();else doSwipe();}
  if(e.key==='l')doLike();
});
function togglePlay(){
  if(v.paused){send({t:'play',pos:v.currentTime})}
  else{send({t:'pause',pos:v.currentTime})}
}
v.addEventListener('play',()=>{if(sup.play>0){sup.play--;return}send({t:'play',pos:v.currentTime})});
v.addEventListener('pause',()=>{if(v.ended)return;if(sup.pause>0){sup.pause--;return}send({t:'pause',pos:v.currentTime})});
setInterval(()=>{
  if(v.duration)document.getElementById('pbar').style.width=(v.currentTime/v.duration*100)+'%';
},200);

if(!ROOM||!TOKEN){
  location.replace('./');  // room creation lives on the /watch/ landing page
}else if(myName){document.getElementById('nameOvl').classList.add('hidden');connect();}
else{document.getElementById('nameInput').focus();}
</script>
</body>
</html>
"""
