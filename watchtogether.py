"""Watch Together + ReelsTogether for the music bot.

Mounted onto the bot's existing aiohttp app (webui.py) under /watch/:

    /watch/                page: synced video player + chat + queue
    /watch/reels           page: ReelsTogether - synced vertical swipe feed
    /watch/ws              websocket: room sync (chat, play/pause/seek, queue, swipes)
    /watch/media/{room}/{file} downloaded video files (range requests supported)

Rooms are keyed to a Discord channel; /watch and /reels slash commands hand
out a tokenized link (deeppixel.online/watch/?room=...#token=...).

Videos are anything yt-dlp can resolve (YouTube, Shorts, Reels, TikTok,
Twitter/X, ...). They are downloaded to a local cache and served from disk so
every participant streams the exact same file.

ReelsTogether keeps a per-room taste profile: likes and full watches boost
keywords from a clip's title/tags, fast skips decay them, and the next clips
are found by balancing learned interests, exploration, and creator diversity.
It keeps four reels ready ahead and up to 30 downloaded reels on disk.
"""

import asyncio
import glob
import hashlib
import json
import logging
import math
import os
import random
import re
import secrets
import time
from collections import deque

import yt_dlp
from aiohttp import web, WSMsgType

import config
from security import (
    PublicURLRequired, SlidingWindowLimiter, client_identity,
    validate_public_url,
)
from runtime import TaskRegistry, increment, prometheus
from storage import SQLiteDocumentStore, load_json, save_json

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
WATCH_CACHE_LIMIT = 10         # downloaded WatchTogether videos/HLS sessions
REELS_CACHE_LIMIT = 30         # downloaded ReelsTogether clips in total
REELS_HISTORY = 12             # recent backward history retained when possible
CHAT_HISTORY = 100
MAX_ACTIVE_ROOMS = 50
MAX_ROOM_PARTICIPANTS = 100
MAX_RETAINED_ROOM_MEMBERS = 500
MAX_ROOM_TOKENS = 500
MAX_QUEUE_ITEMS = 200
MAX_ROOM_PLAYLISTS = 30
MAX_PLAYLIST_ITEMS = 50
ROOM_IDLE_TTL = 2 * 3600
GLOBAL_DOWNLOADS = 4
REELS_READY_AHEAD = 4          # cache the next four reels while this one plays
REELS_MAX_DURATION = 150       # seconds - skip anything longer in the shorts feed
HLS_MIN_DURATION = 8 * 60      # longer than this -> stream while downloading

SETTINGS_FILE = os.path.join(BASE_DIR, 'room_settings.json')
ROOM_PLAYLISTS_FILE = os.path.join(BASE_DIR, 'room_playlists.json')
state_store = SQLiteDocumentStore(os.path.join(BASE_DIR, 'musicbot.sqlite3'), logger)
DEFAULT_SETTINGS = {
    'adblock': True, 'sponsorblock': True, 'quality': 720,
    'control_policy': 'moderators', 'skip_policy': 'vote', 'vote_threshold': 0.5,
}
SB_CATEGORIES = ['sponsor', 'selfpromo', 'interaction']

DEFAULT_TOPICS = [
    'funny fails', 'satisfying', 'memes', 'cats', 'dogs', 'football skills',
    'cooking', 'magic tricks', 'parkour', 'gaming moments', 'science facts',
    'street food', 'car', 'basketball', 'animals',
]

global_download_sem = None
create_limiter = SlidingWindowLimiter(8, 10 * 60)
message_limiter = SlidingWindowLimiter(120, 60)
task_registry = TaskRegistry('watch')


def _spawn(coroutine, name):
    return task_registry.create(coroutine, name)


def _global_download_limiter():
    global global_download_sem
    if global_download_sem is None:
        global_download_sem = asyncio.Semaphore(GLOBAL_DOWNLOADS)
    return global_download_sem

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
                'nocheckcertificate': True, 'skip_download': True,
                'socket_timeout': 15}
_info_opts = {'quiet': True, 'no_warnings': True, 'noplaylist': True,
              'nocheckcertificate': True, 'skip_download': True,
              'socket_timeout': 15}


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
        'socket_timeout': 20,
        'retries': 2,
    }
    if sponsorblock and not vertical:
        opts['postprocessors'] = [
            {'key': 'SponsorBlock', 'categories': SB_CATEGORIES},
            {'key': 'ModifyChapters', 'remove_sponsor_segments': SB_CATEGORIES},
        ]
    return opts


# ---------------------------------------------------------------- persistence

def _load_json(path, default):
    return load_json(path, default, logger)


def _save_json(path, data):
    return save_json(path, data, logger)


tokens = state_store.load('watch_tokens', {}, TOKENS_FILE)
profiles = state_store.load('reels_profiles', {}, PROFILES_FILE)
room_settings = state_store.load('room_settings', {}, SETTINGS_FILE)
room_playlists = state_store.load('room_playlists', {}, ROOM_PLAYLISTS_FILE)


def _save_tokens():
    state_store.save('watch_tokens', tokens)
    _save_json(TOKENS_FILE, tokens)


def _save_profiles():
    state_store.save('reels_profiles', profiles)
    _save_json(PROFILES_FILE, profiles)


def _save_room_settings():
    state_store.save('room_settings', room_settings)
    _save_json(SETTINGS_FILE, room_settings)


def _save_room_playlists():
    state_store.save('room_playlists', room_playlists)
    _save_json(ROOM_PLAYLISTS_FILE, room_playlists)


def get_settings(room_id):
    s = dict(DEFAULT_SETTINGS)
    s.update(room_settings.get(room_id, {}))
    return s


def update_settings(room_id, *, adblock=None, sponsorblock=None, quality=None,
                    control_policy=None, skip_policy=None, vote_threshold=None):
    """Persist validated room settings for Discord and web control surfaces."""
    cfg = get_settings(room_id)
    if adblock is not None:
        cfg['adblock'] = bool(adblock)
    if sponsorblock is not None:
        cfg['sponsorblock'] = bool(sponsorblock)
    if quality is not None:
        try:
            quality = int(quality)
        except (TypeError, ValueError):
            quality = 0
        if quality not in (360, 480, 720, 1080):
            raise ValueError('quality must be 360, 480, 720, or 1080')
        cfg['quality'] = quality
    if control_policy is not None:
        if control_policy not in ('host', 'moderators', 'everyone'):
            raise ValueError('invalid control policy')
        cfg['control_policy'] = control_policy
    if skip_policy is not None:
        if skip_policy not in ('moderators', 'vote', 'everyone'):
            raise ValueError('invalid skip policy')
        cfg['skip_policy'] = skip_policy
    if vote_threshold is not None:
        cfg['vote_threshold'] = max(0.25, min(1.0, float(vote_threshold)))
    room_settings[room_id] = cfg
    _save_room_settings()
    return cfg


def admin_snapshot():
    """Password-admin view of persisted and currently active room settings."""
    token_names = {}
    for info in tokens.values():
        room_id = info.get('room')
        if room_id and room_id not in token_names:
            token_names[room_id] = info.get('name')
    room_ids = set(room_settings) | set(rooms) | set(token_names)
    result = []
    for room_id in sorted(room_ids):
        room = rooms.get(room_id)
        result.append({
            'id': room_id,
            'mode': 'reels' if room_id.startswith('r') else 'watch',
            'name': (room.name if room else token_names.get(room_id)) or room_id,
            'active': bool(room),
            'participants': len(room.sockets) if room else 0,
            'queued': len(room.queue) if room else 0,
            'settings': get_settings(room_id),
        })
    return result


def _validated_room_id(room_id):
    room_id = str(room_id or '')
    if not re.fullmatch(r'[wr][A-Za-z0-9_-]{1,80}', room_id):
        raise ValueError('invalid room id')
    return room_id


def admin_update_room(room_id, data):
    """Validated room update used only by the password-protected admin API."""
    room_id = _validated_room_id(room_id)
    cfg = update_settings(
        room_id,
        adblock=data.get('adblock') if 'adblock' in data else None,
        sponsorblock=data.get('sponsorblock') if 'sponsorblock' in data else None,
        quality=data.get('quality') if 'quality' in data else None,
        control_policy=data.get('control_policy') if 'control_policy' in data else None,
        skip_policy=data.get('skip_policy') if 'skip_policy' in data else None,
        vote_threshold=data.get('vote_threshold') if 'vote_threshold' in data else None,
    )
    room_name = re.sub(r'\s+', ' ', str(data.get('name') or '').strip())[:50]
    if room_name:
        room = rooms.get(room_id)
        if room:
            room.name = room_name
        for info in tokens.values():
            if info.get('room') == room_id:
                info['name'] = room_name
        _save_tokens()
    return cfg


async def admin_delete_room(room_id):
    """Permanently remove a room and revoke its links from the admin API."""
    room_id = _validated_room_id(room_id)
    exists = room_id in rooms or room_id in room_settings or \
        room_id in room_playlists or room_id in profiles or \
        any(info.get('room') == room_id for info in tokens.values())
    if not exists:
        raise KeyError(room_id)

    # Stop work that could write the room back into persisted state while it is
    # being deleted. Task names are created as ``watch:<kind>:<room id>...``.
    room_tasks = [
        task for task in list(task_registry.tasks)
        if not task.done() and (
            f':{room_id}:' in task.get_name() or
            task.get_name().endswith(f':{room_id}')
        )
    ]
    for task in room_tasks:
        task.cancel()
    if room_tasks:
        await asyncio.gather(*room_tasks, return_exceptions=True)

    if cobrowser:
        session = cobrowser.get(room_id)
        if session:
            await session.stop()

    room = rooms.pop(room_id, None)
    if room:
        for ws in list(room.sockets):
            try:
                await ws.close(code=4004, message=b'room deleted by administrator')
            except Exception:
                pass
        room.sockets.clear()

    removed_tokens = [
        key for key, info in tokens.items() if info.get('room') == room_id
    ]
    for key in removed_tokens:
        tokens.pop(key, None)
    room_settings.pop(room_id, None)
    room_playlists.pop(room_id, None)
    profiles.pop(room_id, None)
    _save_tokens()
    _save_room_settings()
    _save_room_playlists()
    _save_profiles()
    logger.info('Admin deleted room %s and revoked %s link(s)',
                room_id, len(removed_tokens))


def get_room_link(channel_id, channel_name, mode):
    """Used by the /watch and /reels slash commands in music.py."""
    room_id = ('w' if mode == 'watch' else 'r') + str(channel_id)
    now = time.time()
    for k, v in list(tokens.items()):
        if now - v.get('created_at', 0) > TOKEN_TTL:
            tokens.pop(k, None)
    if len(tokens) >= MAX_ROOM_TOKENS:
        oldest = sorted(tokens, key=lambda key: tokens[key].get('created_at', 0))
        for key in oldest[:len(tokens) - MAX_ROOM_TOKENS + 1]:
            tokens.pop(key, None)
    token = None
    for k, v in tokens.items():
        if v.get('room') == room_id:
            token = k
            break
    if not token:
        token = secrets.token_urlsafe(12)
        tokens[token] = {'room': room_id, 'name': channel_name, 'created_at': now}
        _save_tokens()
    base = getattr(config, 'WEB_SERVER_URL', 'https://deeppixel.online').rstrip('/')
    path = '/watch/' if mode == 'watch' else '/watch/reels'
    return f"{base}{path}?room={room_id}#token={token}"


