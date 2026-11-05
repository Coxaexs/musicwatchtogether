"""Watch Together and Reels Together: synced video activities for Hoffle voice rooms.

Hoffle is the only way in. It opens these as activities of a voice room: it
checks who someone is and that they may be in that room, then asks the bot for
a one-time ticket (POST /api/admin/watch-ticket on the dashboard, master
session only, see webui.py). The page trades the ticket for a session id it
keeps for the tab and sends that id as its first websocket message, so nothing
depends on cookies (the page may be framed from another site).

Routes, mounted onto the dashboard's aiohttp app:

    /watch/                       Watch Together page
    /watch/reels                  Reels Together page
    /watch/ws?room=...            websocket: {t:'hello', session} first, then the room protocol
    /watch/api/session            ticket -> session
    /watch/media/{room}/{key}/{file}  the room's downloaded videos ({key} is the room's media key)
    /watch/stream?room=&key=      the shared browser's MPEG-TS stream
    /watch/static/{file}          page assets

There is one room per Hoffle voice room and mode: 'wh_<channel id>' for Watch
Together and 'rh_<channel id>' for Reels Together. Who may do what follows
Hoffle: whoever started the activity is its host, people who can manage the
Hoffle server are moderators, and the host or a moderator can make anyone a
moderator of the room.

Videos are anything yt-dlp can resolve. They are downloaded to a local cache
and served from disk so every participant plays the exact same file; long
videos stream while they download (HLS). The Reels feed lives in reels_brain.
"""

import asyncio
import glob
import hashlib
import hmac
import json
import logging
import math
import os
import random
import re
import secrets
import shutil
import time

import yt_dlp
from aiohttp import web, WSMsgType

import config
import reels_brain as brain
from security import PublicURLRequired, SlidingWindowLimiter, validate_public_url
from runtime import TaskRegistry, increment, prometheus
from storage import SQLiteDocumentStore

try:
    if not getattr(config, 'COBROWSER_ENABLED', True):
        cobrowser = None
    else:
        import cobrowser
except Exception as _e:   # missing python-xlib etc. - watch still works
    cobrowser = None
    logging.getLogger('MusicBot.WatchTogether').warning(f"co-browser unavailable: {_e}")

logger = logging.getLogger('MusicBot.WatchTogether')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.getenv("DATA_DIR") or BASE_DIR
STATIC_DIR = os.path.join(BASE_DIR, 'webstatic')
WATCH_CACHE_DIR = os.path.join(DATA_DIR, 'watch_cache')
REELS_CACHE_DIR = os.path.join(DATA_DIR, 'reels_cache')

TICKET_TTL = 120                # a ticket is used within seconds of being made
SESSION_TTL = 7 * 86400
MAX_SESSIONS = 3000
WATCH_CACHE_LIMIT = 10          # downloaded WatchTogether videos/HLS sessions
REELS_CACHE_LIMIT = 40          # downloaded reels in total
REELS_HISTORY = 12              # recent reels kept for swiping back
REELS_READY_AHEAD = 3           # reels downloaded ahead of the one playing
REELS_MAX_DURATION = 180
MAX_ACTIVE_ROOMS = 50
MAX_ROOM_PARTICIPANTS = 100
MAX_QUEUE_ITEMS = 200
MAX_ROOM_PLAYLISTS = 30
MAX_PLAYLIST_ITEMS = 50
ROOM_IDLE_TTL = 2 * 3600
GLOBAL_DOWNLOADS = 4
HLS_MIN_DURATION = 8 * 60       # longer than this -> stream while downloading
SOURCE_CACHE_TTL = 15 * 60
REACTIONS = ('😂', '❤️', '🔥', '😮', '👏', '💀', '😭', '👀')

state_store = SQLiteDocumentStore(os.path.join(DATA_DIR, 'musicbot.sqlite3'), logger)
DEFAULT_SETTINGS = {
    'adblock': True, 'sponsorblock': True, 'quality': 720,
    # Friends in a voice room: anyone may drive until a host locks it down.
    'control_policy': 'everyone', 'skip_policy': 'everyone', 'vote_threshold': 0.5,
    'swipe_policy': 'everyone', 'autoscroll': True,
}
SB_CATEGORIES = ['sponsor', 'selfpromo', 'interaction']

global_download_sem = None
message_limiter = SlidingWindowLimiter(150, 60)
search_limiter = SlidingWindowLimiter(12, 60)
add_limiter = SlidingWindowLimiter(25, 60)
learn_limiter = SlidingWindowLimiter(60, 60)
task_registry = TaskRegistry('watch')

URL_RE = re.compile(r'^https?://', re.IGNORECASE)
YT_ID_RE = re.compile(
    r'(?:youtube\.com/(?:watch\?(?:[^#\s]*&)?v=|shorts/|embed/|live/|v/)|youtu\.be/)'
    r'([A-Za-z0-9_-]{11})')
ROOM_RE = re.compile(r'[wr]h_[A-Za-z0-9_-]{1,80}')

_search_opts = {'quiet': True, 'no_warnings': True, 'extract_flat': True,
                'nocheckcertificate': True, 'skip_download': True,
                'socket_timeout': 15}
_info_opts = {'quiet': True, 'no_warnings': True, 'noplaylist': True,
              'nocheckcertificate': True, 'skip_download': True,
              'socket_timeout': 15}
if config.YTDLP_COOKIEFILE:
    _search_opts['cookiefile'] = config.YTDLP_COOKIEFILE
    _info_opts['cookiefile'] = config.YTDLP_COOKIEFILE


def _spawn(coroutine, name):
    return task_registry.create(coroutine, name)


def _global_download_limiter():
    global global_download_sem
    if global_download_sem is None:
        global_download_sem = asyncio.Semaphore(GLOBAL_DOWNLOADS)
    return global_download_sem


