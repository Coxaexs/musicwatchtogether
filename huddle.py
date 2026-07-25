"""Client for Huddle, the self-hosted chat app that shares this bot.

Huddle voice rooms play music the same way a Discord voice channel does, except
the mixing happens in each listener's browser against a position the Huddle hub
hands out. That means this module never touches audio: it reads and writes the
shared player state over HTTP, and the dashboard treats each Huddle voice room
as one more "guild" it can drive.

Nothing here runs unless HUDDLE_BASE_URL and HUDDLE_BOT_TOKEN are configured,
so a bot with no Huddle beside it behaves exactly as before.
"""

import logging
import json
import os
import hashlib
import time
from collections import Counter
from datetime import datetime

import aiohttp

import config

logger = logging.getLogger('MusicBot.Huddle')

#: Dashboard ids for Huddle rooms are prefixed so they cannot collide with
#: Discord's numeric guild ids.
PREFIX = 'huddle:'

_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=12)
_CACHE_TTL = 2.0

_session = None
_cache = {'at': 0.0, 'servers': []}
_BOT_DIR = os.path.dirname(os.path.abspath(__file__))
_SETTINGS_FILE = os.path.join(_BOT_DIR, 'guild_settings.json')
_DISLIKES_FILE = os.path.join(_BOT_DIR, 'dislikes.json')
_FAVORITES_FILE = os.path.join(_BOT_DIR, 'favorites.json')
_STATS_FILE = os.path.join(_BOT_DIR, 'stats.jsonl')

_DEFAULT_SETTINGS = {
    'autoplay': False,
    'artist_diversity': True,
    'vibe_match': True,
    'automix': False,
    'automix_blend': 8,
    'audio_filter': None,
    'crossfade_seconds': 0,
    'karaoke_mode': False,
}


def _read_json(path, fallback):
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            value = json.load(handle)
        return value if isinstance(value, type(fallback)) else fallback
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return fallback


def _write_json(path, value):
    temporary = path + '.tmp'
    with open(temporary, 'w', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(temporary, path)


def settings_for(channel_or_guild_id):
    guild_id = (channel_or_guild_id if is_huddle_id(channel_or_guild_id)
                else PREFIX + str(channel_or_guild_id))
    stored = _read_json(_SETTINGS_FILE, {}).get(guild_id) or {}
    return {**_DEFAULT_SETTINGS, **stored}


def update_settings(channel_or_guild_id, **patch):
    guild_id = (channel_or_guild_id if is_huddle_id(channel_or_guild_id)
                else PREFIX + str(channel_or_guild_id))
    all_settings = _read_json(_SETTINGS_FILE, {})
    settings = {**_DEFAULT_SETTINGS, **(all_settings.get(guild_id) or {})}
    settings.update(patch)
    all_settings[guild_id] = settings
    _write_json(_SETTINGS_FILE, all_settings)
    return settings


def enabled():
    return bool(config.HUDDLE_BASE_URL and config.HUDDLE_BOT_TOKEN)


def is_huddle_id(guild_id):
    return isinstance(guild_id, str) and guild_id.startswith(PREFIX)


def channel_id_from(guild_id):
    return guild_id[len(PREFIX):] if is_huddle_id(guild_id) else None


async def _client():
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            timeout=_REQUEST_TIMEOUT,
            headers={'Authorization': f'Bearer {config.HUDDLE_BOT_TOKEN}'},
        )
    return _session


async def close():
    global _session
    if _session and not _session.closed:
        await _session.close()
    _session = None


def _url(path):
    return config.HUDDLE_BASE_URL.rstrip('/') + path


async def _request(method, path, payload=None):
    session = await _client()
    async with session.request(method, _url(path), json=payload) as response:
        body = await response.json(content_type=None)
        if response.status >= 400:
            raise RuntimeError((body or {}).get('error') or f'Huddle returned {response.status}')
        return body or {}