def _token_room(token):
    info = tokens.get(token or '')
    if not info:
        return None
    if time.time() - info.get('created_at', 0) > TOKEN_TTL:
        tokens.pop(token, None)
        _save_tokens()
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
        self.sockets = {}          # ws -> {'id': str, 'name': str, 'role': str, 'order': int}
        self.roles = {}            # private member session -> role
        self.member_ids = {}       # private member session -> public UI id
        self._join_sequence = 0
        self.dl_sem = None
        self.reels_task = None
        self.sync_task = None
        self._advanced_past = -1   # 'ended' debounce
        self.last_query = None     # avoid back-to-back identical feed searches
        self.recent_uploaders = deque(maxlen=8)  # prevent one creator taking over
        self._topup_busy = False   # topup is not reentrant-safe
        self.skip_votes = set()
        # Created lazily in the websocket loop for Python 3.9 compatibility.
        self.playlist_lock = None
        self.last_activity = time.monotonic()
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

    def download_limiter(self):
        if self.dl_sem is None:
            # Reels prefetches four ahead; let those four use the global pool
            # together. Long-form WatchTogether remains capped at two/room.
            self.dl_sem = asyncio.Semaphore(4 if self.mode == 'reels' else 2)
        return self.dl_sem

    def save_profile(self):
        profiles[self.id] = {
            'interests': self.interests,
            'seen': list(self.seen)[-500:],
        }
        _save_profiles()

    def join(self, ws, member_session, name):
        self._join_sequence += 1
        if member_session not in self.roles:
            active_sessions = {member['session'] for member in self.sockets.values()}
            removable = [session for session, role in self.roles.items()
                         if session not in active_sessions and role != 'host']
            while len(self.roles) >= MAX_RETAINED_ROOM_MEMBERS and removable:
                expired_session = removable.pop(0)
                self.roles.pop(expired_session, None)
                self.member_ids.pop(expired_session, None)
            has_host = any(role == 'host' for role in self.roles.values())
            self.roles[member_session] = 'viewer' if has_host else 'host'
        public_id = self.member_ids.setdefault(member_session, secrets.token_urlsafe(9))
        member = {
            'id': public_id,
            'session': member_session,
            'name': name,
            'role': self.roles[member_session],
            'order': self._join_sequence,
            'muted_until': 0,
        }
        self.sockets[ws] = member
        return member

    def role_for(self, member_session):
        return self.roles.get(member_session, 'viewer')

    def ensure_active_host(self):
        active = sorted(self.sockets.values(), key=lambda member: member['order'])
        if any(member['role'] == 'host' for member in active):
            return None
        active_sessions = {member['session'] for member in active}
        for member_session, role in list(self.roles.items()):
            if role == 'host' and member_session not in active_sessions:
                self.roles[member_session] = 'moderator'
        replacement = next((member for member in active if member['role'] == 'moderator'),
                           active[0] if active else None)
        if replacement:
            old_role = replacement['role']
            replacement['role'] = 'host'
            self.roles[replacement['session']] = 'host'
            return replacement, old_role
        return None


rooms = {}


def _prune_rooms():
    now = time.monotonic()
    stale = [room_id for room_id, room in rooms.items()
             if not room.sockets and now - room.last_activity >= ROOM_IDLE_TTL]
    for room_id in stale:
        room = rooms.pop(room_id)
        for task in (room.sync_task, room.reels_task):
            if task and not task.done():
                task.cancel()


def _get_room(room_id, name=None):
    _prune_rooms()
    room = rooms.get(room_id)
    if not room:
        if len(rooms) >= MAX_ACTIVE_ROOMS:
            empty = [candidate for candidate in rooms.values()
                     if not candidate.sockets]
            if empty:
                victim = min(empty, key=lambda candidate: candidate.last_activity)
                rooms.pop(victim.id, None)
            else:
                raise RuntimeError('server room limit reached')
        room = Room(room_id, name)
        rooms[room_id] = room
        increment('rooms_created_total')
    elif name and room.name != name:
        room.name = name
    return room


def _room_cookie_name(room_id):
    safe = re.sub(r'[^A-Za-z0-9_]', '_', room_id)[:64]
    return f'wt_session_{safe}'


def _room_member_cookie_name(room_id):
    safe = re.sub(r'[^A-Za-z0-9_]', '_', room_id)[:64]
    return f'wt_member_{safe}'


def _request_room_token(request, room_id):
    return request.cookies.get(_room_cookie_name(room_id), '')


def _client_key(request):
    return client_identity(request)


def _room_owns_file(room, rel):
    normalized = rel.replace('\\', '/').lstrip('/')
    if '..' in normalized.split('/'):
        return False
    for item in room.queue:
        owned = str(item.get('file') or '').replace('\\', '/').lstrip('/')
        if not owned:
            continue
        if normalized == owned:
            return True
        if owned.endswith('/index.m3u8'):
            directory = owned.rsplit('/', 1)[0] + '/'
            if normalized.startswith(directory):
                return True
    return False


def _safe_int(value, default=-1, minimum=-1, maximum=1_000_000):
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(minimum, min(maximum, parsed))


def _safe_float(value, default=0.0, minimum=0.0, maximum=7 * 86400.0):
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if parsed != parsed:  # NaN
        return default
    return max(minimum, min(maximum, parsed))


def _can_moderate(member):
    return bool(member and member.get('role') in ('host', 'moderator'))


def _is_host(member):
    return bool(member and member.get('role') == 'host')


def _can_control(room, member):
    policy = get_settings(room.id).get('control_policy', 'moderators')
    if policy == 'everyone':
        return bool(member)
    if policy == 'host':
        return _is_host(member)
    return _can_moderate(member)


def _playlist_summaries(room_id):
    playlists = room_playlists.get(room_id, {})
    return [
        {
            'name': name,
            'count': len(value.get('items') or []),
            'updated_by': value.get('updated_by'),
            'updated_at': value.get('updated_at'),
            'owner': value.get('owner_name') or value.get('updated_by'),
            'editors': value.get('editor_names') or [],
            'revisions': len(value.get('revisions') or []),
        }
        for name, value in sorted(playlists.items(), key=lambda pair: pair[0].lower())
    ]


def _member_owner_key(member):
    return hashlib.sha256(member['session'].encode()).hexdigest()


def _room_playlist_lock(room):
    if room.playlist_lock is None:
        room.playlist_lock = asyncio.Lock()
    return room.playlist_lock


def _can_edit_room_playlist(playlist, member):
    owner_key = _member_owner_key(member)
    return _can_moderate(member) or playlist.get('owner_key') == owner_key or \
        owner_key in (playlist.get('editor_keys') or [])


def _item_public(item):
    return {k: item.get(k) for k in
            ('uid', 'vid', 'title', 'duration', 'thumbnail', 'status',
             'progress', 'file', 'added_by', 'uploader', 'likes', 'error',
             'embed_kind', 'embed', 'url', 'live_dl', 'choices',
             'width', 'height')}


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
        'participants': [
            {'id': member['id'], 'name': member['name'], 'role': member['role']}
            for member in sorted(room.sockets.values(), key=lambda value: value['order'])
        ],
        'playlists': _playlist_summaries(room.id),
        'skip_votes': len(room.skip_votes),
        'skip_needed': max(1, math.ceil(
            len(room.sockets) * get_settings(room.id).get('vote_threshold', 0.5))),
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
    room.ensure_active_host()


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
                       quality=720, sponsorblock=False, http_headers=None):
    opts = _dl_opts(cache_dir, vertical, quality, sponsorblock)
    if http_headers:
        # replay the browser's headers (Referer/UA/cookies) for sniffed media
        opts['http_headers'] = dict(http_headers)
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


def _blocking_ffmpeg_dl(url, headers, cache_dir, uid, kind=None):
    """Direct download with ffmpeg (stream-copy). Handles disguised media -
    a .m3u8/.mp4 served as .txt/text-plain - that yt-dlp refuses. Uses the
    browser's replay headers so protected CDNs accept the request."""
    import subprocess
    out = os.path.join(cache_dir, f'{uid}.mp4')
    hdr_blob = ''.join(f'{k}: {v}\r\n' for k, v in (headers or {}).items()
                       if k.lower() != 'range')
    ua = (headers or {}).get('User-Agent')

    def _base():
        c = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y']
        if hdr_blob:
            c += ['-headers', hdr_blob]
        if ua:
            c += ['-user_agent', ua]
        return c

    # attempts: force the HLS demuxer first when we know it's a manifest (a
    # .txt-disguised m3u8 won't auto-probe), then plain copy, then transcode
    tail = ['-c', 'copy', '-bsf:a', 'aac_adtstoasc', '-movflags', '+faststart', out]
    force_hls = ['-f', 'hls', '-i', url]
    plain = ['-i', url]
    transcode = ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23',
                 '-c:a', 'aac', '-movflags', '+faststart', out]
    attempts = []
    if kind == 'manifest':
        attempts += [_base() + force_hls + tail, _base() + plain + tail,
                     _base() + force_hls + transcode]
    else:
        attempts += [_base() + plain + tail, _base() + force_hls + tail,
                     _base() + plain + transcode]

    last = 'ffmpeg failed'
    for cmd in attempts:
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=3600)
        except subprocess.TimeoutExpired:
            last = 'ffmpeg timed out'
            continue
        if proc.returncode == 0 and os.path.isfile(out) and \
                os.path.getsize(out) > 1024:
            return os.path.basename(out)
        last = proc.stderr.decode(errors='replace')[-200:] or last
        if os.path.isfile(out):
            try:
                os.remove(out)
            except OSError:
                pass
    raise RuntimeError(last)


def _trim_cache(keep, protected, cache_dirs=None):
    try:
        files = []
        cache_dirs = tuple(cache_dirs or (WATCH_CACHE_DIR, REELS_CACHE_DIR))
        # Preserve priority order (new item, current items, forward buffer,
        # history), but never let protection make the configured cap soft.
        protected = set([
            path for path in protected
            if any(path == root or path.startswith(root + os.sep)
                   for root in cache_dirs)
        ][:keep])
        for cache_dir in cache_dirs:
            files.extend(
                f for f in glob.glob(os.path.join(cache_dir, '*.*'))
                if not f.endswith(('.part', '.ytdl'))
            )
        hls_root = os.path.join(WATCH_CACHE_DIR, 'hls')
        if WATCH_CACHE_DIR in cache_dirs and os.path.isdir(hls_root):
            files.extend(
                os.path.join(hls_root, name) for name in os.listdir(hls_root)
                if os.path.isdir(os.path.join(hls_root, name))
            )
        files.sort(key=os.path.getmtime)
        removable = [f for f in files if f not in protected]
        excess = len(files) - keep
        for f in removable:
            if excess <= 0:
                break
            try:
                if os.path.isdir(f):
                    import shutil
                    shutil.rmtree(f)
                else:
                    os.remove(f)
                excess -= 1
            except OSError:
                pass
    except Exception as e:
        logger.warning(f"cache trim failed: {e}")


def _protected_files(extra=()):
    """Pick at most the global cache cap's most useful files to retain."""
    ordered = []

    def add(room, item):
        fname = item.get('file') if item else None
        if fname:
            cache_dir = REELS_CACHE_DIR if room.mode == 'reels' else WATCH_CACHE_DIR
            path = os.path.join(cache_dir, fname)
            if fname.startswith('hls/'):
                path = os.path.dirname(path)
            if path not in ordered:
                ordered.append(path)

    # A just-finished/in-progress download is protected until it has had a
    # chance to become current, followed by every room's current item.
    for path in extra:
        if path and path not in ordered:
            ordered.append(path)
    for room in rooms.values():
        add(room, room.current())

    # Ready-ahead items keep forward swipes instant.
    for distance in range(1, REELS_READY_AHEAD + 1):
        for room in rooms.values():
            idx = room.index + distance
            if 0 <= idx < len(room.queue):
                add(room, room.queue[idx])

    # Fill the remaining slots with backward reel history.
    for distance in range(1, REELS_HISTORY + 1):
        for room in rooms.values():
            if room.mode != 'reels':
                continue
            idx = room.index - distance
            if idx >= 0:
                add(room, room.queue[idx])

    return ordered


# ---------------------------------------------------------------- downloads

async def _download_item(room, item):
    """Apply both global and per-room capacity before doing network work."""
    increment('downloads_started_total')
    try:
        async with _global_download_limiter():
            await _download_item_inner(room, item)
        if item.get('status') in ('ready', 'embed'):
            increment('downloads_completed_total')
        else:
            increment('downloads_failed_total')
    except asyncio.CancelledError:
        increment('downloads_cancelled_total')
        raise
    except Exception:
        increment('downloads_failed_total')
        raise