def _dl_opts(cache_dir, vertical=False, quality=720, sponsorblock=False):
    height = 1280 if vertical else quality
    opts = {
        # H.264 first: "ext=mp4" alone also matches YouTube's AV1 and VP9, which
        # plenty of browsers and Windows machines show as a blank picture.
        'format': (f'bestvideo[height<={height}][vcodec^=avc1]+bestaudio[ext=m4a]'
                   f'/best[height<={height}][vcodec^=avc1]'
                   f'/bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]'
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
    if config.YTDLP_COOKIEFILE:
        opts['cookiefile'] = config.YTDLP_COOKIEFILE
    return opts


# ---------------------------------------------------------------- persistence

class _Doc:
    """A persisted dict, written at most every few seconds off the event loop."""

    def __init__(self, key):
        self.key = key
        self.data = state_store.load(key, {})
        if not isinstance(self.data, dict):
            self.data = {}
        self._dirty = False
        self._task = None

    def touch(self):
        self._dirty = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.flush()
            return
        if self._task is None or self._task.done():
            self._task = loop.create_task(self._later())

    async def _later(self):
        await asyncio.sleep(3)
        if self._dirty:
            self._dirty = False
            snapshot = json.loads(json.dumps(self.data))
            await asyncio.get_running_loop().run_in_executor(
                None, state_store.save, self.key, snapshot)

    def flush(self):
        if self._task and not self._task.done():
            self._task.cancel()
        self._dirty = False
        state_store.save(self.key, self.data)


sessions = _Doc('together_sessions')      # session id -> who, which room
settings_doc = _Doc('together_settings')  # room id -> settings
playlists_doc = _Doc('together_playlists')  # room id -> name -> playlist
mods_doc = _Doc('together_mods')          # room id -> [hoffle user ids]
people_doc = _Doc('reels_people')         # hoffle user id -> taste (reels_brain)
feeds_doc = _Doc('reels_feeds')           # room id -> feed state (reels_brain)
secret_doc = _Doc('together_secret')
if not secret_doc.data.get('key'):
    secret_doc.data['key'] = secrets.token_hex(32)
    secret_doc.flush()
_MEDIA_SECRET = secret_doc.data['key'].encode()

tickets = {}   # ticket -> {room, name, user, roles, expires}


def media_key(room_id):
    return hmac.new(_MEDIA_SECRET, f'media:{room_id}'.encode(), hashlib.sha256).hexdigest()[:24]


def get_settings(room_id):
    cfg = dict(DEFAULT_SETTINGS)
    cfg.update(settings_doc.data.get(room_id) or {})
    return cfg


def update_settings(room_id, **changes):
    """Validated room settings; returns the full settings."""
    cfg = get_settings(room_id)
    if changes.get('adblock') is not None:
        cfg['adblock'] = bool(changes['adblock'])
    if changes.get('sponsorblock') is not None:
        cfg['sponsorblock'] = bool(changes['sponsorblock'])
    if changes.get('autoscroll') is not None:
        cfg['autoscroll'] = bool(changes['autoscroll'])
    if changes.get('quality') is not None:
        try:
            quality = int(changes['quality'])
        except (TypeError, ValueError):
            quality = 0
        if quality not in (360, 480, 720, 1080):
            raise ValueError('quality must be 360, 480, 720, or 1080')
        cfg['quality'] = quality
    for key, allowed in (('control_policy', ('everyone', 'managers')),
                         ('skip_policy', ('everyone', 'vote', 'managers')),
                         ('swipe_policy', ('everyone', 'managers'))):
        value = changes.get(key)
        if value is not None:
            if value not in allowed:
                raise ValueError(f'invalid {key}')
            cfg[key] = value
    if changes.get('vote_threshold') is not None:
        cfg['vote_threshold'] = max(0.25, min(1.0, float(changes['vote_threshold'])))
    settings_doc.data[room_id] = cfg
    settings_doc.touch()
    return cfg


def _room_id(mode, channel_id):
    channel_id = str(channel_id or '')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', channel_id):
        raise ValueError('invalid channel id')
    return ('r' if mode == 'reels' else 'w') + 'h_' + channel_id


def _validated_room_id(room_id):
    room_id = str(room_id or '')
    if not ROOM_RE.fullmatch(room_id):
        raise ValueError('invalid room id')
    return room_id


def _clean_text(value, limit):
    return re.sub(r'\s+', ' ', str(value or '')).strip()[:limit]


def _clean_user(raw):
    raw = raw if isinstance(raw, dict) else {}
    uid = re.sub(r'[^A-Za-z0-9_:.-]', '', str(raw.get('id') or ''))[:80]
    if not uid:
        raise ValueError('user id required')
    avatar_url = str(raw.get('avatar_url') or '')[:500]
    if avatar_url and not (avatar_url.startswith('/') or URL_RE.match(avatar_url)):
        avatar_url = ''
    color = str(raw.get('color') or '')
    return {
        'id': uid,
        'name': _clean_text(raw.get('name'), 40) or 'Someone',
        'avatar': _clean_text(raw.get('avatar'), 8),
        'avatar_url': avatar_url,
        'color': color if re.fullmatch(r'#[0-9a-fA-F]{6}', color) else '',
    }


# ---------------------------------------------------------------- Hoffle handoff

def issue_ticket(body):
    """Hoffle's request to let one of its members into a room (admin API).

    body: {mode, channel_id, room_name, user: {id, name, avatar, avatar_url,
    color}, host: bool, moderator: bool}. Returns {ticket, room, path}.
    """
    mode = 'reels' if body.get('mode') == 'reels' else 'watch'
    room_id = _room_id(mode, body.get('channel_id'))
    user = _clean_user(body.get('user'))
    now = time.time()
    for key in [k for k, v in tickets.items() if v['expires'] < now]:
        tickets.pop(key, None)
    if len(tickets) > 500:
        tickets.clear()
    ticket = secrets.token_urlsafe(24)
    tickets[ticket] = {
        'room': room_id,
        'name': _clean_text(body.get('room_name'), 60) or ('Reels' if mode == 'reels' else 'Watch'),
        'user': user,
        'host': bool(body.get('host')),
        'moderator': bool(body.get('moderator')),
        'expires': now + TICKET_TTL,
    }
    increment('tickets_issued_total')
    return {'ticket': ticket, 'room': room_id,
            'path': '/watch/reels' if mode == 'reels' else '/watch/'}


def _redeem_ticket(room_id, ticket):
    info = tickets.pop(str(ticket or ''), None)
    if not info or info['expires'] < time.time() or info['room'] != room_id:
        return None
    sid = secrets.token_urlsafe(24)
    data = sessions.data
    data[sid] = {
        'room': room_id, 'room_name': info['name'], 'user': info['user'],
        'host': info['host'], 'moderator': info['moderator'], 'created': time.time(),
    }
    if len(data) > MAX_SESSIONS:
        for key in sorted(data, key=lambda k: data[k].get('created', 0))[:len(data) - MAX_SESSIONS]:
            data.pop(key, None)
    sessions.touch()
    return sid


def _session(room_id, sid):
    info = sessions.data.get(str(sid or ''))
    if not info or info.get('room') != room_id:
        return None
    if time.time() - info.get('created', 0) > SESSION_TTL:
        sessions.data.pop(sid, None)
        sessions.touch()
        return None
    return info


# ---------------------------------------------------------------- room model

class Room:
    def __init__(self, room_id, name):
        self.id = room_id
        self.mode = 'reels' if room_id.startswith('r') else 'watch'
        self.channel_id = room_id[3:]
        self.name = name or ('Reels Together' if self.mode == 'reels' else 'Watch Together')
        self.queue = []
        self.index = -1
        self.playing = False
        self.position = 0.0
        self.pos_ts = time.monotonic()
        self.sockets = {}            # ws -> member dict
        self._order = 0
        self.dl_sem = None
        self.reels_task = None
        self.sync_task = None
        self._advanced_past = -1     # 'ended' debounce
        self._topup_busy = False
        self.skip_votes = set()
        self.playlist_lock = None
        self.last_activity = time.monotonic()
        self.feed = feeds_doc.data.setdefault(room_id, brain.new_feed()) \
            if self.mode == 'reels' else None

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
            self.dl_sem = asyncio.Semaphore(3 if self.mode == 'reels' else 2)
        return self.dl_sem

    # ---- people ----
    def role_of(self, info):
        uid = info['user']['id']
        if info.get('host'):
            return 'host'
        if info.get('moderator') or uid in (mods_doc.data.get(self.id) or []):
            return 'moderator'
        return 'member'

    def join(self, ws, info):
        self._order += 1
        member = dict(info['user'])
        member['role'] = self.role_of(info)
        member['order'] = self._order
        member['hoffle_moderator'] = bool(info.get('moderator') or info.get('host'))
        self.sockets[ws] = member
        return member

    def present(self):
        """One entry per person (a person may have the room open twice)."""
        people = {}
        for member in sorted(self.sockets.values(), key=lambda m: m['order']):
            people.setdefault(member['id'], member)
        return list(people.values())

    def present_ids(self):
        return [member['id'] for member in self.present()]


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
            empty = [candidate for candidate in rooms.values() if not candidate.sockets]
            if not empty:
                raise RuntimeError('server room limit reached')
            victim = min(empty, key=lambda candidate: candidate.last_activity)
            rooms.pop(victim.id, None)
        room = Room(room_id, name)
        rooms[room_id] = room
        increment('rooms_created_total')
    elif name and room.name != name:
        room.name = name
    return room


def _can_manage(member):
    return bool(member and member.get('role') in ('host', 'moderator'))


def _allowed(room, member, policy_key):
    policy = get_settings(room.id).get(policy_key, 'everyone')
    return policy == 'everyone' or (policy == 'vote' and member) or _can_manage(member)


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


def _public_person(member):
    return {k: member.get(k) for k in ('id', 'name', 'avatar', 'avatar_url', 'color')}


def _item_public(item, viewer_id=None):
    data = {k: item.get(k) for k in
            ('uid', 'vid', 'title', 'duration', 'thumbnail', 'status', 'progress',
             'file', 'uploader', 'error', 'embed_kind', 'embed', 'url', 'live_dl',
             'choices', 'width', 'height', 'why', 'added_by')}
    likes = item.get('likes') or []
    data['likes'] = len(likes)
    data['liked_ids'] = list(likes)
    data['liked_by'] = [item.get('liker_names', {}).get(uid, '') for uid in likes[-5:]]
    data['creator'] = bool(item.get('channel_id'))
    return data


def _thumb(vid):
    return f'https://i.ytimg.com/vi/{vid}/hqdefault.jpg' if vid and re.fullmatch(r'[\w-]{11}', vid) else None


def _set_embed_fallback(item, err=''):
    """When yt-dlp can't download, fall back to embedding the page."""
    url = item.get('url') or ''
    if URL_RE.match(url):
        match = YT_ID_RE.search(url)
        if match:
            item['embed_kind'] = 'youtube'
            item['embed'] = match.group(1)
        else:
            item['embed_kind'] = 'iframe'
            item['embed'] = url
        item['status'] = 'embed'
        item['error'] = str(err)[:200]
    else:
        item['status'] = 'error'
        item['error'] = str(err or 'No results found')[:200]


def _playlist_summaries(room_id):
    playlists = playlists_doc.data.get(room_id) or {}
    return [{'name': name, 'count': len(value.get('items') or []),
             'owner': value.get('owner_name'), 'owner_id': value.get('owner_id'),
             'updated_at': value.get('updated_at')}
            for name, value in sorted(playlists.items(), key=lambda pair: pair[0].lower())]


def _feed_view(room):
    people = [people_doc.data.get(uid) for uid in room.present_ids()]
    taste = brain.taste(people, room.feed.get('seeds'))
    return {
        'interests': brain.top_interests(taste, 14),
        'need_seed': not room.queue and not taste,
        'finding': room._topup_busy,
    }


def _room_state(room):
    if room.mode == 'reels':
        lo = max(0, room.index - 2)
        items = room.queue[lo:room.index + REELS_READY_AHEAD + 2]
        offset = lo
    else:
        items = room.queue
        offset = 0
    cfg = get_settings(room.id)
    present = room.present()
    state = {
        't': 'state',
        'room': room.id, 'name': room.name, 'mode': room.mode,
        'index': room.index, 'offset': offset, 'total': len(room.queue),
        'playing': room.playing, 'position': round(room.get_position(), 3),
        'now': int(time.time() * 1000),
        'queue': [_item_public(i) for i in items],
        'members': [dict(_public_person(m), role=m['role']) for m in present],
        'settings': cfg,
    }
    if room.mode == 'watch':
        state['playlists'] = _playlist_summaries(room.id)
        state['skip_votes'] = len(room.skip_votes)
        state['skip_needed'] = max(1, math.ceil(len(present) * cfg.get('vote_threshold', 0.5)))
        state['browser'] = (cobrowser.get(room.id).public_state()
                            if cobrowser and cobrowser.get(room.id) else {'active': False})
        state['browser_available'] = bool(cobrowser)
    else:
        state['feed'] = _feed_view(room)
    return state


async def _send(ws, payload):
    try:
        await ws.send_str(json.dumps(payload))
    except Exception:
        pass


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


async def _notice(room, text, by=None, kind='info'):
    await _broadcast(room, {'t': 'notice', 'text': text, 'kind': kind,
                            'by': _public_person(by) if by else None})


def _sync_payload(room, **extra):
    payload = {'t': 'sync', 'index': room.index, 'playing': room.playing,
               'position': round(room.get_position(), 3), 'now': int(time.time() * 1000)}
    payload.update(extra)
    return payload


# ---------------------------------------------------------------- yt-dlp

def _blocking_extract(query):
    q = query if URL_RE.match(query) else f'ytsearch1:{query}'
    with yt_dlp.YoutubeDL(_info_opts) as ydl:
        info = ydl.extract_info(q, download=False)
    if info and info.get('entries') is not None:
        entries = [e for e in info['entries'] if e]
        info = entries[0] if entries else None
    return info


def _blocking_flat(url, limit=40):
    opts = dict(_search_opts, playlistend=limit)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return [e for e in (info.get('entries') or []) if e] if info else []


def _flat_result(entry):
    vid = entry.get('id')
    thumbs = entry.get('thumbnails') or []
    thumb = _thumb(vid) or (thumbs[-1].get('url') if thumbs else None)
    url = entry.get('url') or ''
    if not URL_RE.match(url):
        url = f'https://www.youtube.com/watch?v={vid}' if vid else ''
    return {
        'url': url, 'vid': vid, 'title': entry.get('title') or 'Untitled',
        'duration': entry.get('duration'), 'thumbnail': thumb,
        'uploader': entry.get('channel') or entry.get('uploader'),
        'views': entry.get('view_count'),
        'live': entry.get('live_status') == 'is_live',
    }


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

    tail = ['-c', 'copy', '-bsf:a', 'aac_adtstoasc', '-movflags', '+faststart', out]
    force_hls = ['-f', 'hls', '-i', url]
    plain = ['-i', url]
    transcode = ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23',
                 '-c:a', 'aac', '-movflags', '+faststart', out]
    if kind == 'manifest':
        attempts = [_base() + force_hls + tail, _base() + plain + tail,
                    _base() + force_hls + transcode]
    else:
        attempts = [_base() + plain + tail, _base() + force_hls + tail,
                    _base() + plain + transcode]

    last = 'ffmpeg failed'
    for cmd in attempts:
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=3600)
        except subprocess.TimeoutExpired:
            last = 'ffmpeg timed out'
            continue
        if proc.returncode == 0 and os.path.isfile(out) and os.path.getsize(out) > 1024:
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
            if any(path == root or path.startswith(root + os.sep) for root in cache_dirs)
        ][:keep])
        for cache_dir in cache_dirs:
            files.extend(f for f in glob.glob(os.path.join(cache_dir, '*.*'))
                         if not f.endswith(('.part', '.ytdl')))
        hls_root = os.path.join(WATCH_CACHE_DIR, 'hls')
        if WATCH_CACHE_DIR in cache_dirs and os.path.isdir(hls_root):
            files.extend(os.path.join(hls_root, name) for name in os.listdir(hls_root)
                         if os.path.isdir(os.path.join(hls_root, name)))
        files.sort(key=os.path.getmtime)
        removable = [f for f in files if f not in protected]
        excess = len(files) - keep
        for f in removable:
            if excess <= 0:
                break
            try:
                if os.path.isdir(f):
                    shutil.rmtree(f)
                else:
                    os.remove(f)
                excess -= 1
            except OSError:
                pass
    except Exception as e:
        logger.warning(f"cache trim failed: {e}")