async def fetch_servers(force=False):
    """Huddle's servers, their voice rooms, and who/what is in them."""
    if not enabled():
        return []
    now = time.monotonic()
    if not force and now - _cache['at'] < _CACHE_TTL:
        return _cache['servers']
    try:
        data = await _request('GET', '/api/bot/servers')
    except Exception as error:  # a Huddle that is down must not break Discord
        logger.debug('Huddle unreachable: %s', error)
        return _cache['servers'] if now - _cache['at'] < 30 else []
    _cache['servers'] = data.get('servers') or []
    _cache['at'] = now
    return _cache['servers']


async def guild_entries():
    """Voice rooms shaped like the dashboard's guild list entries."""
    entries = []
    for server in await fetch_servers():
        for room in server.get('voiceChannels') or []:
            player = room.get('player') or {}
            track = player.get('track')
            entries.append({
                'id': PREFIX + room['id'],
                'name': f"{server['name']} · {room['name']}",
                'icon': None,
                'platform': 'huddle',
                'connected': bool(track),
                'playing': bool(track) and not player.get('paused'),
                'title': track.get('title') if track else None,
                'listeners': len([m for m in room.get('members') or [] if not m.get('bot')]),
            })
    return entries


def _position_seconds(player):
    """Where the track is now, from the position the hub last published."""
    if not player or not player.get('track'):
        return 0.0
    position = player.get('positionMs') or 0
    if not player.get('paused'):
        # updatedAt is the hub's clock; both machines are this same server.
        position += max(0, time.time() * 1000 - (player.get('updatedAt') or 0))
    return round(position / 1000, 1)


def _song_json(track, requester=None):
    duration = track.get('duration')
    return {
        'title': track.get('title') or 'Unknown',
        'duration': _format_duration(duration),
        'duration_seconds': duration,
        'requester': requester or track.get('requestedBy') or 'Huddle',
        'source_type': 'huddle',
        'thumbnail': track.get('thumbnail'),
        'autoplay_score': None,
        'autoplay_reason': None,
    }


def _format_duration(seconds):
    if not seconds:
        return None
    seconds = int(seconds)
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f'{hours}:{minutes:02d}:{secs:02d}'
    return f'{minutes}:{secs:02d}'


async def room_state(guild_id):
    """The dashboard's guild-state shape for one Huddle voice room."""
    channel_id = channel_id_from(guild_id)
    servers = await fetch_servers()
    room = None
    server = None
    for candidate in servers:
        for entry in candidate.get('voiceChannels') or []:
            if entry['id'] == channel_id:
                room, server = entry, candidate
                break
        if room:
            break
    if not room:
        raise KeyError(guild_id)

    player = room.get('player') or {}
    track = player.get('track')
    current = None
    if track:
        current = _song_json(track)
        current['position_seconds'] = _position_seconds(player)

    members = room.get('members') or []
    settings = settings_for(guild_id)
    return {
        'id': guild_id,
        'name': f"{server['name']} · {room['name']}",
        'icon': None,
        'platform': 'huddle',
        'connected': bool(track),
        'channel': room['name'],
        'playing': bool(track) and not player.get('paused'),
        'paused': bool(player.get('paused')),
        'volume': int(player.get('volume') or 100),
        'loop': {'track': 'song', 'queue': 'queue'}.get(player.get('loop'), 'off'),
        'autoplay': settings['autoplay'],
        'artist_diversity': settings['artist_diversity'],
        'vibe_match': settings['vibe_match'],
        'audio_filter': settings['audio_filter'] or 'off',
        'crossfade_seconds': settings['crossfade_seconds'],
        'automix': settings['automix'],
        'automix_blend_seconds': settings['automix_blend'],
        'karaoke': settings['karaoke_mode'],
        'idle_disconnect_minutes': 0,
        'sleep_timer_ends_at': 0,
        'is_247': False,
        'current': current,
        'queue': [_song_json(item) for item in (player.get('queue') or [])[:100]],
        'queue_length': len(player.get('queue') or []),
        'history': [],
        'active_playlist': None,
        'active_playlist_cover': None,
        'listeners': [m.get('name') for m in members if not m.get('bot')],
    }