async def _download_item_inner(room, item):
    cache_dir = REELS_CACHE_DIR if room.mode == 'reels' else WATCH_CACHE_DIR
    loop = asyncio.get_running_loop()
    async with room.download_limiter():
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
        hdrs = item.get('_http_headers')
        # Watchdog: a wedged yt-dlp must never hold a download slot forever -
        # that starved the feed and left rooms stuck on one reel.
        dl_timeout = 180 if room.mode == 'reels' else 1800
        try:
            try:
                fname, info = await asyncio.wait_for(loop.run_in_executor(
                    None, _blocking_download, item['url'], cache_dir,
                    room.mode == 'reels', progress_cb,
                    cfg['quality'], cfg['sponsorblock'], hdrs), dl_timeout)
            except Exception:
                if not cfg['sponsorblock'] or room.mode == 'reels':
                    raise
                # SponsorBlock post-processing can fail on its own; retry clean
                fname, info = await asyncio.wait_for(loop.run_in_executor(
                    None, _blocking_download, item['url'], cache_dir,
                    False, progress_cb, cfg['quality'], False, hdrs), dl_timeout)
            item['file'] = fname
            item['vid'] = info.get('id') or item.get('vid')
            item['title'] = info.get('title') or item.get('title')
            item['duration'] = info.get('duration') or item.get('duration')
            item['uploader'] = info.get('uploader') or item.get('uploader')
            if info.get('thumbnail'):
                item['thumbnail'] = info['thumbnail']
            item['tags'] = (info.get('tags') or [])[:10]
            item['width'] = info.get('width')
            item['height'] = info.get('height')
            item['status'] = 'ready'
        except Exception as e:
            # sniffed/picked direct URLs (which carry replay headers) can be
            # disguised media yt-dlp won't touch — ffmpeg copy handles a bare
            # .m3u8/.mp4 regardless of extension or mime
            if hdrs is not None:
                try:
                    item['progress'] = 0
                    fname = await loop.run_in_executor(
                        None, _blocking_ffmpeg_dl, item['url'], hdrs,
                        cache_dir, item['uid'], item.get('_media_kind'))
                    item['file'] = fname
                    item['status'] = 'ready'
                except Exception as e2:
                    logger.error(f"ffmpeg fallback failed for {item.get('url')}: {e2}")
                    _set_embed_fallback(item, e2)
            else:
                logger.error(f"watch download failed for {item.get('url')}: {e}")
                _set_embed_fallback(item, e)
        cache_limit = REELS_CACHE_LIMIT if room.mode == 'reels' else WATCH_CACHE_LIMIT
        _trim_cache(cache_limit, _protected_files([
            os.path.join(cache_dir, item['file']) if item.get('file') else None
        ]), (cache_dir,))
        await _after_ready(room, item)


async def _hls_item(room, item):
    increment('downloads_started_total')
    try:
        async with _global_download_limiter(), room.download_limiter():
            await _hls_item_inner(room, item)
        if item.get('status') == 'ready':
            increment('downloads_completed_total')
        else:
            increment('downloads_failed_total')
    except asyncio.CancelledError:
        increment('downloads_cancelled_total')
        raise
    except Exception:
        increment('downloads_failed_total')
        raise


async def _hls_item_inner(room, item):
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
    _trim_cache(WATCH_CACHE_LIMIT, _protected_files([out_dir]), (WATCH_CACHE_DIR,))
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
    _trim_cache(WATCH_CACHE_LIMIT, _protected_files([out_dir]), (WATCH_CACHE_DIR,))
    await _broadcast_state(room)


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
    if len(room.queue) >= MAX_QUEUE_ITEMS:
        await _notice(room, f'❌ Queue limit reached ({MAX_QUEUE_ITEMS} items)')
        return
    if URL_RE.match(query):
        try:
            await validate_public_url(query)
        except PublicURLRequired as exc:
            await _notice(room, f'❌ {exc}')
            return
    item = {
        'uid': secrets.token_hex(6), 'vid': None, 'url': query,
        'title': query if URL_RE.match(query) else f'🔎 {query}',
        'duration': None, 'thumbnail': None, 'uploader': None,
        'status': 'pending', 'progress': 0, 'file': None,
        'added_by': added_by, 'likes': 0, 'tags': [],
    }
    if room.mode == 'reels':
        # pasted reels queue up right after the current one, FIFO
        pos = max(0, room.index + 1)
        while pos < len(room.queue) and \
                room.queue[pos].get('added_by') != '✨ feed':
            pos += 1
        room.queue.insert(pos, item)
    elif play_next and 0 <= room.index < len(room.queue):
        room.queue.insert(room.index + 1, item)
    else:
        room.queue.append(item)
    await _broadcast_state(room)
    loop = asyncio.get_running_loop()
    try:
        info = await loop.run_in_executor(None, _blocking_extract, query)
    except Exception as e:
        await _sniff_or_fallback(room, item, e)
        return
    if not info:
        await _sniff_or_fallback(room, item, 'No results found')
        return
    item['vid'] = info.get('id')
    item['url'] = info.get('webpage_url') or info.get('url') or query
    item['title'] = info.get('title') or item['title']
    item['duration'] = info.get('duration')
    item['thumbnail'] = info.get('thumbnail')
    item['uploader'] = info.get('uploader')
    item['tags'] = (info.get('tags') or [])[:10]
    if room.mode == 'reels':
        # the feed learns from what people paste and won't re-serve it
        if item['vid']:
            room.seen.add(item['vid'])
        _learn(room, item, 3)
    await _broadcast_state(room)
    if room.mode == 'watch' and (info.get('duration') or 0) >= HLS_MIN_DURATION:
        _spawn(_hls_item(room, item), f'hls:{room.id}:{item["uid"]}')
    else:
        _spawn(_download_item(room, item), f'download:{room.id}:{item["uid"]}')


async def _sniff_or_fallback(room, item, err):
    """yt-dlp couldn't resolve the page. If it's a real URL, read its network
    traffic in a headless browser (DevTools-style) and let the room pick from
    the media it loads — sniffing can catch ads, hence the picker."""
    url = item.get('url') or ''
    if room.mode != 'watch' or not URL_RE.match(url) or not cobrowser:
        _set_embed_fallback(item, err)
        await _after_ready(room, item)
        return
    item['status'] = 'sniffing'
    item['error'] = str(err)[:200]
    await _broadcast_state(room)
    try:
        found = await cobrowser.sniff_media(
            url, adblock=get_settings(room.id)['adblock'])
    except Exception as e:
        logger.warning(f"sniff failed for {url}: {e}")
        found = []
    if not found:
        _set_embed_fallback(item, err)
        await _after_ready(room, item)
        return
    item['choices'] = [{k: m[k] for k in ('url', 'mime', 'kind', 'name')}
                       for m in found]
    item['_choice_headers'] = {m['url']: m.get('headers') or {} for m in found}
    item['status'] = 'choice'
    await _notice(room, f'📹 Found {len(found)} media file(s) on that page — '
                        f'pick the right one (some could be ads)')
    await _after_ready(room, item)


# ---------------------------------------------------------------- reels brain

def _is_vertical(info):
    """Reels must actually be vertical (shorts/reels/tiktok format)."""
    w, h = info.get('width'), info.get('height')
    if w and h:
        return h > w
    ar = info.get('aspect_ratio')
    if ar:
        return ar < 1
    return '/shorts/' in (info.get('webpage_url') or '')


def _tokenize(item):
    words = re.findall(r'[a-zA-ZğüşöçıİĞÜŞÖÇ0-9]{3,}', (item.get('title') or '').lower())
    terms = [w for w in words if w not in STOPWORDS][:6]
    for tag in (item.get('tags') or [])[:4]:
        tag = tag.lower().strip()
        if tag and tag not in STOPWORDS and len(tag) >= 3:
            terms.append(tag)
    # channels people like are one of the strongest signals shorts feeds have
    uploader = re.sub(r'[^a-z0-9ğüşöçı]+', '', (item.get('uploader') or '').lower())
    if len(uploader) >= 3 and uploader not in STOPWORDS:
        terms.append(uploader)
    return terms


def _learn(room, item, delta):
    if not item:
        return
    # Slow decay so yesterday's binge doesn't dominate the feed forever
    for term in list(room.interests):
        w = room.interests[term] * 0.97
        if abs(w) < 0.4:
            room.interests.pop(term, None)
        else:
            room.interests[term] = round(w, 2)
    for term in _tokenize(item):
        w = room.interests.get(term, 0) + delta
        room.interests[term] = max(-5, min(50, w))
    # keep the profile small: drop the weakest terms
    if len(room.interests) > 80:
        for term, _w in sorted(room.interests.items(), key=lambda kv: kv[1])[:20]:
            room.interests.pop(term, None)
    room.save_profile()


def _pick_query(room):
    positive = {k: v for k, v in room.interests.items() if v > 0.5}
    roll = random.random()
    if not positive or roll < 0.15:
        # pure explore: something completely fresh
        query = random.choice(DEFAULT_TOPICS)
    elif roll < 0.30:
        # guided explore: a loved topic crossed with a fresh angle
        weights = [v ** 0.6 for v in positive.values()]
        term = random.choices(list(positive), weights=weights)[0]
        query = f'{term} {random.choice(DEFAULT_TOPICS)}'
    else:
        # exploit - but soften weights so one runaway term can't own the feed
        weights = [v ** 0.6 for v in positive.values()]
        terms = random.choices(list(positive), weights=weights,
                               k=min(2, len(positive)))
        query = ' '.join(dict.fromkeys(terms))
    if query == room.last_query:
        query = random.choice([t for t in DEFAULT_TOPICS if t != query])
    room.last_query = query
    return query


def _viable_ahead(room):
    """Items past the cursor that can still play (dead ones don't count)."""
    return sum(1 for it in room.queue[room.index + 1:]
               if it.get('status') != 'error')


def _prune_dead_feed_items(room):
    """Drop errored feed items so they can't dam the swipe path."""
    removed = False
    i = 0
    while i < len(room.queue):
        it = room.queue[i]
        if (it.get('added_by') == '✨ feed' and it.get('status') == 'error'
                and i != room.index):
            room.queue.pop(i)
            if i < room.index:
                room.index -= 1
            removed = True
            continue
        i += 1
    # cursor parked on a dead feed item: hand it the next viable one
    cur = room.current()
    if (cur and cur.get('added_by') == '✨ feed' and cur.get('status') == 'error'
            and room.index + 1 < len(room.queue)):
        room.queue.pop(room.index)
        room._advanced_past = room.index - 1
        room.set_position(0, playing=True)
        removed = True
    return removed


def _entry_score(room, entry):
    """Rank flat search results against what the room liked/disliked."""
    title = (entry.get('title') or '').lower()
    words = set(re.findall(r'[a-zA-ZğüşöçıİĞÜŞÖÇ0-9]{3,}', title))
    score = sum(room.interests.get(w, 0) for w in words if w not in STOPWORDS)
    uploader = (entry.get('uploader') or entry.get('channel') or '').strip().lower()
    if uploader and uploader in room.recent_uploaders:
        # Repetition is allowed, just substantially less likely.
        score -= 4 + list(room.recent_uploaders).count(uploader)
    return score


async def _reels_topup(room):
    if room._topup_busy:
        return
    room._topup_busy = True
    try:
        await _reels_topup_inner(room)
    finally:
        room._topup_busy = False


async def _reels_topup_inner(room):
    if _prune_dead_feed_items(room):
        await _broadcast_state(room)
    if _viable_ahead(room) >= REELS_READY_AHEAD:
        return
    loop = asyncio.get_running_loop()
    query = _pick_query(room)
    missing = max(0, REELS_READY_AHEAD - _viable_ahead(room))
    try:
        entries = await loop.run_in_executor(
            None, _blocking_search, f'{query} #shorts', 20)
    except Exception as e:
        logger.warning(f"reels search failed ({query!r}): {e}")
        return
    # Best matches first (learned dislikes filter out), with a little jitter
    # so the same channels don't front-run every search.
    entries = [e for e in entries if _entry_score(room, e) > -3]
    entries.sort(key=lambda e: _entry_score(room, e) + random.uniform(0, 2),
                 reverse=True)
    added = 0
    checked = 0
    for e in entries:
        if (added >= missing or checked >= 14 or
                _viable_ahead(room) >= REELS_READY_AHEAD or
                len(room.queue) >= MAX_QUEUE_ITEMS):
            break
        vid = e.get('id')
        dur = e.get('duration')
        if not vid or vid in room.seen:
            continue
        if dur and dur > REELS_MAX_DURATION:
            continue
        room.seen.add(vid)
        checked += 1
        # flat search results carry no dimensions - fully resolve each
        # candidate and only accept actual VERTICAL shorts
        url = e.get('url') or f'https://www.youtube.com/watch?v={vid}'
        try:
            info = await loop.run_in_executor(None, _blocking_extract, url)
        except Exception:
            continue
        if not info or not _is_vertical(info):
            continue
        if (info.get('duration') or 0) > REELS_MAX_DURATION:
            continue
        item = {
            'uid': secrets.token_hex(6), 'vid': info.get('id') or vid,
            'url': info.get('webpage_url') or url,
            'title': info.get('title') or 'Reel',
            'duration': info.get('duration'),
            'thumbnail': info.get('thumbnail'),
            'uploader': info.get('uploader') or info.get('channel'),
            'width': info.get('width'), 'height': info.get('height'),
            'status': 'pending', 'progress': 0, 'file': None,
            'added_by': '✨ feed', 'likes': 0,
            'tags': (info.get('tags') or [])[:10],
        }
        room.queue.append(item)
        uploader_key = (item.get('uploader') or '').strip().lower()
        if uploader_key:
            room.recent_uploaders.append(uploader_key)
        added += 1
        _spawn(_download_item(room, item), f'download:{room.id}:{item["uid"]}')
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