def _protected_files(extra=()):
    """Pick the most useful cached files to retain, in priority order."""
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

    for path in extra:
        if path and path not in ordered:
            ordered.append(path)
    for room in rooms.values():
        add(room, room.current())
    for distance in range(1, REELS_READY_AHEAD + 2):
        for room in rooms.values():
            idx = room.index + distance
            if 0 <= idx < len(room.queue):
                add(room, room.queue[idx])
    for distance in range(1, REELS_HISTORY + 1):
        for room in rooms.values():
            if room.mode != 'reels':
                continue
            idx = room.index - distance
            if idx >= 0:
                add(room, room.queue[idx])
    return ordered


# ---------------------------------------------------------------- downloads

_media_info = {}   # video id -> what yt-dlp said about it, for cache hits
_MEDIA_FIELDS = ('title', 'duration', 'uploader', 'channel_id', 'thumbnail', 'width', 'height', 'tags_w')

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


def _reel_problem(item):
    """Why a downloaded clip can't be a reel, or None."""
    width, height = item.get('width'), item.get('height')
    if width and height and width >= height:
        return 'not a vertical video'
    if (item.get('duration') or 0) > REELS_MAX_DURATION:
        return 'longer than three minutes'
    return None


async def _download_item_inner(room, item):
    cache_dir = REELS_CACHE_DIR if room.mode == 'reels' else WATCH_CACHE_DIR
    loop = asyncio.get_running_loop()
    async with room.download_limiter():
        if item.get('status') in ('ready', 'error'):
            return
        vid = item.get('vid')
        if vid and vid in _media_info:
            hit = [f for f in glob.glob(os.path.join(cache_dir, f'{vid}.*'))
                   if not f.endswith(('.part', '.ytdl'))]
            if hit:
                for key, value in _media_info[vid].items():
                    if value is not None:
                        item[key] = value
                item['file'] = os.path.basename(hit[0])
                item['status'] = 'ready'
                os.utime(hit[0], None)  # refresh mtime so trim keeps it
                await _after_ready(room, item)
                return
        item['status'] = 'downloading'
        item['progress'] = 0
        if room.mode == 'watch':
            await _broadcast_state(room)

        def progress_cb(pct):
            item['progress'] = pct
            loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(
                    _broadcast(room, {'t': 'progress', 'uid': item['uid'], 'pct': pct})))

        cfg = get_settings(room.id)
        hdrs = item.get('_http_headers')
        # Watchdog: a wedged yt-dlp must never hold a download slot forever.
        dl_timeout = 120 if room.mode == 'reels' else 1800
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
            item['uploader'] = info.get('channel') or info.get('uploader') or item.get('uploader')
            item['channel_id'] = info.get('channel_id') or item.get('channel_id')
            if info.get('thumbnail'):
                item['thumbnail'] = info['thumbnail']
            item['width'] = info.get('width')
            item['height'] = info.get('height')
            item['tags_w'] = brain.item_tags(item['title'], info.get('tags'))
            item['status'] = 'ready'
            if item['vid']:
                _media_info[item['vid']] = {key: item.get(key) for key in _MEDIA_FIELDS}
                if len(_media_info) > 2000:
                    for key in list(_media_info)[:500]:
                        _media_info.pop(key, None)
            if room.mode == 'reels' and item.get('why', {}).get('kind') != 'added':
                problem = _reel_problem(item)
                if problem:
                    item['status'] = 'error'
                    item['error'] = problem
                    try:
                        os.remove(os.path.join(cache_dir, fname))
                    except OSError:
                        pass
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
            elif room.mode == 'reels' and item.get('why', {}).get('kind') != 'added':
                logger.info(f"reel download failed for {item.get('url')}: {e}")
                item['status'] = 'error'
                item['error'] = str(e)[:200]
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
        fmt = await loop.run_in_executor(None, _blocking_hls_formats, item['url'], cfg['quality'])
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
            '-hls_segment_filename', os.path.join(out_dir, 's%06d.ts'), playlist]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL, start_new_session=True)

    def seg_count():
        try:
            with open(playlist, 'r') as f:
                return f.read().count('#EXTINF')
        except OSError:
            return 0

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
                await _broadcast(room, {'t': 'progress', 'uid': item['uid'], 'pct': pct})
    item['live_dl'] = False
    item['progress'] = 100
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
    if room.mode == 'reels' and item.get('status') == 'error':
        _prune_dead_reels(room)
    # The room was waiting on this one: start it from the top now, not from
    # however long the download took.
    if item['status'] in ('ready', 'embed') and room.current() is item and not item.get('_started'):
        room.set_position(0, playing=True)
    if item['status'] in ('ready', 'embed'):
        item['_started'] = room.current() is item or item.get('_started')
    # auto-start when nothing is playing yet and this item became playable
    if item['status'] in ('ready', 'embed') and room.current() is None:
        try:
            room.index = room.queue.index(item)
            room.set_position(0, playing=True)
        except ValueError:
            pass
    cur = room.current()
    if room.mode == 'reels' and cur and cur.get('status') == 'error':
        _prune_dead_reels(room)
    # The queue had run out and this is the next one: carry on with it.
    if (room.mode == 'watch' and item['status'] in ('ready', 'embed') and cur is not None
            and cur is not item and not room.playing and room.index + 1 < len(room.queue)
            and room.queue[room.index + 1] is item and (cur.get('duration') or 0)
            and room.get_position() >= cur['duration'] - 1.5):
        _move_to(room, room.index + 1)
    await _broadcast_state(room)