async def play(guild_id, query, requested_by='Music dashboard'):
    channel_id = channel_id_from(guild_id)
    await _request('POST', '/api/bot/player', {
        'channelId': channel_id,
        'query': query,
        'requestedBy': requested_by,
    })
    _cache['at'] = 0.0
    return await room_state(guild_id)


#: Dashboard action names that map straight onto a hub player action.
_SIMPLE_ACTIONS = {
    'pause': {'name': 'pause'},
    'resume': {'name': 'resume'},
    'skip': {'name': 'skip'},
    'stop': {'name': 'stop'},
    'shuffle': {'name': 'shuffle'},
    'clear': {'name': 'clear'},
}


async def action(guild_id, body):
    """Translates a dashboard action into a hub player action."""
    name = body.get('action')
    if name == 'settings':
        return await room_state(guild_id)
    if name in ('like', 'dislike'):
        result = await feedback(guild_id, body.get('user_id') or 'huddle', name)
        state = await room_state(guild_id)
        state['feedback'] = {**result, 'kind': name}
        return state
    if name in ('stats', 'wrapped'):
        state = await room_state(guild_id)
        state['report'] = stats(guild_id, wrapped=name == 'wrapped')
        return state
    settings = settings_for(guild_id)
    if name in ('autoplay', 'artist_diversity', 'vibe_match', 'automix', 'karaoke'):
        key = 'karaoke_mode' if name == 'karaoke' else name
        enabled = body.get('enabled')
        if enabled is None:
            enabled = not bool(settings.get(key))
        update_settings(guild_id, **{key: bool(enabled)})
        return await room_state(guild_id)
    if name == 'automix_blend':
        update_settings(
            guild_id,
            automix_blend=max(4, min(15, int(body.get('seconds') or 8))),
        )
        return await room_state(guild_id)
    if name == 'filter':
        preset = str(body.get('preset') or 'off')
        if preset not in ('off', 'bassboost', 'nightcore', 'slowed', '8d', 'karaoke'):
            raise ValueError('invalid filter preset')
        update_settings(
            guild_id,
            audio_filter=None if preset == 'off' else preset,
            karaoke_mode=preset == 'karaoke',
        )
        return await room_state(guild_id)
    if name == 'crossfade':
        update_settings(
            guild_id,
            crossfade_seconds=max(0, min(10, int(body.get('seconds') or 0))),
        )
        return await room_state(guild_id)

    if name in _SIMPLE_ACTIONS:
        payload = dict(_SIMPLE_ACTIONS[name])
    elif name == 'volume':
        payload = {'name': 'volume', 'volume': int(body.get('level') or 0)}
    elif name == 'seek':
        payload = {'name': 'seek', 'positionMs': int(float(body.get('seconds') or 0) * 1000)}
    elif name == 'loop':
        mode = body.get('mode') or 'off'
        payload = {'name': 'loop',
                   'mode': {'song': 'track', 'track': 'track', 'queue': 'queue'}.get(mode, 'off')}
    elif name == 'remove':
        payload = {'name': 'remove', 'index': int(body.get('index') or 0)}
    elif name == 'previous':
        # Huddle keeps no history yet; restarting the track is the closest thing.
        payload = {'name': 'seek', 'positionMs': 0}
    else:
        raise ValueError(f'{name} is not supported for Huddle rooms')

    channel_id = channel_id_from(guild_id)
    await _request('POST', '/api/bot/player', {'channelId': channel_id, 'action': payload})
    _cache['at'] = 0.0
    return await room_state(guild_id)


def _feedback_file(kind):
    return _FAVORITES_FILE if kind == 'like' else _DISLIKES_FILE