def _clean_playlist_name(value):
    return re.sub(r'\s+', ' ', str(value or '').strip())[:50]


async def _save_room_playlist(room, name, member):
    name = _clean_playlist_name(name)
    if not name:
        await _notice(room, '❌ Give the playlist a name')
        return
    playlists = room_playlists.setdefault(room.id, {})
    if name not in playlists and len(playlists) >= MAX_ROOM_PLAYLISTS:
        await _notice(room, f'❌ Playlist limit reached ({MAX_ROOM_PLAYLISTS})')
        return
    existing = playlists.get(name)
    if existing and not _can_edit_room_playlist(existing, member):
        await _notice(room, f'🔒 {member["name"]}, only the owner or a moderator can update “{name}”')
        return
    items = []
    for item in room.queue[:MAX_PLAYLIST_ITEMS]:
        url = str(item.get('url') or '')
        if not URL_RE.match(url):
            continue
        items.append({
            'url': url,
            'title': str(item.get('title') or '')[:200],
            'duration': item.get('duration'),
            'thumbnail': item.get('thumbnail'),
            'uploader': item.get('uploader'),
        })
    if not items:
        await _notice(room, '❌ There are no reusable URLs in the queue')
        return
    revisions = list((existing or {}).get('revisions') or [])[-9:]
    if existing and existing.get('items'):
        revisions.append({'items': existing['items'], 'updated_at': existing.get('updated_at'),
                          'updated_by': existing.get('updated_by')})
    playlists[name] = {
        'items': items,
        'owner_key': (existing or {}).get('owner_key') or _member_owner_key(member),
        'owner_name': (existing or {}).get('owner_name') or member['name'],
        'editor_keys': list((existing or {}).get('editor_keys') or []),
        'editor_names': list((existing or {}).get('editor_names') or []),
        'revisions': revisions,
        'updated_by': member['name'],
        'updated_at': time.time(),
    }
    _save_room_playlists()
    increment('playlists_saved_total')
    await _notice(room, f'💾 {member["name"]} saved “{name}” ({len(items)} items)')
    await _broadcast_state(room)


async def _load_room_playlist(room, name, member):
    playlist = room_playlists.get(room.id, {}).get(_clean_playlist_name(name))
    if not playlist:
        await _notice(room, '❌ Playlist not found')
        return
    available = max(0, MAX_QUEUE_ITEMS - len(room.queue))
    entries = list(playlist.get('items') or [])[:min(MAX_PLAYLIST_ITEMS, available)]
    if not entries:
        await _notice(room, '❌ The queue is full')
        return
    await _notice(room, f'📚 {member["name"]} is loading “{name}”…')
    increment('playlists_loaded_total')
    for entry in entries:
        if task_registry.closing:
            return
        await _add_query(room, str(entry.get('url') or ''), member['name'])


async def _delete_room_playlist(room, name, member):
    name = _clean_playlist_name(name)
    playlists = room_playlists.get(room.id, {})
    if name not in playlists:
        await _notice(room, '❌ Playlist not found')
        return
    if not _can_edit_room_playlist(playlists[name], member):
        await _notice(room, f'🔒 Only the owner or a moderator can delete “{name}”')
        return
    playlists.pop(name, None)
    if not playlists:
        room_playlists.pop(room.id, None)
    _save_room_playlists()
    increment('playlists_deleted_total')
    await _notice(room, f'🗑️ {member["name"]} deleted “{name}”')
    await _broadcast_state(room)


async def _playlist_manage(room, member, data, ws):
    action = str(data.get('action') or '')
    name = _clean_playlist_name(data.get('name'))
    playlists = room_playlists.setdefault(room.id, {})
    playlist = playlists.get(name)
    if action == 'import':
        new_name = name or 'Imported playlist'
        if len(playlists) >= MAX_ROOM_PLAYLISTS:
            await _notice(room, f'❌ Playlist limit reached ({MAX_ROOM_PLAYLISTS})')
            return
        if new_name in playlists:
            await _notice(room, '❌ A playlist with that name already exists')
            return
        raw_items = list(data.get('items') or [])[:MAX_PLAYLIST_ITEMS]
        items = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            url = str(raw.get('url') or '').strip()[:1000]
            if URL_RE.match(url):
                items.append({'url': url, 'title': str(raw.get('title') or url)[:200],
                              'duration': raw.get('duration'),
                              'thumbnail': raw.get('thumbnail'), 'uploader': raw.get('uploader')})
        if not items:
            await _notice(room, '❌ Import contains no valid media URLs')
            return
        playlists[new_name] = {'items': items, 'owner_key': _member_owner_key(member),
            'owner_name': member['name'], 'revisions': [], 'updated_by': member['name'],
            'updated_at': time.time()}
        _save_room_playlists()
        await _notice(room, f'📥 {member["name"]} imported “{new_name}”')
    elif not playlist:
        await _notice(room, '❌ Playlist not found')
        return
    elif action == 'export':
        await ws.send_str(json.dumps({'t': 'playlist_export', 'name': name,
            'items': playlist.get('items') or []}))
        return
    elif not _can_edit_room_playlist(playlist, member):
        await _notice(room, '🔒 Only the owner or a moderator can edit that playlist')
        return
    elif action in ('editor_add', 'editor_remove'):
        if playlist.get('owner_key') != _member_owner_key(member) and not _can_moderate(member):
            await _notice(room, '🔒 Only the owner or a moderator can manage editors')
            return
        target_id = re.sub(r'[^A-Za-z0-9_-]', '', str(data.get('id') or ''))[:64]
        target = next((value for value in room.sockets.values()
                       if value['id'] == target_id), None)
        if not target:
            await _notice(room, '❌ That participant is no longer connected')
            return
        target_key = _member_owner_key(target)
        editor_keys = playlist.setdefault('editor_keys', [])
        editor_names = playlist.setdefault('editor_names', [])
        if action == 'editor_add':
            if target_key not in editor_keys:
                editor_keys.append(target_key)
            if target['name'] not in editor_names:
                editor_names.append(target['name'])
            await _notice(room, f'🤝 {target["name"]} can now edit “{name}”')
        else:
            if target_key in editor_keys:
                editor_keys.remove(target_key)
            if target['name'] in editor_names:
                editor_names.remove(target['name'])
            await _notice(room, f'🔒 {target["name"]} can no longer edit “{name}”')
        playlist['updated_at'] = time.time()
        _save_room_playlists()
    elif action == 'rename':
        new_name = _clean_playlist_name(data.get('new_name'))
        if not new_name or new_name in playlists:
            await _notice(room, '❌ Choose a new, unused playlist name')
            return
        playlists[new_name] = playlists.pop(name)
        name = new_name
        _save_room_playlists()
        await _notice(room, f'✏️ Playlist renamed to “{name}”')
    elif action == 'duplicate':
        new_name = _clean_playlist_name(data.get('new_name')) or f'{name} copy'
        if new_name in playlists or len(playlists) >= MAX_ROOM_PLAYLISTS:
            await _notice(room, '❌ Choose a new, unused playlist name')
            return
        playlists[new_name] = {'items': list(playlist.get('items') or []),
            'owner_key': _member_owner_key(member), 'owner_name': member['name'],
            'revisions': [], 'updated_by': member['name'], 'updated_at': time.time()}
        _save_room_playlists()
        await _notice(room, f'📋 Duplicated as “{new_name}”')
    elif action == 'undo':
        revisions = playlist.get('revisions') or []
        if not revisions:
            await _notice(room, '❌ There is no earlier revision')
            return
        previous = revisions.pop()
        playlist['items'] = previous.get('items') or []
        playlist['updated_by'] = member['name']
        playlist['updated_at'] = time.time()
        _save_room_playlists()
        await _notice(room, f'↩️ Restored the previous version of “{name}”')
    await _broadcast_state(room)


# ---------------------------------------------------------------- ws handler

async def _handle_browser_message(room, ws, member, data):
    """Handle the co-browser/settings protocol; return whether it was handled."""
    event = data.get('t')
    browser_events = {
        'browser_start', 'settings', 'browser_stop', 'browser_control',
        'browser_nav', 'bmouse', 'bkey', 'browser_media', 'browser_pick',
        'pick_choice',
    }
    if event not in browser_events:
        return False
    name = member['name']
    if not _can_moderate(member):
        await _notice(room, f'🔒 {name}, that action requires a moderator')
        increment('role_denials_total')
        return True
    sess = cobrowser.get(room.id) if cobrowser else None

    if event == 'browser_start':
        if not cobrowser:
            await _notice(room, '❌ Browser mode is not available on this server')
            return True
        url = str(data.get('url') or '').strip()[:500]
        if url:
            try:
                await validate_public_url(url)
            except PublicURLRequired as exc:
                await _notice(room, f'❌ {exc}')
                return True
        if sess:
            if url:
                await sess.navigate(url)
            return True
        room.set_position(room.get_position(), playing=False)
        await _notice(room, f'🌐 {name} is starting the shared browser…')
        await _broadcast_state(room)

        async def on_change():
            await _broadcast_state(room)
        try:
            cfg = get_settings(room.id)
            await cobrowser.start(room.id, url, started_by=name,
                                  on_change=on_change, adblock=cfg['adblock'],
                                  quality=cfg['quality'])
        except Exception as exc:
            logger.exception('Shared browser failed to start')
            await _notice(room, f'❌ Browser failed to start: {exc}')
        await _broadcast_state(room)

    elif event == 'settings':
        if not _can_moderate(member):
            await _notice(room, f'🔒 {name}, room settings require a moderator')
            increment('role_denials_total')
            return True
        cfg = get_settings(room.id)
        changed = []
        for key in ('adblock', 'sponsorblock'):
            if key in data and bool(data[key]) != cfg[key]:
                cfg[key] = bool(data[key])
                changed.append(f"{key} {'on' if cfg[key] else 'off'}")
        try:
            quality = int(data.get('quality')) if 'quality' in data else 0
        except (TypeError, ValueError):
            quality = 0
        if quality in (360, 480, 720, 1080) and quality != cfg['quality']:
            cfg['quality'] = quality
            changed.append(f'quality {quality}p')
        if _is_host(member):
            if 'room_name' in data:
                room_name = re.sub(r'\s+', ' ', str(data.get('room_name') or '').strip())[:50]
                if room_name and room_name != room.name:
                    room.name = room_name
                    for token_info in tokens.values():
                        if token_info.get('room') == room.id:
                            token_info['name'] = room_name
                    _save_tokens()
                    changed.append(f'room name: {room_name}')
            control_policy = str(data.get('control_policy') or '')
            skip_policy = str(data.get('skip_policy') or '')
            if control_policy in ('host', 'moderators', 'everyone') \
                    and control_policy != cfg['control_policy']:
                cfg['control_policy'] = control_policy
                changed.append(f'controls: {control_policy}')
            if skip_policy in ('moderators', 'vote', 'everyone') \
                    and skip_policy != cfg['skip_policy']:
                cfg['skip_policy'] = skip_policy
                room.skip_votes.clear()
                changed.append(f'skipping: {skip_policy}')
            if 'vote_threshold' in data:
                threshold = max(0.25, min(1.0, _safe_float(
                    data.get('vote_threshold'), cfg['vote_threshold'], 0.25, 1.0)))
                if threshold != cfg['vote_threshold']:
                    cfg['vote_threshold'] = threshold
                    changed.append(f'vote threshold: {round(threshold * 100)}%')
        if changed:
            room_settings[room.id] = cfg
            _save_room_settings()
            await _notice(room, f'⚙️ {name} set ' + ', '.join(changed))
            await _broadcast_state(room)

    elif event == 'browser_stop' and sess:
        await _notice(room, f'🌐 {name} closed the shared browser')
        await sess.stop()
    elif event == 'browser_control' and sess:
        sess.controller = name
        await _notice(room, f'🖱️ {name} took control of the browser')
        await _broadcast_state(room)
    elif event == 'browser_nav' and sess and sess.controller == name:
        url = str(data.get('url') or '').strip()[:500]
        if url:
            try:
                await validate_public_url(url)
                await sess.navigate(url)
            except PublicURLRequired as exc:
                await _notice(room, f'❌ {exc}')
    elif event == 'bmouse' and sess and sess.controller == name:
        try:
            action = data.get('a', 'move')
            x = max(0.0, min(1.0, float(data.get('x') or 0)))
            y = max(0.0, min(1.0, float(data.get('y') or 0)))
            button = max(1, min(5, int(data.get('b') or 1)))
            delta = max(-1000.0, min(1000.0, float(data.get('dy') or 0)))
        except (TypeError, ValueError):
            return True
        sess.mouse(action, x, y, button, delta)
        if action in ('move', 'down', 'up'):
            await _broadcast(room, {'t': 'bcursor', 'x': x, 'y': y,
                                    'down': action == 'down'})
    elif event == 'bkey' and sess and sess.controller == name:
        key = str(data.get('key') or '')[:32]
        # Browser chrome is kiosked and navigation must pass browser_nav URL
        # validation. Modifier/location shortcuts would bypass that boundary.
        if key not in {'Control', 'Alt', 'Meta', 'F4', 'F6'}:
            sess.key(data.get('a', 'down'), key)
    elif event == 'browser_media' and sess:
        try:
            await sess.probe_candidates()
        except Exception:
            logger.debug('Browser media probe failed', exc_info=True)
        await ws.send_str(json.dumps(
            {'t': 'bmedia', 'items': sess.get_media(), 'page': sess.page_url}))
    elif event == 'browser_pick' and sess:
        url = str(data.get('url') or '').strip()
        if not url:
            return True
        try:
            await validate_public_url(url)
        except PublicURLRequired as exc:
            await _notice(room, f'❌ {exc}')
            return True
        if len(room.queue) >= MAX_QUEUE_ITEMS:
            await _notice(room, f'❌ Queue limit reached ({MAX_QUEUE_ITEMS} items)')
            return True
        await _notice(room, f'📹 {name} grabbed media from the page')
        item = {
            'uid': secrets.token_hex(6), 'vid': None, 'url': url,
            'title': url.split('?')[0].rstrip('/').split('/')[-1] or url,
            'duration': None, 'thumbnail': None,
            'uploader': ((sess.page_url or '').split('/')[2]
                         if '://' in (sess.page_url or '') else None),
            'status': 'pending', 'progress': 0, 'file': None,
            'added_by': name, 'likes': 0, 'tags': [],
            '_http_headers': sess.media_headers(url),
            '_media_kind': sess.media_kind(url),
        }
        room.queue.append(item)
        await _broadcast_state(room)
        _spawn(_download_item(room, item), f'download:{room.id}:{item["uid"]}')
    elif event == 'pick_choice':
        uid = str(data.get('uid') or '')
        url = str(data.get('url') or '')
        item = next((entry for entry in room.queue if entry['uid'] == uid), None)
        if item and url and item.get('choices') and \
                any(choice['url'] == url for choice in item['choices']):
            try:
                await validate_public_url(url)
            except PublicURLRequired as exc:
                await _notice(room, f'❌ {exc}')
                return True
            chosen = next(choice for choice in item['choices'] if choice['url'] == url)
            item['_http_headers'] = (item.get('_choice_headers') or {}).get(url) or {}
            item['_http_headers'].setdefault('Referer', item['url'])
            item['_media_kind'] = chosen.get('kind')
            item['url'] = url
            item['title'] = chosen.get('name') or item['title']
            item['status'] = 'pending'
            item['choices'] = None
            item.pop('_choice_headers', None)
            await _notice(room, f"📹 {name} picked: {chosen.get('name', 'media')}")
            await _broadcast_state(room)
            _spawn(_download_item(room, item), f'download:{room.id}:{item["uid"]}')
    return True