def _new_item(url, title, added_by=None, **extra):
    item = {
        'uid': secrets.token_hex(6), 'vid': None, 'url': url, 'title': title,
        'duration': None, 'thumbnail': None, 'uploader': None, 'channel_id': None,
        'status': 'pending', 'progress': 0, 'file': None,
        'added_by': _public_person(added_by) if added_by else None,
        'likes': [], 'liker_names': {}, 'tags_w': {},
    }
    item.update(extra)
    return item


async def _add_query(room, query, by, play_next=False):
    """Add a link or the first search result. *by* is a member dict."""
    if len(room.queue) >= MAX_QUEUE_ITEMS:
        await _notice(room, f'The queue is full ({MAX_QUEUE_ITEMS} videos)', kind='error')
        return None
    if URL_RE.match(query):
        try:
            await validate_public_url(query)
        except PublicURLRequired as exc:
            await _notice(room, str(exc), kind='error')
            return None
    item = _new_item(query, query if URL_RE.match(query) else f'Searching “{query}”…', by)
    if room.mode == 'reels':
        item['why'] = {'kind': 'added', 'label': f'added by {by["name"]}' if by else 'added'}
        # pasted reels play right after the current one, in the order added
        pos = max(0, room.index + 1)
        while pos < len(room.queue) and (room.queue[pos].get('why') or {}).get('kind') == 'added':
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
        return item
    if not info:
        await _sniff_or_fallback(room, item, 'No results found')
        return item
    item['vid'] = info.get('id')
    item['url'] = info.get('webpage_url') or info.get('url') or query
    item['title'] = info.get('title') or item['title']
    item['duration'] = info.get('duration')
    item['thumbnail'] = info.get('thumbnail') or _thumb(item['vid'])
    item['uploader'] = info.get('channel') or info.get('uploader')
    item['channel_id'] = info.get('channel_id')
    item['tags_w'] = brain.item_tags(item['title'], info.get('tags'))
    if room.mode == 'reels':
        brain.mark_seen(room.feed, item['vid'])
        if by:
            profile = people_doc.data.setdefault(by['id'], brain.new_profile())
            brain.learn_person(profile, item, 'added')
            people_doc.touch()
        feeds_doc.touch()
    await _broadcast_state(room)
    if room.mode == 'watch' and (info.get('duration') or 0) >= HLS_MIN_DURATION:
        _spawn(_hls_item(room, item), f'hls:{room.id}:{item["uid"]}')
    else:
        _spawn(_download_item(room, item), f'download:{room.id}:{item["uid"]}')
    return item


async def _sniff_or_fallback(room, item, err):
    """yt-dlp couldn't resolve the page. If it's a real URL, read its network
    traffic in a headless browser and let the room pick from the media it
    loads (sniffing can catch ads, hence the picker)."""
    url = item.get('url') or ''
    if room.mode != 'watch' or not URL_RE.match(url) or not cobrowser:
        _set_embed_fallback(item, err)
        await _after_ready(room, item)
        return
    item['status'] = 'sniffing'
    item['error'] = str(err)[:200]
    await _broadcast_state(room)
    try:
        found = await cobrowser.sniff_media(url, adblock=get_settings(room.id)['adblock'])
    except Exception as e:
        logger.warning(f"sniff failed for {url}: {e}")
        found = []
    if not found:
        _set_embed_fallback(item, err)
        await _after_ready(room, item)
        return
    item['choices'] = [{k: m[k] for k in ('url', 'mime', 'kind', 'name')} for m in found]
    item['_choice_headers'] = {m['url']: m.get('headers') or {} for m in found}
    item['status'] = 'choice'
    await _notice(room, f'Found {len(found)} videos on that page. Pick the right one (some could be ads).')
    await _after_ready(room, item)


# ---------------------------------------------------------------- reels feed

_source_cache = {}   # url -> (fetched at, entries)


async def _source_entries(url):
    now = time.monotonic()
    cached = _source_cache.get(url)
    if cached and now - cached[0] < SOURCE_CACHE_TTL:
        return cached[1]
    loop = asyncio.get_running_loop()
    entries = await asyncio.wait_for(
        loop.run_in_executor(None, _blocking_flat, url, 60), 30)
    _source_cache[url] = (now, entries)
    if len(_source_cache) > 200:
        for key in sorted(_source_cache, key=lambda k: _source_cache[k][0])[:50]:
            _source_cache.pop(key, None)
    return entries


def _viable_ahead(room):
    return sum(1 for it in room.queue[room.index + 1:] if it.get('status') != 'error')


def _prune_dead_reels(room):
    """Drop dead feed reels so they can't block the swipe path."""
    removed = False
    i = 0
    while i < len(room.queue):
        it = room.queue[i]
        if it.get('status') == 'error' and i != room.index:
            room.queue.pop(i)
            if i < room.index:
                room.index -= 1
            removed = True
            continue
        i += 1
    cur = room.current()
    if cur and cur.get('status') == 'error' and room.index + 1 < len(room.queue):
        room.queue.pop(room.index)
        room._advanced_past = room.index - 1
        room.set_position(0, playing=True)
        removed = True
    return removed


def _people(room):
    return [(uid, people_doc.data.get(uid)) for uid in room.present_ids()]


async def _reels_topup(room):
    if room._topup_busy:
        return
    room._topup_busy = True
    try:
        await _reels_topup_inner(room)
    finally:
        room._topup_busy = False


async def _reels_topup_inner(room, rng=random):
    if _prune_dead_reels(room):
        await _broadcast_state(room)
    tried = set()
    for _attempt in range(4):
        missing = REELS_READY_AHEAD - _viable_ahead(room)
        if missing <= 0 or len(room.queue) >= MAX_QUEUE_ITEMS:
            return
        people = _people(room)
        profiles = [p for _uid, p in people if p]
        group = brain.taste(profiles, room.feed.get('seeds'))
        if not group and not room.queue:
            return   # nobody has said what they like yet: the seed screen asks
        driver_id, driver = brain.choose_driver(room.feed, people)
        driver_taste = brain.taste([driver]) if driver else None
        creators = brain.creators_of([driver] if driver else profiles)
        arm, why = brain.choose_arm(room.feed, group, driver_taste, creators, rng=rng,
                                    exclude=tried)
        tried.add(arm)
        try:
            entries = await _source_entries(brain.arm_url(arm))
        except Exception as e:
            logger.info(f"reels source {arm} failed: {e}")
            entries = []
        blend = dict(group)
        for tag, weight in (driver_taste or {}).items():
            blend[tag] = blend.get(tag, 0) + weight
        picks = brain.rank_entries(entries, room.feed, blend, rng=rng, limit=min(2, missing))
        if not picks:
            brain.learn_arm(room.feed, arm, 'skip')
            continue
        names = {cid: name for cid, name, _w in creators}
        driver_name = next((m['name'] for m in room.present() if m['id'] == driver_id), None)
        label = brain.arm_label(arm, names)
        for entry in picks:
            vid = entry.get('id')
            brain.mark_seen(room.feed, vid)
            item = _new_item(
                f'https://www.youtube.com/shorts/{vid}', entry.get('title') or 'Reel',
                vid=vid, thumbnail=_thumb(vid), arm=arm,
                why={'kind': 'explore' if why == 'explore' else why,
                     'label': label if why != 'explore' else f'something new · {label}',
                     'for': driver_name if why != 'explore' and len(people) > 1 else None})
            item['tags_w'] = brain.item_tags(entry.get('title'))
            room.queue.append(item)
            brain.note_creator(room.feed, entry.get('channel_id') or (entry.get('channel') or '').lower())
            _spawn(_download_item(room, item), f'download:{room.id}:{item["uid"]}')
        feeds_doc.touch()
        await _broadcast_state(room)


async def _reels_loop(room):
    try:
        while room.sockets:
            try:
                await _reels_topup(room)
            except Exception as e:
                logger.error(f"reels loop error: {e}", exc_info=True)
            await asyncio.sleep(3)
    finally:
        room.reels_task = None


def _ensure_reels_loop(room):
    if room.mode == 'reels' and room.reels_task is None and room.sockets:
        room.reels_task = _spawn(_reels_loop(room), f'reels:{room.id}')