async def feedback(guild_id, user_id, kind):
    """Toggle a room-scoped like/dislike in the bot's existing data files."""
    if kind not in ('like', 'dislike'):
        raise ValueError('feedback must be like or dislike')
    state = await room_state(guild_id)
    current = state.get('current')
    if not current:
        raise ValueError('Nothing is playing.')
    channel_id = channel_id_from(guild_id)
    servers = await fetch_servers(force=True)
    track = None
    for server in servers:
        for room in server.get('voiceChannels') or []:
            if room.get('id') == channel_id:
                track = (room.get('player') or {}).get('track')
                break
    if not track:
        raise ValueError('Nothing is playing.')

    path = _feedback_file(kind)
    data = _read_json(path, {})
    stable_user_id = int.from_bytes(
        hashlib.sha256(str(user_id).encode('utf-8')).digest()[:8], 'big'
    ) & 0x7FFFFFFFFFFFFFFF
    entries = data.setdefault(str(stable_user_id), [])
    url = track.get('pageUrl') or track.get('audioUrl')
    for index, entry in enumerate(entries):
        if entry.get('url') == url and str(entry.get('guild_id')) == guild_id:
            entries.pop(index)
            _write_json(path, data)
            return {'added': False, 'title': track.get('title') or 'track'}
    entries.append({
        'title': track.get('title') or 'Unknown',
        'url': url,
        'artist': track.get('artist'),
        'guild_id': guild_id,
    })
    _write_json(path, data)
    return {'added': True, 'title': track.get('title') or 'track'}


def record_play(channel_id, track):
    """Write Huddle plays into the exact stats stream used by /stats/wrapped."""
    if not track:
        return
    entry = {
        'ts': int(time.time()),
        'guild_id': PREFIX + str(channel_id),
        'user_id': track.get('requestedBy'),
        'user_name': track.get('requestedBy') or 'Huddle',
        'title': track.get('title') or 'Unknown',
        'url': track.get('pageUrl') or track.get('audioUrl'),
        'seconds': track.get('duration') or 0,
        'channel_id': channel_id,
    }
    try:
        # A publisher restart can rediscover the track already in progress. Avoid
        # counting that as a second listen in stats/wrapped.
        try:
            with open(_STATS_FILE, 'rb') as handle:
                handle.seek(0, 2)
                end = handle.tell()
                handle.seek(max(0, end - 16384))
                recent_lines = handle.read().decode('utf-8', errors='ignore').splitlines()
            for line in reversed(recent_lines[-50:]):
                try:
                    recent = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (
                    recent.get('guild_id') == entry['guild_id']
                    and recent.get('url') == entry['url']
                    and entry['ts'] - int(recent.get('ts') or 0) < 120
                ):
                    return
        except FileNotFoundError:
            pass
        with open(_STATS_FILE, 'a', encoding='utf-8') as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + '\n')
    except OSError as error:
        logger.debug('Could not log Huddle play: %s', error)


def stats(guild_id, wrapped=False):
    now = datetime.now()
    start = None
    label = 'All time'
    if wrapped:
        start = datetime(now.year, now.month, 1)
        label = f"{now.strftime('%B %Y')} so far"
    plays = []
    try:
        with open(_STATS_FILE, 'r', encoding='utf-8') as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(entry.get('guild_id')) != guild_id:
                    continue
                if start and int(entry.get('ts') or 0) < int(start.timestamp()):
                    continue
                plays.append(entry)
    except FileNotFoundError:
        pass
    songs = Counter(item.get('title') or 'Unknown' for item in plays).most_common(5)
    requesters = Counter(
        item.get('user_name') or 'Unknown' for item in plays
    ).most_common(5)
    return {
        'label': label,
        'plays': len(plays),
        'unique': len({item.get('url') for item in plays}),
        'hours': round(sum(item.get('seconds') or 0 for item in plays) / 3600, 1),
        'topSongs': songs,
        'topRequesters': requesters,
    }


async def say(channel_id, text, link=None, action_label=None):
    """Posts a bot message into a Huddle text channel."""
    if not enabled():
        return
    try:
        await _request('POST', '/api/bots/messages', {
            'channelId': channel_id,
            'content': text,
            'name': 'Music + Watch',
            'avatar': '♫',
            'link': link,
            'actionLabel': action_label,
        })
    except Exception as error:
        logger.debug('Could not post to Huddle: %s', error)