async def ws_handler(request):
    room_id = request.query.get('room', '')
    token = _request_room_token(request, room_id)
    info = _token_room(token)
    if not info or info['room'] != room_id:
        raise web.HTTPUnauthorized(text='bad token')

    try:
        room = _get_room(room_id, info.get('name'))
    except RuntimeError as exc:
        raise web.HTTPServiceUnavailable(text=str(exc))
    if len(room.sockets) >= MAX_ROOM_PARTICIPANTS:
        raise web.HTTPServiceUnavailable(text='room participant limit reached')

    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    name = 'Guest'
    member = None
    joined = False
    member_session = re.sub(
        r'[^A-Za-z0-9_-]', '',
        request.cookies.get(_room_member_cookie_name(room_id), ''),
    )[:64] or secrets.token_urlsafe(24)

    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            t = data.get('t')
            increment('websocket_messages_total')
            room.last_activity = time.monotonic()
            if not message_limiter.allow((room.id, id(ws))):
                await ws.send_str(json.dumps(
                    {'t': 'chat', 'name': None,
                     'text': '⚠️ Slow down for a moment.', 'ts': time.time()}))
                continue

            if t == 'join':
                name = str(data.get('name') or 'Guest')[:24].strip() or 'Guest'
                member = room.join(ws, member_session, name)
                joined = True
                increment('websocket_joins_total')
                if room.sync_task is None:
                    room.sync_task = _spawn(_sync_loop(room), f'sync:{room.id}')
                if room.mode == 'reels' and room.reels_task is None \
                        and (room.interests or room.queue):
                    room.reels_task = _spawn(_reels_loop(room), f'reels:{room.id}')
                await ws.send_str(json.dumps({'t': 'welcome', 'id': member['id']}))
                await ws.send_str(json.dumps(_room_state(room)))
                await _notice(room, f'👋 {name} joined as {member["role"]}')
                await _broadcast_state(room)
                continue

            if not joined:
                continue

            if room.mode == 'watch' and t in {'play', 'pause', 'seek', 'jump'} and \
                    not _can_control(room, member):
                await _notice(room, f'🔒 {name}, playback controls require a moderator under this room policy')
                increment('role_denials_total')
                continue

            if t == 'chat':
                text = str(data.get('text') or '')[:500].strip()
                if member.get('muted_until', 0) > time.time():
                    await ws.send_str(json.dumps({'t': 'chat', 'name': None,
                        'text': '🔇 You are temporarily muted.', 'ts': time.time()}))
                elif text:
                    entry = {'name': name, 'text': text, 'ts': time.time()}
                    room.chat.append(entry)
                    await _broadcast(room, dict(entry, t='chat'))

            elif t == 'play':
                sess = cobrowser.get(room.id) if cobrowser else None
                if sess:   # resuming the video ends browser mode
                    await sess.stop()
                room.set_position(_safe_float(data.get('pos'), room.get_position()),
                                  playing=True)
                await _broadcast(room, {'t': 'sync', 'index': room.index,
                                        'playing': True,
                                        'position': round(room.position, 2),
                                        'by': name, 'action': 'play'})

            elif t == 'pause':
                room.set_position(_safe_float(data.get('pos'), room.get_position()),
                                  playing=False)
                await _broadcast(room, {'t': 'sync', 'index': room.index,
                                        'playing': False,
                                        'position': round(room.position, 2),
                                        'by': name, 'action': 'pause'})

            elif t == 'seek':
                room.set_position(_safe_float(data.get('pos')))
                await _broadcast(room, {'t': 'sync', 'index': room.index,
                                        'playing': room.playing,
                                        'position': round(room.position, 2),
                                        'by': name, 'action': 'seek'})

            elif t == 'add':
                q = str(data.get('q') or '').strip()
                if q:
                    await _notice(room, f'➕ {name} added: {q[:80]}')
                    _spawn(_add_query(room, q, name, bool(data.get('next'))),
                           f'add:{room.id}')

            elif t == 'jump':
                idx = _safe_int(data.get('index'))
                if 0 <= idx < len(room.queue):
                    sess = cobrowser.get(room.id) if cobrowser else None
                    if sess:
                        await sess.stop()
                    room.skip_votes.clear()
                    room.index = idx
                    room._advanced_past = idx - 1
                    room.set_position(0, playing=True)
                    await _broadcast_state(room)

            elif t == 'skip':
                skip_policy = get_settings(room.id).get('skip_policy', 'vote')
                immediate = _can_moderate(member) or skip_policy == 'everyone'
                if not immediate and skip_policy == 'moderators':
                    await _notice(room, f'🔒 {name}, skipping requires a moderator')
                    continue
                if not immediate:
                    room.skip_votes.add(member['session'])
                    needed = max(1, math.ceil(
                        len(room.sockets) * get_settings(room.id)['vote_threshold']))
                    if len(room.skip_votes) < needed:
                        await _notice(room, f'🗳️ {name} voted to skip ({len(room.skip_votes)}/{needed})')
                        await _broadcast_state(room)
                        continue
                    await _notice(room, f'🗳️ Skip vote passed ({len(room.skip_votes)}/{needed})')
                room.skip_votes.clear()
                if room.index + 1 < len(room.queue):
                    room.index += 1
                    room.set_position(0, playing=True)
                else:
                    room.set_position(room.get_position(), playing=False)
                room._advanced_past = room.index - 1
                await _notice(room, f'⏭ {name} skipped')
                await _broadcast_state(room)

            elif t == 'reorder':
                if not _can_control(room, member):
                    await _notice(room, f'🔒 {name}, reordering is not allowed by the room policy')
                    continue
                source = _safe_int(data.get('from'))
                target = _safe_int(data.get('to'))
                if 0 <= source < len(room.queue) and 0 <= target < len(room.queue) \
                        and source != room.index and target != room.index:
                    current = room.current()
                    item = room.queue.pop(source)
                    room.queue.insert(target, item)
                    if current in room.queue:
                        room.index = room.queue.index(current)
                    await _broadcast_state(room)

            elif t == 'remove':
                if not _can_moderate(member):
                    await _notice(room, f'🔒 {name}, removing items requires a moderator')
                    increment('role_denials_total')
                    continue
                idx = _safe_int(data.get('index'))
                if 0 <= idx < len(room.queue) and idx != room.index:
                    room.queue.pop(idx)
                    if idx < room.index:
                        room.index -= 1
                    await _broadcast_state(room)

            elif t == 'remove_current':
                if not _can_moderate(member):
                    await _notice(room, f'🔒 {name}, removing items requires a moderator')
                    increment('role_denials_total')
                    continue
                # drop the item being watched (e.g. a dead/unpickable one)
                if 0 <= room.index < len(room.queue):
                    room.skip_votes.clear()
                    room.queue.pop(room.index)
                    if room.index >= len(room.queue):
                        room.index = len(room.queue) - 1
                    room._advanced_past = room.index - 1
                    room.set_position(0, playing=room.index >= 0)
                    await _notice(room, f'🗑 {name} removed the current item')
                    await _broadcast_state(room)

            elif t == 'ended':
                idx = _safe_int(data.get('index'))
                if idx == room.index and idx > room._advanced_past:
                    room.skip_votes.clear()
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
                        room.reels_task = _spawn(_reels_loop(room), f'reels:{room.id}')
                    await _broadcast_state(room)

            elif t == 'swipe' and room.mode == 'reels':
                cur = room.current()
                watched = _safe_float(data.get('watched'))
                dur = _safe_float(data.get('dur')) or (cur or {}).get('duration') or 0
                if cur and dur:
                    ratio = watched / dur
                    if ratio < 0.35:
                        _learn(room, cur, -1)
                    elif ratio >= 0.9:
                        _learn(room, cur, 1)
                # land on the next item that can actually play
                nxt = room.index + 1
                while nxt < len(room.queue) and \
                        room.queue[nxt].get('status') == 'error':
                    nxt += 1
                if nxt < len(room.queue):
                    room.index = nxt
                    room._advanced_past = room.index - 1
                    room.set_position(0, playing=True)
                else:
                    # out of reels: fetch more right now instead of waiting
                    # for the next loop tick
                    _spawn(_reels_topup(room), f'topup:{room.id}')
                await _broadcast_state(room)

            elif t == 'swipe_back' and room.mode == 'reels':
                # Move backward through reels that are still playable. Recent
                # history is protected from cache trimming, so this normally
                # permits several consecutive backward swipes.
                prev = room.index - 1
                while prev >= 0:
                    item = room.queue[prev]
                    playable = item.get('status') == 'embed'
                    if item.get('file'):
                        playable = os.path.isfile(os.path.join(REELS_CACHE_DIR,
                                                               item['file']))
                    if playable:
                        break
                    prev -= 1
                if prev >= 0:
                    room.index = prev
                    room._advanced_past = room.index - 1
                    room.set_position(0, playing=True)
                    await _broadcast_state(room)
                else:
                    await _notice(room, '⏮ This is the oldest available reel')

            elif t == 'like' and room.mode == 'reels':
                cur = room.current()
                if cur:
                    cur['likes'] = cur.get('likes', 0) + 1
                    _learn(room, cur, 2)
                    await _broadcast(room, {'t': 'heart', 'by': name,
                                            'uid': cur['uid'],
                                            'likes': cur['likes']})

            elif t == 'playlist_save':
                async with _room_playlist_lock(room):
                    await _save_room_playlist(room, data.get('name'), member)

            elif t == 'playlist_load':
                playlist_name = _clean_playlist_name(data.get('name'))
                _spawn(_load_room_playlist(room, playlist_name, member),
                       f'playlist:{room.id}:{playlist_name}')

            elif t == 'playlist_delete':
                async with _room_playlist_lock(room):
                    await _delete_room_playlist(room, data.get('name'), member)

            elif t == 'playlist_manage':
                async with _room_playlist_lock(room):
                    await _playlist_manage(room, member, data, ws)

            elif t == 'role_set':
                if not _is_host(member):
                    await _notice(room, f'🔒 {name}, only the host can change roles')
                    increment('role_denials_total')
                    continue
                target_id = re.sub(r'[^A-Za-z0-9_-]', '',
                                   str(data.get('id') or ''))[:64]
                role = str(data.get('role') or '')
                target = next((value for value in room.sockets.values()
                               if value['id'] == target_id), None)
                if not target or role not in ('viewer', 'moderator', 'host'):
                    continue
                if role == 'host':
                    if target['id'] == member['id']:
                        continue
                    member['role'] = 'moderator'
                    room.roles[member['session']] = 'moderator'
                elif target['role'] == 'host':
                    continue
                target['role'] = role
                room.roles[target['session']] = role
                await _notice(room, f'🛡️ {target["name"]} is now {role}')
                await _broadcast_state(room)

            elif t == 'participant_action':
                if not _is_host(member):
                    await _notice(room, f'🔒 {name}, only the host can manage participants')
                    continue
                target_id = re.sub(r'[^A-Za-z0-9_-]', '', str(data.get('id') or ''))[:64]
                pair = next(((socket, value) for socket, value in room.sockets.items()
                             if value['id'] == target_id), None)
                if not pair or pair[1]['id'] == member['id']:
                    continue
                target_socket, target = pair
                action = str(data.get('action') or '')
                if action in ('viewer', 'moderator', 'host'):
                    if action == 'host':
                        member['role'] = 'moderator'
                        room.roles[member['session']] = 'moderator'
                    target['role'] = action
                    room.roles[target['session']] = action
                    await _notice(room, f'🛡️ {target["name"]} is now {action}')
                elif action == 'mute':
                    target['muted_until'] = time.time() + 300
                    await _notice(room, f'🔇 {target["name"]} was muted for 5 minutes')
                elif action == 'unmute':
                    target['muted_until'] = 0
                    await _notice(room, f'🔊 {target["name"]} was unmuted')
                elif action == 'kick':
                    await _notice(room, f'👋 {target["name"]} was removed by the host')
                    await target_socket.close(code=4003, message=b'removed by host')
                await _broadcast_state(room)

            elif await _handle_browser_message(room, ws, member, data):
                pass
    finally:
        room.sockets.pop(ws, None)
        if member:
            room.skip_votes.discard(member.get('session'))
        room.last_activity = time.monotonic()
        message_limiter.discard((room.id, id(ws)))
        if joined:
            promoted = room.ensure_active_host()
            await _notice(room, f'💨 {name} left')
            if promoted:
                replacement, _old_role = promoted
                await _notice(room, f'👑 {replacement["name"]} is now the host')
            await _broadcast_state(room)
    return ws