def _learn(room, item, signal, uids):
    """Teach the feed: the item's arm once, each listed person's taste."""
    if not item:
        return
    brain.learn_arm(room.feed, item.get('arm'), signal)
    for uid in uids:
        profile = people_doc.data.setdefault(uid, brain.new_profile())
        brain.learn_person(profile, item, signal)
    people_doc.touch()
    feeds_doc.touch()


def _conclude_reel(room, how, by=None):
    """The reel on screen is leaving: what did the room think of it?"""
    item = room.current()
    if not item or item.get('_visit_done') or item.get('status') != 'ready':
        return
    item['_visit_done'] = True
    duration = item.get('duration') or 0
    if not duration:
        return
    ratio = room.get_position() / duration
    present = room.present_ids()
    if how == 'ended' or ratio >= 0.95:
        _learn(room, item, 'complete', present)
    elif ratio >= 0.5:
        _learn(room, item, 'partial', present)
    elif ratio < 0.33 and how in ('swipe', 'less') and by:
        _learn(room, item, 'skip', [by['id']])


def _move_to(room, index):
    room.index = index
    room._advanced_past = index - 1
    room.skip_votes.clear()
    room.set_position(0, playing=True)
    item = room.current()
    if item:
        item.pop('_visit_done', None)
        item['_started'] = item.get('status') in ('ready', 'embed')


async def _reel_step(room, direction, how='swipe', by=None):
    """Next (direction 1) or previous (-1) playable reel for everyone."""
    if direction > 0:
        _conclude_reel(room, how, by)
        nxt = room.index + 1
        while nxt < len(room.queue) and room.queue[nxt].get('status') == 'error':
            nxt += 1
        if nxt < len(room.queue):
            _move_to(room, nxt)
        else:
            _spawn(_reels_topup(room), f'topup:{room.id}')
            return 'waiting'
    else:
        prev = room.index - 1
        while prev >= 0:
            item = room.queue[prev]
            playable = item.get('status') == 'embed' or (
                item.get('file') and os.path.isfile(os.path.join(REELS_CACHE_DIR, item['file'])))
            if playable:
                break
            prev -= 1
        if prev < 0:
            return False
        _move_to(room, prev)
    await _broadcast_state(room)
    if direction > 0:
        _spawn(_reels_topup(room), f'topup:{room.id}')
    return True


async def _more_from_creator(room, member):
    """Two more shorts from the creator of the reel on screen, right after it."""
    item = room.current()
    cid = item and item.get('channel_id')
    if not cid:
        await _send_member(room, member, {'t': 'notice', 'kind': 'error',
                                          'text': "Can't tell who made this one"})
        return
    profile = people_doc.data.setdefault(member['id'], brain.new_profile())
    brain.learn_person(profile, item, 'like')
    people_doc.touch()
    try:
        entries = await _source_entries(brain.arm_url(f'creator:{cid}'))
    except Exception:
        entries = []
    picks = brain.rank_entries(entries, room.feed, {}, limit=2)
    if not picks:
        await _notice(room, f'No more shorts from {item.get("uploader") or "them"} right now', kind='error')
        return
    pos = room.index + 1
    for entry in picks:
        vid = entry.get('id')
        brain.mark_seen(room.feed, vid)
        new = _new_item(f'https://www.youtube.com/shorts/{vid}', entry.get('title') or 'Reel',
                        vid=vid, thumbnail=_thumb(vid), arm=f'creator:{cid}', channel_id=cid,
                        uploader=item.get('uploader'),
                        why={'kind': 'creator', 'label': f'more from @{item.get("uploader") or "them"}',
                             'for': member['name']})
        new['tags_w'] = brain.item_tags(entry.get('title'))
        room.queue.insert(pos, new)
        pos += 1
        _spawn(_download_item(room, new), f'download:{room.id}:{new["uid"]}')
    await _notice(room, f'{member["name"]} wants more from @{item.get("uploader") or "them"}', by=member)
    await _broadcast_state(room)


async def _send_member(room, member, payload):
    for ws, value in list(room.sockets.items()):
        if value is member:
            await _send(ws, payload)


# ---------------------------------------------------------------- sync loop

async def _sync_loop(room):
    try:
        while room.sockets:
            await asyncio.sleep(5)
            await _broadcast(room, _sync_payload(room))
    finally:
        room.sync_task = None


# ---------------------------------------------------------------- playlists

def _clean_playlist_name(value):
    return _clean_text(value, 50)


def _room_playlist_lock(room):
    if room.playlist_lock is None:
        room.playlist_lock = asyncio.Lock()
    return room.playlist_lock


def _can_edit_playlist(playlist, member):
    return _can_manage(member) or playlist.get('owner_id') == member['id']


async def _save_room_playlist(room, name, member):
    name = _clean_playlist_name(name)
    if not name:
        await _notice(room, 'Give the playlist a name', kind='error')
        return
    playlists = playlists_doc.data.setdefault(room.id, {})
    if name not in playlists and len(playlists) >= MAX_ROOM_PLAYLISTS:
        await _notice(room, f'This room already has {MAX_ROOM_PLAYLISTS} playlists', kind='error')
        return
    existing = playlists.get(name)
    if existing and not _can_edit_playlist(existing, member):
        await _notice(room, f'Only {existing.get("owner_name") or "its owner"} or a moderator can change “{name}”', kind='error')
        return
    items = []
    for item in room.queue[:MAX_PLAYLIST_ITEMS]:
        url = str(item.get('url') or '')
        if URL_RE.match(url):
            items.append({'url': url, 'title': str(item.get('title') or '')[:200],
                          'duration': item.get('duration'), 'thumbnail': item.get('thumbnail'),
                          'uploader': item.get('uploader')})
    if not items:
        await _notice(room, 'Nothing in the queue can be saved', kind='error')
        return
    playlists[name] = {
        'items': items,
        'owner_id': (existing or {}).get('owner_id') or member['id'],
        'owner_name': (existing or {}).get('owner_name') or member['name'],
        'updated_by': member['name'], 'updated_at': time.time(),
    }
    playlists_doc.touch()
    increment('playlists_saved_total')
    await _notice(room, f'{member["name"]} saved “{name}” ({len(items)} videos)', by=member)
    await _broadcast_state(room)


async def _load_room_playlist(room, name, member):
    playlist = (playlists_doc.data.get(room.id) or {}).get(_clean_playlist_name(name))
    if not playlist:
        await _notice(room, 'That playlist is gone', kind='error')
        return
    available = max(0, MAX_QUEUE_ITEMS - len(room.queue))
    entries = list(playlist.get('items') or [])[:min(MAX_PLAYLIST_ITEMS, available)]
    if not entries:
        await _notice(room, 'The queue is full', kind='error')
        return
    await _notice(room, f'{member["name"]} is adding “{name}”', by=member)
    increment('playlists_loaded_total')
    for entry in entries:
        if task_registry.closing:
            return
        await _add_query(room, str(entry.get('url') or ''), member)


async def _delete_room_playlist(room, name, member):
    name = _clean_playlist_name(name)
    playlists = playlists_doc.data.get(room.id) or {}
    if name not in playlists:
        await _notice(room, 'That playlist is gone', kind='error')
        return
    if not _can_edit_playlist(playlists[name], member):
        await _notice(room, f'Only its owner or a moderator can delete “{name}”', kind='error')
        return
    playlists.pop(name, None)
    if not playlists:
        playlists_doc.data.pop(room.id, None)
    playlists_doc.touch()
    await _notice(room, f'{member["name"]} deleted “{name}”', by=member)
    await _broadcast_state(room)


# ---------------------------------------------------------------- shared browser

