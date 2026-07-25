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
import time

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
        'autoplay': False,
        'artist_diversity': False,
        'vibe_match': False,
        'audio_filter': 'off',
        'crossfade_seconds': 0,
        'automix': False,
        'automix_blend_seconds': 0,
        'karaoke': False,
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