# ---------------------------------------------------------------- http routes

async def watch_page(request):
    return web.Response(text=WATCH_HTML, content_type='text/html', headers={
        'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})


async def reels_page(request):
    return web.Response(text=REELS_HTML, content_type='text/html', headers={
        'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})


async def api_session(request):
    """Exchange a link fragment for a room-scoped HttpOnly cookie."""
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text='invalid json')
    room_id = str(body.get('room') or '')[:80]
    token = str(body.get('token') or '')
    info = _token_room(token)
    if not info or info.get('room') != room_id:
        raise web.HTTPUnauthorized(text='bad token')
    response = web.json_response({'ok': True})
    forwarded_proto = request.headers.get('X-Forwarded-Proto', '')
    cookies = getattr(request, 'cookies', {})
    member_session = re.sub(
        r'[^A-Za-z0-9_-]', '',
        cookies.get(_room_member_cookie_name(room_id), ''),
    )[:64] or secrets.token_urlsafe(24)
    secure = request.secure or forwarded_proto == 'https'
    response.set_cookie(
        _room_cookie_name(room_id), token, max_age=TOKEN_TTL,
        httponly=True, secure=secure,
        samesite='Strict', path='/watch/')
    response.set_cookie(
        _room_member_cookie_name(room_id), member_session, max_age=TOKEN_TTL,
        httponly=True, secure=secure, samesite='Strict', path='/watch/')
    return response


async def api_create(request):
    """Standalone rooms: anyone on the landing page can create one."""
    client = _client_key(request)
    if not create_limiter.allow(client):
        return web.json_response({'error': 'too many rooms created; try later'},
                                 status=429)
    try:
        body = await request.json()
    except Exception:
        body = {}
    mode = 'reels' if body.get('mode') == 'reels' else 'watch'
    name = str(body.get('name') or '').strip()[:40] or \
        ('📱 Reels Party' if mode == 'reels' else '🍿 Watch Party')
    room_id = ('r' if mode == 'reels' else 'w') + 'p' + secrets.token_hex(5)
    token = secrets.token_urlsafe(12)
    now = time.time()
    for key, value in list(tokens.items()):
        if now - value.get('created_at', 0) >= TOKEN_TTL:
            tokens.pop(key, None)
    if len(tokens) >= MAX_ROOM_TOKENS:
        return web.json_response({'error': 'server room limit reached'}, status=503)
    tokens[token] = {'room': room_id, 'name': name, 'created_at': now}
    _save_tokens()
    base = getattr(config, 'WEB_SERVER_URL', 'https://deeppixel.online').rstrip('/')
    path = '/watch/' if mode == 'watch' else '/watch/reels'
    return web.json_response({'url': f'{base}{path}?room={room_id}#token={token}'})


async def media(request):
    # HLS segment requests are playlist-relative and carry no query string,
    # so the page also stores the token in a cookie
    room_id = request.match_info['room_id']
    token = _request_room_token(request, room_id)
    info = _token_room(token)
    if not info or info.get('room') != room_id:
        raise web.HTTPUnauthorized(text='bad token')
    room = rooms.get(room_id)
    rel = request.match_info['file']
    if not room or not _room_owns_file(room, rel):
        raise web.HTTPForbidden(text='media does not belong to this room')
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
    token = _request_room_token(request, room_id)
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


def runtime_stats():
    return {
        'rooms': len(rooms),
        'participants': sum(len(room.sockets) for room in rooms.values()),
        'queued_items': sum(len(room.queue) for room in rooms.values()),
        'background_tasks': task_registry.active,
        'browser_sessions': len(cobrowser.sessions) if cobrowser else 0,
    }


def metrics_text():
    return prometheus(runtime_stats())


async def shutdown(_app=None):
    """Stop accepting room work and release every external process/task."""
    logger.info('Stopping Watch Together (%s rooms, %s tasks)',
                len(rooms), task_registry.active)
    sockets = [ws for room in rooms.values() for ws in room.sockets]
    for ws in sockets:
        try:
            await ws.close(code=1001, message=b'server shutting down')
        except Exception:
            pass
    if cobrowser:
        try:
            await cobrowser.stop_all()
        except Exception:
            logger.exception('Shared browsers did not all stop cleanly')
    await task_registry.cancel_all()
    _save_profiles()
    _save_room_playlists()
    rooms.clear()
    increment('graceful_shutdowns_total')


def setup(app, bot=None):
    import shutil
    global task_registry, global_download_sem
    if task_registry.closing:
        task_registry = TaskRegistry('watch')
        global_download_sem = None
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
    app.router.add_post('/watch/api/session', api_session)
    app.router.add_post('/watch/api/create', api_create)
    app.router.add_get('/watch/media/{room_id}/{file:.+}', media)
    app.on_shutdown.append(shutdown)
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
  .playlistbar{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0 12px}
  .playlistbar input,.playlistbar select{background:var(--panel2);border:1px solid #333350;
    color:var(--text);border-radius:8px;padding:8px 10px;min-width:120px}
  .qitem{display:flex;align-items:center;gap:10px;padding:8px 6px;border-bottom:1px solid #2a2a40;
         font-size:14px;border-radius:6px}
  .qitem:last-child{border-bottom:none}
  .qitem.now{background:var(--panel2)}
  .qitem img{width:56px;height:32px;object-fit:cover;border-radius:4px;background:var(--panel2)}
  .qitem .t{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:pointer}
  .qitem .s{color:var(--muted);font-size:12px;white-space:nowrap}
  .qitem .x{color:var(--danger);cursor:pointer;padding:2px 6px}
  .queue-move{padding:4px 7px;font-size:12px}
  .qitem[draggable=true]{cursor:grab}.qitem.dragging{opacity:.45}.qitem.drag-over{box-shadow:inset 0 2px var(--accent)}
  .chip.host{color:#ffd166}.chip.moderator{color:#71d7ff}
  button:focus-visible,input:focus-visible,select:focus-visible,.chip:focus-visible{outline:3px solid var(--accent);outline-offset:2px}
  @media(prefers-reduced-motion:reduce){*,*::before,*::after{animation-duration:.01ms!important;transition-duration:.01ms!important}}
  .chatbox{background:var(--panel);border-radius:var(--radius);display:flex;flex-direction:column;
           height:calc(100vh - 120px);min-height:420px;position:sticky;top:12px}
  @media(max-width:900px){
    .chatbox{height:380px;position:static}
    .top{position:sticky;top:0;z-index:20;background:color-mix(in srgb,var(--bg) 88%,transparent);backdrop-filter:blur(18px)}
    .playlistbar input,.playlistbar select{flex:1 1 160px}
    .qitem{gap:6px}.qitem img{width:44px;height:28px}.qitem .s:last-of-type{display:none}
  }
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
  /* Huddle embeds the exact same room and websocket instead of forking the
     player. Only the surrounding standalone chrome is hidden. */
  body.huddle-embed{padding:0;overflow:auto}
  .huddle-embed .top,.huddle-embed .chatbox{display:none}
  .huddle-embed .layout{display:block;max-width:none;margin:0;padding:0}
  .huddle-embed .stage{border-radius:0}
  .huddle-embed .card{margin:8px;border-radius:10px}
  .huddle-embed .toast{bottom:10px}
</style>
</head>
<body>
<div class="top">
  <h1><span class="dot" id="dot"></span>🎬 <span id="roomName">Watch Together</span></h1>
  <div class="who" id="who"></div>
  <button class="chip" style="cursor:pointer;border:1px solid var(--accent)" onclick="copyLink()">🔗 invite</button>
  <button id="settingsBtn" class="chip" style="cursor:pointer" onclick="openSettings()">⚙️</button>
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
      <div class="overlay hidden" id="failOvl" style="overflow-y:auto">
        <div id="failBody" style="max-width:560px;width:92%"></div>
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
        <button title="Jump to live (fixes lag)" onclick="resyncStream()">⟳</button>
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
        <button id="skipBtn" onclick="send({t:'skip'})">⏭</button>
        <button title="Shared browser" onclick="startBrowser()">🌐</button>
      </div>
    </div>
    <div class="card">
      <div class="qhead"><h2>📜 Up next</h2><span class="s" id="qcount"></span></div>
      <div class="playlistbar">
        <select id="roomPlaylist"><option value="">Shared playlists…</option></select>
        <button onclick="loadPlaylist()">Load</button>
        <button id="deletePlaylistBtn" onclick="deletePlaylist()">Delete</button>
        <input id="playlistName" maxlength="50" placeholder="New playlist name">
        <button class="primary" onclick="savePlaylist()">Save queue</button>
        <button onclick="managePlaylist('rename')">Rename</button><button onclick="managePlaylist('duplicate')">Duplicate</button>
        <button onclick="managePlaylist('undo')">↩ Undo</button><button onclick="managePlaylist('export')">⇩ Export</button>
        <select id="playlistEditor" aria-label="Playlist editor"><option value="">Choose editor…</option></select>
        <button onclick="managePlaylistEditor('editor_add')">Grant edit</button>
        <button onclick="managePlaylistEditor('editor_remove')">Revoke</button>
        <label class="chip" style="cursor:pointer">⇧ Import<input type="file" accept="application/json" hidden onchange="importRoomPlaylist(this.files[0]);this.value=''"></label>
      </div>
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
  <label class="setrow"><span>✏️ Room name<small>host can rename this WatchTogether room</small></span>
    <input id="setRoomName" maxlength="50" style="max-width:160px"></label>
  <label class="setrow"><span>🛡 Adblock<small>AdGuard in the shared browser</small></span>
    <input type="checkbox" id="setAdblock"></label>
  <label class="setrow"><span>⏭ SponsorBlock<small>cut sponsor segments from downloads</small></span>
    <input type="checkbox" id="setSponsor"></label>
  <label class="setrow"><span>🎞 Max quality<small>for downloads &amp; streams</small></span>
    <select id="setQuality">
      <option value="360">360p</option><option value="480">480p</option>
      <option value="720">720p</option><option value="1080">1080p</option>
    </select></label>
  <label class="setrow"><span>🎮 Playback controls<small>who may play, pause, seek, and reorder</small></span>
    <select id="setControlPolicy"><option value="host">Host only</option><option value="moderators">Host &amp; moderators</option><option value="everyone">Everyone</option></select></label>
  <label class="setrow"><span>⏭ Skip policy<small>moderators, voting, or everyone</small></span>
    <select id="setSkipPolicy"><option value="moderators">Moderators only</option><option value="vote">Vote to skip</option><option value="everyone">Everyone</option></select></label>
  <label class="setrow"><span>🗳 Vote threshold<small>percentage of connected participants</small></span>
    <select id="setVoteThreshold"><option value="0.25">25%</option><option value="0.5">50%</option><option value="0.67">67%</option><option value="1">100%</option></select></label>
  <div style="color:var(--muted);font-size:12px;margin:6px 0 12px">
    Adblock applies the next time the browser starts. Quality &amp; SponsorBlock apply to newly added videos.</div>
  <button class="primary" style="width:100%" onclick="saveSettings()">Save for this room</button>
  <button style="width:100%;margin-top:8px" onclick="document.getElementById('settingsModal').classList.add('hidden')">Cancel</button>
</div></div>
<div class="modal hidden" id="roleModal"><div class="box" role="dialog" aria-modal="true" aria-labelledby="roleTitle">
  <b id="roleTitle">Manage participant</b><div id="roleTarget" style="margin:10px;color:var(--muted)"></div>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
    <button onclick="participantAction('viewer')">👤 Viewer</button><button onclick="participantAction('moderator')">🛡️ Moderator</button>
    <button onclick="participantAction('host')">👑 Transfer host</button><button onclick="participantAction('mute')">🔇 Mute 5 min</button>
    <button onclick="participantAction('unmute')">🔊 Unmute</button><button class="danger" onclick="participantAction('kick')">✕ Remove</button>
  </div><button style="width:100%;margin-top:10px" onclick="closeRoleModal()">Cancel</button>
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
document.body.classList.toggle('huddle-embed', P.get('embed')==='1');
const ROOM = P.get('room') || '';
const LINK_TOKEN = new URLSearchParams(location.hash.slice(1)).get('token') || '';
let CLIENT_ID='';
let ws = null, st = null, myName = localStorage.getItem('wt_name') || '';
let roomDragFrom=-1;
let sup = {play:0, pause:0, seek:0};  // suppress echoing remote-triggered events
let needGesture = true, syncAt = 0, endedSent = -1;
const v = document.getElementById('v');
async function exchangeRoomToken(){
  if(!ROOM || !LINK_TOKEN)return true;
  const r=await fetch('/watch/api/session',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({room:ROOM,token:LINK_TOKEN})});
  const nextParams=new URLSearchParams({room:ROOM});
  if(P.get('embed')==='1')nextParams.set('embed','1');
  history.replaceState(null,'',location.pathname+'?'+nextParams.toString());
  if(!r.ok){toast('❌ This room link is invalid or expired');return false}
  return true;
}

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
  ws = new WebSocket(proto+location.host+'/watch/ws?room='+encodeURIComponent(ROOM));
  ws.onopen = ()=>{document.getElementById('dot').classList.add('on');send({t:'join',name:myName})};
  ws.onclose = ()=>{document.getElementById('dot').classList.remove('on');
    setTimeout(connect, 2500)};
  ws.onmessage = e=>{
    let d; try{d=JSON.parse(e.data)}catch(_){return}
    if(d.t==='welcome') CLIENT_ID=d.id;
    else if(d.t==='state') applyState(d);
    else if(d.t==='sync') applySync(d);
    else if(d.t==='chat') addMsg(d);
    else if(d.t==='progress') updateProgress(d);
    else if(d.t==='bcursor') showCursor(d);
    else if(d.t==='bmedia') showMedia(d);
    else if(d.t==='playlist_export') downloadRoomPlaylist(d);
  };
}

function curItem(){
  if(!st) return null;
  const i = st.index - st.offset;
  return (i>=0 && i<st.queue.length) ? st.queue[i] : null;
}
function me(){return st&&st.participants.find(p=>p.id===CLIENT_ID)}
function canModerate(){const m=me();return m&&(m.role==='host'||m.role==='moderator')}
function canControl(){const m=me(),p=st&&st.settings&&st.settings.control_policy;if(!m)return false;return p==='everyone'||(p==='host'?m.role==='host':canModerate())}
function requireControl(){
  if(canControl())return true;
  toast('🔒 The room policy does not allow that control');
  if(st)applySync(st);
  return false;
}
function roleIcon(role){return role==='host'?'👑':role==='moderator'?'🛡️':'👤'}
let roleTargetId=null;
function manageRole(id,role){
  const m=me(); if(!m||m.role!=='host'||id===CLIENT_ID)return;
  roleTargetId=id;
  const target=st.participants.find(p=>p.id===id);
  document.getElementById('roleTarget').textContent=(target?target.name:'Participant')+' · '+role;
  document.getElementById('roleModal').classList.remove('hidden');
}
function closeRoleModal(){roleTargetId=null;document.getElementById('roleModal').classList.add('hidden')}
function participantAction(action){if(roleTargetId)send({t:'participant_action',id:roleTargetId,action});closeRoleModal()}
function renderPlaylists(){
  const sel=document.getElementById('roomPlaylist'), current=sel.value;
  sel.innerHTML='<option value="">Shared playlists…</option>'+((st&&st.playlists)||[]).map(p=>
    `<option value="${encodeURIComponent(p.name)}">${esc(p.name)} (${p.count}) · ${esc(p.owner||'shared')}</option>`).join('');
  if([...sel.options].some(o=>o.value===current))sel.value=current;
  const editors=document.getElementById('playlistEditor'), editorCurrent=editors.value;
  editors.innerHTML='<option value="">Choose editor…</option>'+((st&&st.participants)||[]).filter(p=>p.id!==CLIENT_ID).map(p=>
    `<option value="${esc(p.id)}">${esc(p.name)} · ${esc(p.role)}</option>`).join('');
  if([...editors.options].some(o=>o.value===editorCurrent))editors.value=editorCurrent;
  document.getElementById('deletePlaylistBtn').disabled=!sel.value;
  document.getElementById('settingsBtn').disabled=!canModerate();
}
function savePlaylist(){const n=document.getElementById('playlistName').value.trim();if(n){send({t:'playlist_save',name:n});document.getElementById('playlistName').value=''}}
function loadPlaylist(){const v=document.getElementById('roomPlaylist').value;if(v)send({t:'playlist_load',name:decodeURIComponent(v)})}
function deletePlaylist(){const v=document.getElementById('roomPlaylist').value,n=v?decodeURIComponent(v):'';if(n&&confirm('Delete “'+n+'”?'))send({t:'playlist_delete',name:n})}
function selectedPlaylist(){const v=document.getElementById('roomPlaylist').value;return v?decodeURIComponent(v):''}
function managePlaylist(action){const name=selectedPlaylist();if(!name)return toast('Choose a playlist first');const newName=document.getElementById('playlistName').value.trim();if(['rename','duplicate'].includes(action)&&!newName)return toast('Type the new name first');send({t:'playlist_manage',action,name,new_name:newName})}
function managePlaylistEditor(action){const name=selectedPlaylist(),id=document.getElementById('playlistEditor').value;if(!name||!id)return toast('Choose a playlist and participant');send({t:'playlist_manage',action,name,id})}
function downloadRoomPlaylist(data){const blob=new Blob([JSON.stringify({name:data.name,items:data.items},null,2)],{type:'application/json'});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=data.name.replace(/[^a-z0-9_-]+/gi,'_')+'.json';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000)}
async function importRoomPlaylist(file){if(!file)return;try{const data=JSON.parse(await file.text());const name=document.getElementById('playlistName').value.trim()||data.name||'Imported playlist';send({t:'playlist_manage',action:'import',name,items:data.items});}catch(_){toast('❌ Invalid playlist JSON')}}

function applyState(d){
  const prev = st ? curItem() : null;
  const prevUid = prev ? prev.uid : null;
  st = d;
  document.getElementById('roomName').textContent = d.name;
  document.title = d.name + ' — Watch Together';
  document.getElementById('who').innerHTML = d.participants.map(p=>
    `<button class="chip ${p.role} ${p.id===CLIENT_ID?'me':''}" title="${esc(p.role)}" onclick='manageRole(${JSON.stringify(p.id)},${JSON.stringify(p.role)})'>${roleIcon(p.role)} ${esc(p.name)}</button>`).join('');
  if(d.chat && !document.getElementById('msgs').childElementCount)
    d.chat.forEach(addMsg);
  renderQueue();
  renderPlaylists();
  const skipBtn=document.getElementById('skipBtn');
  if(skipBtn)skipBtn.textContent=(d.settings.skip_policy==='vote'&&d.skip_votes)?`🗳️ ${d.skip_votes}/${d.skip_needed}`:'⏭';
  updateBrowserView();
  const cur = curItem();
  const failOvl = document.getElementById('failOvl');
  if(browserActive()){
    failOvl.classList.add('hidden');
    document.getElementById('ovl').classList.add('hidden');
    if(!v.paused) v.pause();
  } else if(cur && ['embed','error','sniffing','choice'].includes(cur.status)){
    v.removeAttribute('src'); v.load();
    renderFailOverlay(cur);
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

// ---- fail / sniff / pick overlay ----
let failKey = null;
function renderFailOverlay(cur){
  const key = cur.uid + '|' + cur.status + '|' + (cur.choices ? cur.choices.length : 0);
  if(key === failKey) return;   // don't rebuild (keeps buttons clickable)
  failKey = key;
  const el = document.getElementById('failBody');
  if(cur.status === 'sniffing'){
    el.innerHTML = `<div class="big" style="font-size:42px">🔍</div>
      <div style="margin:8px 0">yt-dlp couldn't grab <b>${esc(cur.title||'this')}</b> directly.<br>
      Scanning the page's network traffic for media…</div>
      <div class="spin" style="width:34px;height:34px;border:4px solid #333;border-top-color:var(--accent);border-radius:50%;margin:12px auto;animation:spin 1s linear infinite"></div>
      <style>@keyframes spin{to{transform:rotate(360deg)}}</style>`;
  } else if(cur.status === 'choice' && cur.choices && cur.choices.length){
    el.innerHTML = `<div class="big" style="font-size:38px">📹</div>
      <div style="margin:6px 0 10px">Found on the page — <b>pick the right one</b> (some could be ads):</div>
      <div style="background:var(--panel);border-radius:10px;padding:6px 12px;text-align:left;max-height:40vh;overflow-y:auto">` +
      cur.choices.map(c =>
        `<div class="mediaitem"><span class="mi-kind">${esc(c.kind)}</span>`+
        `<span class="mi-name" title="${esc(c.url)}">${esc(c.name)} <span style="color:var(--muted)">${esc(c.mime)}</span></span>`+
        `<button class="primary" onclick='send({t:"pick_choice",uid:${JSON.stringify(cur.uid)},url:${JSON.stringify(c.url)}})'>▶ This one</button></div>`
      ).join('') + `</div>
      <div style="display:flex;gap:10px;justify-content:center;margin-top:12px;flex-wrap:wrap">
        <button onclick="openInBrowser()">🌐 Open in shared browser instead</button>
        <button onclick="send({t:'skip'})">⏭ Skip</button>
        <button class="danger" onclick="send({t:'remove_current'})">✖ Remove</button>
      </div>`;
  } else {
    el.innerHTML = `<div class="big" style="font-size:42px">🚫</div>
      <div style="margin:8px 0">${esc(cur.title||'This one')} couldn't be downloaded.</div>
      <div style="display:flex;gap:10px;flex-wrap:wrap;justify-content:center;margin-top:10px">
        <button class="primary" onclick="openInBrowser()">🌐 Open in shared browser</button>
        <button onclick="send({t:'skip'})">⏭ Skip it</button>
      </div>`;
  }
}

// ---- video source (plain file or progressive HLS while downloading) ----
let curSrcUid=null, hlsP=null;
function destroyHls(){ if(hlsP){ try{hlsP.destroy()}catch(_){} hlsP=null; } }
function setVideoSrc(cur){
  destroyHls();
  curSrcUid = cur.uid;
  const url = 'media/'+encodeURIComponent(ROOM)+'/'+cur.file.split('/').map(encodeURIComponent).join('/');
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
  if(!requireControl())return;
  if(!st || !st.settings) return;
  document.getElementById('setAdblock').checked = !!st.settings.adblock;
  document.getElementById('setSponsor').checked = !!st.settings.sponsorblock;
  document.getElementById('setQuality').value = String(st.settings.quality||720);
  document.getElementById('setControlPolicy').value=st.settings.control_policy||'moderators';
  document.getElementById('setSkipPolicy').value=st.settings.skip_policy||'vote';
  document.getElementById('setVoteThreshold').value=String(st.settings.vote_threshold||0.5);
  document.getElementById('setRoomName').value=st.name||'';
  const host=me()&&me().role==='host';
  ['setRoomName','setControlPolicy','setSkipPolicy','setVoteThreshold'].forEach(id=>document.getElementById(id).disabled=!host);
  document.getElementById('settingsModal').classList.remove('hidden');
}
function saveSettings(){
  send({t:'settings',
    room_name: document.getElementById('setRoomName').value.trim(),
    adblock: document.getElementById('setAdblock').checked,
    sponsorblock: document.getElementById('setSponsor').checked,
    quality: +document.getElementById('setQuality').value,
    control_policy:document.getElementById('setControlPolicy').value,
    skip_policy:document.getElementById('setSkipPolicy').value,
    vote_threshold:+document.getElementById('setVoteThreshold').value});
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
  const url = proto+location.host+'/watch/stream?room='+encodeURIComponent(ROOM);
  bPlayer = new JSMpeg.Player(url, {
    canvas: document.getElementById('bcanvas'),
    audio: true, pauseWhenHidden: false,
    videoBufferSize: 768*1024, audioBufferSize: 256*1024,
  });
}
function stopStream(){
  if(bPlayer){ try{bPlayer.destroy()}catch(_){} bPlayer=null; }
}
function resyncStream(){ stopStream(); startStream(); toast('⟳ jumped to live'); }
function startBrowser(){
  if(!requireControl())return;
  send({t:'browser_start'});
  toast('🌐 Starting the shared browser… (takes ~10s)');
}
function openInBrowser(){
  if(!requireControl())return;
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
  if(cur && ['embed','error','sniffing','choice'].includes(cur.status)) return;
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

v.addEventListener('play', ()=>{ if(sup.play>0){sup.play--;return} if(!requireControl())return; send({t:'play',pos:v.currentTime}) });
v.addEventListener('pause', ()=>{ if(v.ended)return; if(sup.pause>0){sup.pause--;return}
  if(v.seeking||!requireControl())return; send({t:'pause',pos:v.currentTime}) });
v.addEventListener('seeked', ()=>{ if(sup.seek>0){sup.seek--;return} if(!requireControl())return; send({t:'seek',pos:v.currentTime}) });
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
    else if(it.status==='sniffing') status='🔍';
    else if(it.status==='choice') status='📹 pick';
    else if(it.status==='error') status='❌';
    else if(it.status==='embed') status='🌐';
    else if(it.live_dl) status='▶️⬇ '+(it.progress||0)+'%';
    else status = fmt(it.duration);
    return `<div class="qitem ${now?'now':''}" draggable="${!now&&canControl()}" data-uid="${it.uid}" ondragstart="roomQueueDragStart(event,${gi})" ondragover="roomQueueDragOver(event)" ondragleave="this.classList.remove('drag-over')" ondrop="roomQueueDrop(event,${gi})" ondragend="roomQueueDragEnd()">`+
      (it.thumbnail?`<img src="${esc(it.thumbnail)}" loading="lazy">`:'<img>')+
      `<span class="t" onclick="if(requireControl())send({t:'jump',index:${gi}})" title="${esc(it.title)}">${now?'▶ ':''}${esc(it.title)}</span>`+
      `<span class="s" data-status>${status}</span>`+
      `<span class="s">${esc(it.added_by||'')}</span>`+
      (!now&&canControl()?`<button class="queue-move" aria-label="Move up" onclick="send({t:'reorder',from:${gi},to:${Math.max(0,gi-1)}})">↑</button><button class="queue-move" aria-label="Move down" onclick="send({t:'reorder',from:${gi},to:${Math.min(st.total-1,gi+1)}})">↓</button>`:'')+
      (now||!canModerate()?'':`<span class="x" onclick="send({t:'remove',index:${gi}})">✖</span>`)+
      `</div>`;
  }).join('');
}
function roomQueueDragStart(event,index){roomDragFrom=index;event.currentTarget.classList.add('dragging');event.dataTransfer.effectAllowed='move'}
function roomQueueDragOver(event){if(roomDragFrom<0)return;event.preventDefault();event.currentTarget.classList.add('drag-over')}
function roomQueueDragEnd(){document.querySelectorAll('.qitem').forEach(el=>el.classList.remove('dragging','drag-over'));roomDragFrom=-1}
function roomQueueDrop(event,index){event.preventDefault();if(roomDragFrom>=0&&roomDragFrom!==index)send({t:'reorder',from:roomDragFrom,to:index});roomQueueDragEnd()}
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
  if(cur && ['embed','error','sniffing','choice'].includes(cur.status)) return;
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

async function startPage(){
  if(!ROOM){document.getElementById('landing').classList.remove('hidden');return}
  if(!await exchangeRoomToken())return;
  if(myName){connect();return}
  document.getElementById('nameModal').classList.remove('hidden');
  document.getElementById('nameInput').focus();
}
startPage();
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
  <button class="rbtn" onclick="toggleAddPanel()">➕</button>
  <button class="rbtn" onclick="copyLink()">🔗</button>
  <button class="rbtn" onclick="toggleFullscreen()">⛶</button>
  <button class="rbtn" title="Previous reel" aria-label="Previous reel" onclick="doSwipeBack()">⬇️</button>
  <button class="rbtn" title="Next reel" aria-label="Next reel" onclick="doSwipe()">⬆️</button>
</div>
<div class="meta"><div class="t" id="mtitle"></div><div class="u" id="muser"></div></div>
<div class="chatfeed" id="chatfeed"></div>
<div id="addPanel" class="hidden" style="position:fixed;left:10px;right:70px;bottom:66px;
     z-index:11;display:flex;gap:8px">
  <input id="addq" maxlength="300"
         placeholder="Paste a reel / short / tiktok link, or search — plays next & teaches the feed"
         style="flex:1;background:rgba(255,255,255,.12);backdrop-filter:blur(8px);
                border:1px solid rgba(255,255,255,.25);color:#fff;border-radius:999px;
                padding:11px 16px;font-size:14px;outline:none"
         onkeydown="if(event.key==='Enter')submitAdd()">
  <button class="rbtn" style="width:auto;border-radius:999px;padding:0 16px;font-size:14px"
          onclick="submitAdd()">Add</button>
</div>
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
const ROOM = P.get('room')||'';
const LINK_TOKEN = new URLSearchParams(location.hash.slice(1)).get('token')||'';
let CLIENT_ID='';
let ws=null, st=null, myName=localStorage.getItem('wt_name')||'';
let sup={play:0,pause:0,seek:0}, syncAt=0, swipeLock=0, watchedStart=0;
const v=document.getElementById('v');

async function exchangeRoomToken(){
  if(!ROOM||!LINK_TOKEN)return true;
  const r=await fetch('/watch/api/session',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({room:ROOM,token:LINK_TOKEN})});
  history.replaceState(null,'',location.pathname+'?room='+encodeURIComponent(ROOM));
  if(!r.ok){toast('❌ This room link is invalid or expired');return false}
  return true;
}

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
  ws=new WebSocket(proto+location.host+'/watch/ws?room='+encodeURIComponent(ROOM));
  ws.onopen=()=>send({t:'join',name:myName});
  ws.onclose=()=>setTimeout(connect,2500);
  ws.onmessage=e=>{
    let d; try{d=JSON.parse(e.data)}catch(_){return}
    if(d.t==='welcome')CLIENT_ID=d.id;
    else if(d.t==='state')applyState(d);
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
  const mine=d.participants.find(p=>p.id===CLIENT_ID);
  const badge=mine?(mine.role==='host'?' 👑':mine.role==='moderator'?' 🛡️':''):'';
  document.getElementById('whoCount').textContent='👥 '+d.participants.length+badge;
  document.getElementById('seedOvl').classList.toggle('hidden', !d.need_seed);
  const cur=curItem();
  if(cur&&(cur.file||cur.status==='embed')){
    document.getElementById('loadOvl').classList.add('hidden');
    if(cur.status==='embed'){
      if(cur.uid!==embedUid) showReelEmbed(cur);
    } else {
      if(embedUid) clearReelEmbed();
      if(cur.uid!==prevUid || !v.src.includes(encodeURIComponent(cur.file))){
        v.src='media/'+encodeURIComponent(ROOM)+'/'+encodeURIComponent(cur.file);
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
function doSwipeBack(){
  if(Date.now()-swipeLock<600)return;
  if(!st||st.index<=0){toast('⏮ This is the oldest available reel');return}
  swipeLock=Date.now();
  document.getElementById('hint').style.display='none';
  send({t:'swipe_back'});
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
function toggleAddPanel(){
  const p=document.getElementById('addPanel');
  p.classList.toggle('hidden');
  if(!p.classList.contains('hidden')) document.getElementById('addq').focus();
}
function submitAdd(){
  const i=document.getElementById('addq'), q=i.value.trim();
  if(!q) return;
  send({t:'add', q});
  i.value=''; i.blur();
  document.getElementById('addPanel').classList.add('hidden');
  toast('➕ Queued — plays next, and the feed learns from it');
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

// gestures: swipe up = next, swipe down = previous, double tap = like,
// single tap = pause/play
let tY=null, lastTap=0;
document.getElementById('stage').addEventListener('touchstart',e=>{tY=e.touches[0].clientY},{passive:true});
document.getElementById('stage').addEventListener('touchend',e=>{
  if(tY===null)return;
  const dy=tY-e.changedTouches[0].clientY; tY=null;
  if(dy>70){doSwipe();return}
  if(dy < -70){doSwipeBack();return}
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
window.addEventListener('wheel',e=>{
  if(e.deltaY>30)doSwipe();
  else if(e.deltaY < -30)doSwipeBack();
},{passive:true});
window.addEventListener('keydown',e=>{
  if(e.target.tagName==='INPUT')return;
  if(e.key==='ArrowUp'||e.key==='ArrowDown'||e.key===' '){e.preventDefault();
    if(e.key===' ')togglePlay();
    else if(e.key==='ArrowUp')doSwipeBack();
    else doSwipe();}
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

async function startPage(){
  if(!ROOM){location.replace('./');return}
  if(!await exchangeRoomToken())return;
  if(myName){document.getElementById('nameOvl').classList.add('hidden');connect();}
  else{document.getElementById('nameInput').focus();}
}
startPage();
</script>
</body>
</html>
"""