async def _handle_browser_message(room, ws, member, data):
    """The shared browser (moderators only); returns whether it was handled."""
    event = data.get('t')
    browser_events = {'browser_start', 'browser_stop', 'browser_control', 'browser_nav',
                      'bmouse', 'bkey', 'browser_media', 'browser_pick', 'pick_choice'}
    if event not in browser_events:
        return False
    name = member['name']
    if event != 'pick_choice' and not _can_manage(member):
        await _send(ws, {'t': 'notice', 'kind': 'error',
                         'text': 'Only the host or a moderator can use the shared browser'})
        increment('role_denials_total')
        return True
    sess = cobrowser.get(room.id) if cobrowser else None

    if event == 'browser_start':
        if not cobrowser:
            await _send(ws, {'t': 'notice', 'kind': 'error', 'text': 'The shared browser is not available here'})
            return True
        url = str(data.get('url') or '').strip()[:500]
        if url:
            try:
                await validate_public_url(url)
            except PublicURLRequired as exc:
                await _send(ws, {'t': 'notice', 'kind': 'error', 'text': str(exc)})
                return True
        if sess:
            if url:
                await sess.navigate(url)
            return True
        room.set_position(room.get_position(), playing=False)
        await _notice(room, f'{name} is opening the shared browser…', by=member)
        await _broadcast_state(room)

        async def on_change():
            await _broadcast_state(room)
        try:
            cfg = get_settings(room.id)
            await cobrowser.start(room.id, url, started_by=member['id'], on_change=on_change,
                                  adblock=cfg['adblock'], quality=cfg['quality'])
        except Exception as exc:
            logger.exception('Shared browser failed to start')
            await _notice(room, f'The shared browser failed to start: {exc}', kind='error')
        await _broadcast_state(room)
    elif event == 'browser_stop' and sess:
        await _notice(room, f'{name} closed the shared browser', by=member)
        await sess.stop()
    elif event == 'browser_control' and sess:
        sess.controller = member['id']
        await _notice(room, f'{name} took the mouse', by=member)
        await _broadcast_state(room)
    elif event == 'browser_nav' and sess and sess.controller == member['id']:
        url = str(data.get('url') or '').strip()[:500]
        if url:
            try:
                await validate_public_url(url)
                await sess.navigate(url)
            except PublicURLRequired as exc:
                await _send(ws, {'t': 'notice', 'kind': 'error', 'text': str(exc)})
    elif event == 'bmouse' and sess and sess.controller == member['id']:
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
            await _broadcast(room, {'t': 'bcursor', 'x': x, 'y': y, 'down': action == 'down'})
    elif event == 'bkey' and sess and sess.controller == member['id']:
        key = str(data.get('key') or '')[:32]
        # Navigation must pass browser_nav URL validation; shortcuts would bypass it.
        if key not in {'Control', 'Alt', 'Meta', 'F4', 'F6'}:
            sess.key(data.get('a', 'down'), key)
    elif event == 'browser_media' and sess:
        try:
            await sess.probe_candidates()
        except Exception:
            logger.debug('Browser media probe failed', exc_info=True)
        await _send(ws, {'t': 'bmedia', 'items': sess.get_media(), 'page': sess.page_url})
    elif event == 'browser_pick' and sess:
        url = str(data.get('url') or '').strip()
        if not url:
            return True
        try:
            await validate_public_url(url)
        except PublicURLRequired as exc:
            await _send(ws, {'t': 'notice', 'kind': 'error', 'text': str(exc)})
            return True
        if len(room.queue) >= MAX_QUEUE_ITEMS:
            await _send(ws, {'t': 'notice', 'kind': 'error', 'text': 'The queue is full'})
            return True
        item = _new_item(url, url.split('?')[0].rstrip('/').split('/')[-1] or url, member,
                         uploader=((sess.page_url or '').split('/')[2]
                                   if '://' in (sess.page_url or '') else None),
                         _http_headers=sess.media_headers(url), _media_kind=sess.media_kind(url))
        room.queue.append(item)
        await _notice(room, f'{name} grabbed a video from the page', by=member)
        await _broadcast_state(room)
        _spawn(_download_item(room, item), f'download:{room.id}:{item["uid"]}')
    elif event == 'pick_choice':
        uid = str(data.get('uid') or '')
        url = str(data.get('url') or '')
        item = next((entry for entry in room.queue if entry['uid'] == uid), None)
        if item and url and item.get('choices') and \
                any(choice['url'] == url for choice in item['choices']):
            if not (_can_manage(member) or (item.get('added_by') or {}).get('id') == member['id']):
                await _send(ws, {'t': 'notice', 'kind': 'error',
                                 'text': 'Only whoever added it or a moderator can pick'})
                return True
            try:
                await validate_public_url(url)
            except PublicURLRequired as exc:
                await _send(ws, {'t': 'notice', 'kind': 'error', 'text': str(exc)})
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
            await _broadcast_state(room)
            _spawn(_download_item(room, item), f'download:{room.id}:{item["uid"]}')
    return True


# ---------------------------------------------------------------- websocket

async def _search(ws, member, q):
    q = _clean_text(q, 200)
    if not q:
        return
    if not search_limiter.allow(member['id']):
        await _send(ws, {'t': 'results', 'q': q, 'items': [], 'error': 'Too many searches, wait a moment'})
        return
    loop = asyncio.get_running_loop()
    try:
        if URL_RE.match(q):
            await validate_public_url(q)
            info = await loop.run_in_executor(None, _blocking_extract, q)
            entries = [info] if info else []
        else:
            entries = await asyncio.wait_for(
                loop.run_in_executor(None, _blocking_flat, f'ytsearch10:{q}', 10), 25)
        items = [_flat_result(e) for e in entries]
        if URL_RE.match(q) and items and not items[0]['url']:
            items[0]['url'] = q
        await _send(ws, {'t': 'results', 'q': q, 'items': [i for i in items if i['url']]})
    except Exception as e:
        await _send(ws, {'t': 'results', 'q': q, 'items': [], 'error': str(e)[:200]})


async def _handle(room, ws, member, data):
    t = data.get('t')
    name = member['name']
    cfg = get_settings(room.id)

    if t == 'ping':
        await _send(ws, {'t': 'pong', 'c': data.get('c'), 's': int(time.time() * 1000)})

    elif t in ('play', 'pause', 'seek'):
        if not _allowed(room, member, 'control_policy' if room.mode == 'watch' else 'swipe_policy'):
            await _send(ws, {'t': 'notice', 'kind': 'error',
                             'text': 'The host has locked playback for this room'})
            await _send(ws, _sync_payload(room))
            return
        if t == 'play':
            sess = cobrowser.get(room.id) if cobrowser else None
            if sess:   # resuming the video ends browser mode
                await sess.stop()
            room.set_position(_safe_float(data.get('pos'), room.get_position()), playing=True)
        elif t == 'pause':
            room.set_position(_safe_float(data.get('pos'), room.get_position()), playing=False)
        else:
            room.set_position(_safe_float(data.get('pos')))
        await _broadcast(room, _sync_payload(room, by=_public_person(member), action=t))

    elif t == 'react':
        emoji = str(data.get('emoji') or '')
        if emoji in REACTIONS:
            await _broadcast(room, {'t': 'react', 'emoji': emoji, 'by': _public_person(member)})
            item = room.current()
            if room.mode == 'reels' and item and learn_limiter.allow((member['id'], item['uid'])) \
                    and not item.get('_reacted', {}).get(member['id']):
                item.setdefault('_reacted', {})[member['id']] = True
                _learn(room, item, 'react', [member['id']])

    elif t == 'search':
        _spawn(_search(ws, member, data.get('q')), f'search:{room.id}')

    elif t == 'add':
        q = _clean_text(data.get('q'), 1000)
        if q and add_limiter.allow(member['id']):
            await _notice(room, f'{name} added {q if not URL_RE.match(q) else "a video"}', by=member)
            _spawn(_add_query(room, q, member, bool(data.get('next'))), f'add:{room.id}')

    elif t == 'jump':
        if not _allowed(room, member, 'control_policy'):
            await _send(ws, {'t': 'notice', 'kind': 'error', 'text': 'The host has locked playback'})
            return
        idx = _safe_int(data.get('index'))
        if 0 <= idx < len(room.queue):
            sess = cobrowser.get(room.id) if cobrowser else None
            if sess:
                await sess.stop()
            _move_to(room, idx)
            await _broadcast_state(room)

    elif t == 'skip' and room.mode == 'watch':
        policy = cfg.get('skip_policy', 'everyone')
        immediate = _can_manage(member) or policy == 'everyone'
        if not immediate and policy == 'managers':
            await _send(ws, {'t': 'notice', 'kind': 'error', 'text': 'Only the host or a moderator can skip'})
            return
        if not immediate:
            room.skip_votes.add(member['id'])
            needed = max(1, math.ceil(len(room.present()) * cfg['vote_threshold']))
            if len(room.skip_votes) < needed:
                await _notice(room, f'{name} voted to skip ({len(room.skip_votes)}/{needed})', by=member)
                await _broadcast_state(room)
                return
        if room.index + 1 < len(room.queue):
            _move_to(room, room.index + 1)
        else:
            room.skip_votes.clear()
            room.set_position(room.get_position(), playing=False)
        await _notice(room, f'{name} skipped', by=member)
        await _broadcast_state(room)

    elif t == 'reorder':
        if not _allowed(room, member, 'control_policy'):
            return
        source, target = _safe_int(data.get('from')), _safe_int(data.get('to'))
        if 0 <= source < len(room.queue) and 0 <= target < len(room.queue) \
                and source != room.index and target != room.index:
            current = room.current()
            item = room.queue.pop(source)
            room.queue.insert(target, item)
            if current in room.queue:
                room.index = room.queue.index(current)
            await _broadcast_state(room)

    elif t == 'remove':
        idx = _safe_int(data.get('index'))
        if not (0 <= idx < len(room.queue)):
            return
        item = room.queue[idx]
        mine = (item.get('added_by') or {}).get('id') == member['id']
        if not (_can_manage(member) or mine):
            await _send(ws, {'t': 'notice', 'kind': 'error',
                             'text': 'You can remove what you added; a moderator can remove anything'})
            return
        room.queue.pop(idx)
        if idx < room.index:
            room.index -= 1
        elif idx == room.index:
            if room.index >= len(room.queue):
                room.index = len(room.queue) - 1
            room._advanced_past = room.index - 1
            room.skip_votes.clear()
            room.set_position(0, playing=room.index >= 0)
        await _broadcast_state(room)

    elif t == 'ended':
        idx = _safe_int(data.get('index'))
        if idx != room.index or idx <= room._advanced_past:
            return
        if room.mode == 'watch':
            room._advanced_past = idx
            if room.index + 1 < len(room.queue):
                _move_to(room, room.index + 1)
            else:
                # The end of the queue: stay on the last frame until someone adds more.
                room.skip_votes.clear()
                duration = (room.current() or {}).get('duration') or room.get_position()
                room.set_position(min(room.get_position(), duration), playing=False)
            await _broadcast_state(room)
        elif cfg.get('autoscroll'):
            item = room.current()
            duration = (item or {}).get('duration') or 0
            # Very short clips play twice; nobody's tab can skip one early.
            need = max(duration * 0.9, min(duration * 2, 8)) if duration else 0
            if room.get_position() + 0.75 >= need:
                room._advanced_past = idx
                await _reel_step(room, 1, how='ended')

    elif t == 'swipe' and room.mode == 'reels':
        if not _allowed(room, member, 'swipe_policy'):
            await _send(ws, {'t': 'notice', 'kind': 'error', 'text': 'The host is the one swiping in this room'})
            return
        direction = -1 if _safe_int(data.get('dir'), 1) < 0 else 1
        moved = await _reel_step(room, direction, how='swipe', by=member)
        if moved is False:
            await _send(ws, {'t': 'notice', 'text': 'That was the first reel'})
        elif moved == 'waiting':
            await _send(ws, {'t': 'notice', 'text': 'Finding the next reel…'})
        else:
            await _broadcast(room, {'t': 'swiped', 'dir': direction, 'by': _public_person(member)})

    elif t == 'like' and room.mode == 'reels':
        item = room.current()
        if not item:
            return
        likes = item.setdefault('likes', [])
        if member['id'] in likes:
            likes.remove(member['id'])
            _learn(room, item, 'unlike', [member['id']])
            liked = False
        else:
            likes.append(member['id'])
            item.setdefault('liker_names', {})[member['id']] = member['name']
            _learn(room, item, 'like', [member['id']])
            liked = True
        await _broadcast(room, {'t': 'heart', 'uid': item['uid'], 'likes': len(likes),
                                'liked': liked, 'by': _public_person(member),
                                'liked_by': [item['liker_names'].get(u, '') for u in likes[-5:]]})

    elif t == 'less' and room.mode == 'reels':
        item = room.current()
        if not item:
            return
        item['_visit_done'] = True     # this verdict replaces the watch-time one
        _learn(room, item, 'less', [member['id']])
        await _send(ws, {'t': 'notice', 'text': 'Got it, less like this for you'})
        if _allowed(room, member, 'swipe_policy'):
            await _reel_step(room, 1, how='less', by=member)

    elif t == 'creator' and room.mode == 'reels':
        _spawn(_more_from_creator(room, member), f'creator:{room.id}')

    elif t == 'seed' and room.mode == 'reels':
        words = [str(w) for w in (data.get('topics') or []) if isinstance(w, str)][:20]
        words += brain.split_topics(_clean_text(data.get('text'), 200))
        profile = people_doc.data.setdefault(member['id'], brain.new_profile())
        added = brain.seed_person(profile, words)
        if added:
            people_doc.touch()
            await _notice(room, f'{name} is into ' + ', '.join(f'#{t}' for t in added[:5]), by=member)
            _ensure_reels_loop(room)
            _spawn(_reels_topup(room), f'topup:{room.id}')
            await _broadcast_state(room)

    elif t == 'tune' and room.mode == 'reels':
        profile = people_doc.data.setdefault(member['id'], brain.new_profile())
        if data.get('remove'):
            tag = brain.normalize_tag(data.get('remove'))
            brain.forget_tag(profile, tag)
            room.feed.get('seeds', {}).pop(tag, None)
            # drop queued reels that came from it (not the one playing)
            room.queue[room.index + 1:] = [
                it for it in room.queue[room.index + 1:] if it.get('arm') != f'tag:{tag}']
            brain.learn_arm(room.feed, f'tag:{tag}', 'less')
        if data.get('add'):
            brain.seed_person(profile, brain.split_topics(_clean_text(data.get('add'), 120)))
        if data.get('reset_me'):
            people_doc.data[member['id']] = brain.new_profile()
            await _send(ws, {'t': 'notice', 'text': 'Your Reels taste starts fresh'})
        people_doc.touch()
        feeds_doc.touch()
        _spawn(_reels_topup(room), f'topup:{room.id}')
        await _broadcast_state(room)

    elif t == 'playlist_save' and room.mode == 'watch':
        async with _room_playlist_lock(room):
            await _save_room_playlist(room, data.get('name'), member)

    elif t == 'playlist_load' and room.mode == 'watch':
        playlist_name = _clean_playlist_name(data.get('name'))
        _spawn(_load_room_playlist(room, playlist_name, member), f'playlist:{room.id}')

    elif t == 'playlist_delete' and room.mode == 'watch':
        async with _room_playlist_lock(room):
            await _delete_room_playlist(room, data.get('name'), member)

    elif t == 'settings':
        if not _can_manage(member):
            await _send(ws, {'t': 'notice', 'kind': 'error', 'text': 'Only the host or a moderator can change settings'})
            return
        allowed = ('adblock', 'sponsorblock', 'quality', 'control_policy', 'skip_policy',
                   'vote_threshold', 'swipe_policy', 'autoscroll')
        try:
            before = get_settings(room.id)
            after = update_settings(room.id, **{k: data[k] for k in allowed if k in data})
        except (TypeError, ValueError) as exc:
            await _send(ws, {'t': 'notice', 'kind': 'error', 'text': str(exc)})
            return
        if after != before:
            if after.get('skip_policy') != before.get('skip_policy'):
                room.skip_votes.clear()
            await _notice(room, f'{name} changed the room settings', by=member)
            await _broadcast_state(room)

    elif t == 'role':
        if not _can_manage(member):
            return
        target_id = str(data.get('id') or '')[:80]
        role = data.get('role')
        if role not in ('moderator', 'member'):
            return
        mods = mods_doc.data.setdefault(room.id, [])
        if role == 'moderator' and target_id not in mods:
            mods.append(target_id)
        elif role == 'member' and target_id in mods:
            mods.remove(target_id)
        mods_doc.touch()
        changed = None
        for value in room.sockets.values():
            if value['id'] == target_id and value['role'] != 'host':
                if role == 'member' and value.get('hoffle_moderator'):
                    continue      # Hoffle's own moderators stay moderators
                value['role'] = role
                changed = value
        if changed:
            await _notice(room, f'{changed["name"]} is now {"a moderator" if role == "moderator" else "a member"} here', by=member)
            await _broadcast_state(room)

    elif room.mode == 'watch' and await _handle_browser_message(room, ws, member, data):
        pass


async def ws_handler(request):
    room_id = request.query.get('room', '')
    if not ROOM_RE.fullmatch(room_id):
        raise web.HTTPBadRequest(text='bad room')
    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=64 * 1024)
    await ws.prepare(request)
    room = None
    member = None
    try:
        try:
            first = await ws.receive(timeout=15)
            hello = json.loads(first.data) if first.type == WSMsgType.TEXT else {}
        except (asyncio.TimeoutError, ValueError, TypeError):
            hello = {}
        info = _session(room_id, hello.get('session')) if isinstance(hello, dict) else None
        if not info:
            await ws.close(code=4001, message=b'session expired')
            return ws
        try:
            room = _get_room(room_id, info.get('room_name'))
        except RuntimeError:
            await ws.close(code=4503, message=b'server busy')
            return ws
        if len(room.sockets) >= MAX_ROOM_PARTICIPANTS:
            await ws.close(code=4503, message=b'room is full')
            return ws
        first_here = info['user']['id'] not in room.present_ids()
        member = room.join(ws, info)
        room.last_activity = time.monotonic()
        increment('websocket_joins_total')
        if room.sync_task is None:
            room.sync_task = _spawn(_sync_loop(room), f'sync:{room.id}')
        _ensure_reels_loop(room)
        welcome = {'t': 'welcome', 'you': dict(_public_person(member), role=member['role']),
                   'media': f'/watch/media/{room.id}/{media_key(room.id)}/',
                   'reactions': list(REACTIONS), 's': int(time.time() * 1000)}
        if room.mode == 'reels':
            welcome['topics'] = [{'tag': t, 'label': l, 'emoji': e} for t, l, e in brain.SEED_TOPICS]
        await _send(ws, welcome)
        if first_here:
            await _notice(room, f'{member["name"]} joined', by=member, kind='join')
        await _broadcast_state(room)

        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            increment('websocket_messages_total')
            room.last_activity = time.monotonic()
            if not message_limiter.allow((room.id, id(ws))):
                await _send(ws, {'t': 'notice', 'kind': 'error', 'text': 'Slow down for a moment'})
                continue
            try:
                await _handle(room, ws, member, data)
            except Exception:
                logger.exception('watch message %s failed', data.get('t'))
    finally:
        if room is not None:
            room.sockets.pop(ws, None)
            message_limiter.discard((room.id, id(ws)))
            room.last_activity = time.monotonic()
            if member:
                if member['id'] not in room.present_ids():
                    room.skip_votes.discard(member['id'])
                    await _notice(room, f'{member["name"]} left', by=member, kind='leave')
                await _broadcast_state(room)
    return ws


# ---------------------------------------------------------------- http routes

_digests = {}   # asset path -> (mtime, digest)


def _digest(asset):
    try:
        mtime = os.path.getmtime(asset)
    except OSError:
        return '0'
    cached = _digests.get(asset)
    if cached and cached[0] == mtime:
        return cached[1]
    with open(asset, 'rb') as handle:
        digest = hashlib.sha1(handle.read()).hexdigest()[:10]
    _digests[asset] = (mtime, digest)
    return digest


def _page(name):
    """A page from webstatic/, with each asset link stamped by that asset's
    content, so a changed script is never served from a stale cache."""
    with open(os.path.join(STATIC_DIR, name), 'r', encoding='utf-8') as handle:
        html = handle.read()
    return re.sub(r'/watch/static/([\w.-]+)',
                  lambda m: f'/watch/static/{m.group(1)}?v={_digest(os.path.join(STATIC_DIR, os.path.basename(m.group(1))))}',
                  html)


def _page_response(name):
    return web.Response(text=_page(name), content_type='text/html', headers={
        'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
        'X-Content-Type-Options': 'nosniff'})


async def watch_page(request):
    return _page_response('watch.html')


async def reels_page(request):
    return _page_response('reels.html')


async def api_session(request):
    """Trade a ticket from Hoffle for a session id."""
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text='invalid json')
    room_id = str(body.get('room') or '')[:90]
    if not ROOM_RE.fullmatch(room_id):
        return web.json_response({'error': 'bad room'}, status=400)
    sid = body.get('session')
    if sid and _session(room_id, sid):
        return web.json_response({'session': sid})
    sid = _redeem_ticket(room_id, body.get('ticket'))
    if not sid:
        return web.json_response({'error': 'This link has expired. Open the activity again from Hoffle.'},
                                 status=401)
    return web.json_response({'session': sid})


async def media(request):
    room_id = request.match_info['room_id']
    if not ROOM_RE.fullmatch(room_id) or \
            not hmac.compare_digest(request.match_info['key'], media_key(room_id)):
        raise web.HTTPForbidden(text='bad media key')
    room = rooms.get(room_id)
    rel = request.match_info['file']
    if not room or not _room_owns_file(room, rel):
        raise web.HTTPNotFound(text='media does not belong to this room')
    for d in (WATCH_CACHE_DIR, REELS_CACHE_DIR):
        root = os.path.realpath(d)
        path = os.path.realpath(os.path.join(d, rel))
        if not path.startswith(root + os.sep):
            continue
        if os.path.isfile(path):
            if path.endswith('.m3u8'):
                with open(path, 'r', encoding='utf-8') as f:
                    return web.Response(text=f.read(), content_type='application/vnd.apple.mpegurl',
                                        headers={'Cache-Control': 'no-cache'})
            return web.FileResponse(path, headers={'Cache-Control': 'private, max-age=3600'})
    raise web.HTTPNotFound()


async def static_file(request):
    fname = os.path.basename(request.match_info['file'])
    path = os.path.join(STATIC_DIR, fname)
    if os.path.isfile(path) and not fname.endswith('.html'):
        return web.FileResponse(path, headers={'Cache-Control': 'public, max-age=604800, immutable'
                                               if request.query.get('v') else 'no-cache'})
    raise web.HTTPNotFound()


async def stream_ws(request):
    """Binary MPEG-TS stream of the room's shared browser."""
    room_id = request.query.get('room', '')
    if not ROOM_RE.fullmatch(room_id) or \
            not hmac.compare_digest(request.query.get('key', ''), media_key(room_id)):
        raise web.HTTPForbidden(text='bad key')
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


# ---------------------------------------------------------------- Hoffle commands

async def command(body):
    """Hoffle's slash commands acting on a room (admin API).

    body: {mode, channel_id, room_name, action: 'add'|'seed'|'status', q, user}
    """
    mode = 'reels' if body.get('mode') == 'reels' else 'watch'
    room_id = _room_id(mode, body.get('channel_id'))
    action = body.get('action')
    if action == 'status':
        room = rooms.get(room_id)
        cur = room.current() if room else None
        return {'active': bool(room and room.sockets),
                'people': len(room.present()) if room else 0,
                'title': cur.get('title') if cur else None}
    user = _clean_user(body.get('user'))
    q = _clean_text(body.get('q'), 1000)
    if not q:
        raise ValueError('nothing to add')
    room = _get_room(room_id, _clean_text(body.get('room_name'), 60) or None)
    by = dict(user, role='member')
    if action == 'add':
        if mode != 'watch':
            raise ValueError('add is for Watch Together')
        if not add_limiter.allow(user['id']):
            raise ValueError('Too many videos at once, wait a moment')
        await _notice(room, f'{user["name"]} added {q if not URL_RE.match(q) else "a video"}', by=by)
        _spawn(_add_query(room, q, by), f'add:{room.id}')
        return {'ok': True, 'text': 'Added to Watch Together.'}
    if action == 'seed':
        profile = people_doc.data.setdefault(user['id'], brain.new_profile())
        added = brain.seed_person(profile, brain.split_topics(q))
        people_doc.touch()
        if room.sockets:
            _ensure_reels_loop(room)
            _spawn(_reels_topup(room), f'topup:{room.id}')
            await _broadcast_state(room)
        return {'ok': True, 'topics': added}
    raise ValueError('unknown action')


# ---------------------------------------------------------------- admin

def admin_snapshot():
    """Password-admin view of persisted and currently active rooms."""
    room_ids = set(settings_doc.data) | set(rooms) | set(playlists_doc.data)
    result = []
    for room_id in sorted(room_ids):
        if not ROOM_RE.fullmatch(room_id):
            continue
        room = rooms.get(room_id)
        result.append({
            'id': room_id,
            'mode': 'reels' if room_id.startswith('r') else 'watch',
            'name': (room.name if room else room_id[3:]),
            'active': bool(room and room.sockets),
            'participants': len(room.present()) if room else 0,
            'queued': len(room.queue) if room else 0,
            'settings': get_settings(room_id),
        })
    return result


def admin_update_room(room_id, data):
    room_id = _validated_room_id(room_id)
    allowed = ('adblock', 'sponsorblock', 'quality', 'control_policy', 'skip_policy',
               'vote_threshold', 'swipe_policy', 'autoscroll')
    cfg = update_settings(room_id, **{k: data[k] for k in allowed if k in data})
    room_name = _clean_text(data.get('name'), 50)
    if room_name and room_id in rooms:
        rooms[room_id].name = room_name
    return cfg


async def admin_delete_room(room_id):
    """Forget a room: its settings, playlists, moderators and Reels feed."""
    room_id = _validated_room_id(room_id)
    exists = room_id in rooms or room_id in settings_doc.data or \
        room_id in playlists_doc.data or room_id in feeds_doc.data
    if not exists:
        raise KeyError(room_id)
    room_tasks = [task for task in list(task_registry.tasks)
                  if not task.done() and (f':{room_id}:' in task.get_name() or
                                          task.get_name().endswith(f':{room_id}'))]
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
    for doc in (settings_doc, playlists_doc, mods_doc, feeds_doc):
        doc.data.pop(room_id, None)
        doc.touch()
    for sid in [k for k, v in sessions.data.items() if v.get('room') == room_id]:
        sessions.data.pop(sid, None)
    sessions.touch()
    logger.info('Admin deleted room %s', room_id)


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
    logger.info('Stopping Watch Together (%s rooms, %s tasks)', len(rooms), task_registry.active)
    sockets = [ws for room in rooms.values() for ws in room.sockets]
    for ws in sockets:
        try:
            await ws.close(code=1001, message=b'server restarting')
        except Exception:
            pass
    if cobrowser:
        try:
            await cobrowser.stop_all()
        except Exception:
            logger.exception('Shared browsers did not all stop cleanly')
    await task_registry.cancel_all()
    for doc in (sessions, settings_doc, playlists_doc, mods_doc, people_doc, feeds_doc):
        doc.flush()
    rooms.clear()
    increment('graceful_shutdowns_total')


def setup(app, bot=None):
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
    # Reels history is per room; a fresh start can show old reels again later.
    for feed in feeds_doc.data.values():
        if isinstance(feed, dict):
            feed['recent_creators'] = []
    app.router.add_get('/watch/', watch_page)
    app.router.add_get('/watch/reels', reels_page)
    app.router.add_get('/watch/ws', ws_handler)
    app.router.add_get('/watch/stream', stream_ws)
    app.router.add_get('/watch/static/{file}', static_file)
    app.router.add_post('/watch/api/session', api_session)
    app.router.add_get('/watch/media/{room_id}/{key}/{file:.+}', media)
    app.on_shutdown.append(shutdown)
    logger.info("🎬 Watch Together mounted at /watch/ (Hoffle activities only)")
