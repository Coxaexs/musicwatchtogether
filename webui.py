"""Web dashboard for the music bot.

Runs an aiohttp server inside the bot process so the browser can see and
control the same MusicPlayer objects the Discord commands use.

Config (via .env / config.py):
    WEB_UI_ENABLED  - "1" (default) or "0"
    WEB_UI_HOST     - bind address, default 0.0.0.0
    WEB_UI_PORT     - default 8722
    WEB_UI_PASSWORD - optional; when set, every API call must carry it
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import time

from aiohttp import web

import config
from music import Song, get_autocomplete_suggestions
from security import SlidingWindowLimiter, client_identity
from storage import load_json, save_json

logger = logging.getLogger('MusicBot.WebUI')

# Tokens persist to disk so /web links survive bot restarts
TOKENS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web_tokens.json')
PLAYLIST_META_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'playlist_meta.json')
PLAYLIST_COVERS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'playlist_covers')


WEB_TOKEN_TTL = 24 * 3600
LOGIN_LIMITER = SlidingWindowLimiter(10, 5 * 60)


def _save_tokens():
    save_json(TOKENS_FILE, temp_tokens, logger)


temp_tokens = load_json(TOKENS_FILE, {}, logger)


def _valid_token_info(token):
    info = temp_tokens.get(token or '')
    if not info:
        return None
    if time.time() - info.get('created_at', 0) >= WEB_TOKEN_TTL:
        temp_tokens.pop(token, None)
        _save_tokens()
        return None
    return info

def generate_token(guild_id):
    token = secrets.token_urlsafe(16)
    # Clean up expired tokens (older than 24 hours)
    now = time.time()
    for k, v in list(temp_tokens.items()):
        if now - v.get('created_at', 0) >= WEB_TOKEN_TTL:
            temp_tokens.pop(k, None)
    temp_tokens[token] = {'guild_id': guild_id, 'created_at': now}
    _save_tokens()
    return token



def _duration_to_seconds(duration):
    """Parse '3:45' / '1:02:03' style strings into seconds, or None."""
    if not duration or not isinstance(duration, str):
        return None
    parts = duration.strip().split(':')
    try:
        parts = [int(p) for p in parts]
    except ValueError:
        return None
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + part
    return seconds


def _thumbnail_from_url(url, thumbnail=None):
    """Return stored art, or derive a stable YouTube thumbnail for older playlists."""
    if thumbnail:
        return thumbnail
    value = str(url or '')
    match = re.search(r'(?:youtu\.be/|[?&]v=|/shorts/|/embed/)([A-Za-z0-9_-]{11})', value)
    if match:
        return f'https://i.ytimg.com/vi/{match.group(1)}/hqdefault.jpg'
    return None


def _song_json(song):
    return {
        'title': song.title,
        'duration': song.duration,
        'duration_seconds': _duration_to_seconds(song.duration),
        'requester': getattr(song.requester, 'display_name', str(song.requester)),
        'source_type': song.source_type,
        'thumbnail': _thumbnail_from_url(song.url, song.thumbnail),
    }


class WebUI:
    def __init__(self, bot):
        self.bot = bot
        self.lyrics_cache = {}

    @property
    def cog(self):
        return self.bot.get_cog('MusicCog')

    # ---------- auth ----------

    @staticmethod
    def _supplied_token(request):
        return request.cookies.get('mb_session') or ''

    @staticmethod
    def _set_session_cookie(request, response, token):
        forwarded_proto = request.headers.get('X-Forwarded-Proto', '')
        response.set_cookie(
            'mb_session', token, max_age=WEB_TOKEN_TTL, httponly=True,
            secure=request.secure or forwarded_proto == 'https',
            samesite='Strict', path='/')

    def _authorized(self, request):
        supplied = self._supplied_token(request)
        
        # Passwords are exchanged at /api/session; API calls only accept an
        # opaque session/invitation token.
        if supplied:
            if _valid_token_info(supplied):
                return True
            return False # Supplied token was invalid/expired
            
        # If no token was supplied:
        password = config.WEB_UI_PASSWORD
        if not password:
            return True # Allowed if no password is set in config
            
        return False


    def _get_allowed_guild_id(self, request):
        supplied = self._supplied_token(request)
        if not supplied:
            return None
        # Return guild ID for invitation tokens; password sessions have None.
        token_info = _valid_token_info(supplied)
        if token_info:
            return token_info['guild_id']
        return None

    @web.middleware
    async def auth_middleware(self, request, handler):
        if request.path == '/api/session':
            return await handler(request)
        if request.path.startswith('/api/') and not self._authorized(request):
            return web.json_response({'error': 'unauthorized'}, status=401)
        return await handler(request)

    # ---------- helpers ----------

    def _get_guild_and_player(self, request):
        try:
            guild_id = int(request.match_info['guild_id'])
        except (KeyError, ValueError):
            raise web.HTTPBadRequest(text='bad guild id')
            
        allowed_guild = self._get_allowed_guild_id(request)
        if allowed_guild is not None and allowed_guild != guild_id:
            raise web.HTTPForbidden(text='Forbidden: access to this guild is restricted')
            
        guild = self.bot.get_guild(guild_id)
        if not guild or not self.cog:
            raise web.HTTPNotFound(text='guild not found')
        return guild, self.cog.get_player(guild)


    def _guild_state(self, guild, player):
        vc = guild.voice_client
        connected = bool(vc and vc.is_connected())
        playlist_urls = getattr(player, 'web_playlist_urls', None)
        playlist_started_at = getattr(player, 'web_playlist_started_at', 0)
        if (player.current and playlist_urls and player.current.url not in playlist_urls
                and time.time() - playlist_started_at > 5):
            player.web_playlist_name = None
            player.web_playlist_cover = None
            player.web_playlist_urls = None
        current = None
        if player.current:
            current = _song_json(player.current)
            current['position_seconds'] = round(player.get_playback_position_seconds(), 1)
        return {
            'id': str(guild.id),
            'name': guild.name,
            'icon': str(guild.icon.url) if guild.icon else None,
            'connected': connected,
            'channel': vc.channel.name if connected and vc.channel else None,
            'playing': bool(vc and vc.is_playing()),
            'paused': bool(vc and vc.is_paused()),
            'volume': int(player.volume * 100),
            'loop': 'song' if player.loop else ('queue' if player.loop_queue else 'off'),
            'autoplay': player.autoplay,
            'artist_diversity': player.artist_diversity,
            'vibe_match': player.vibe_match,
            'audio_filter': player.audio_filter or 'off',
            'crossfade_seconds': player.crossfade_seconds,
            'automix': player.automix_enabled,
            'automix_blend_seconds': player.automix_blend_seconds,
            'karaoke': player.karaoke_mode,
            'idle_disconnect_minutes': player.idle_disconnect_seconds // 60,
            'sleep_timer_ends_at': player.sleep_timer_ends_at,
            'is_247': player.is_247_mode,
            'current': current,
            'queue': [_song_json(s) for s in list(player.queue)[:100]],
            'queue_length': len(player.queue),
            'history': [_song_json(s) for s in list(player.history)[:10]],
            'active_playlist': getattr(player, 'web_playlist_name', None),
            'active_playlist_cover': getattr(player, 'web_playlist_cover', None),
        }

    # ---------- routes ----------

    async def index(self, request):
        return web.Response(text=INDEX_HTML, content_type='text/html')

    async def api_session(self, request):
        peer = client_identity(request)
        if not LOGIN_LIMITER.allow(peer):
            return web.json_response({'error': 'too many login attempts'}, status=429)
        try:
            body = await request.json()
        except Exception:
            raise web.HTTPBadRequest(text='invalid json')
        invitation = str(body.get('token') or '')
        if invitation:
            info = _valid_token_info(invitation)
            guild_id = str(body.get('guild_id') or '')
            if not info or str(info.get('guild_id')) != guild_id:
                return web.json_response({'error': 'invalid or expired link'}, status=401)
            response = web.json_response({'ok': True})
            self._set_session_cookie(request, response, invitation)
            return response
        password = str(body.get('password') or '')
        configured = config.WEB_UI_PASSWORD
        if not configured or not secrets.compare_digest(password, configured):
            return web.json_response({'error': 'invalid password'}, status=401)
        token = secrets.token_urlsafe(24)
        temp_tokens[token] = {
            'guild_id': None, 'created_at': time.time(), 'kind': 'session'
        }
        _save_tokens()
        response = web.json_response({'ok': True})
        self._set_session_cookie(request, response, token)
        return response

    async def api_guilds(self, request):
        guilds = []
        allowed_guild_id = self._get_allowed_guild_id(request)
        if self.cog:
            for guild in sorted(self.bot.guilds, key=lambda g: g.name.lower()):
                if allowed_guild_id is not None and allowed_guild_id != guild.id:
                    continue
                player = self.cog.players.get(guild.id)
                vc = guild.voice_client
                guilds.append({
                    'id': str(guild.id),
                    'name': guild.name,
                    'icon': str(guild.icon.url) if guild.icon else None,
                    'connected': bool(vc and vc.is_connected()),
                    'playing': bool(vc and vc.is_playing()),
                    'title': player.current.title if player and player.current else None,
                })
        return web.json_response({
            'ready': self.bot.is_ready(),
            'guilds': guilds,
            'auth_required': bool(config.WEB_UI_PASSWORD) and allowed_guild_id is None
        })


    async def api_guild_state(self, request):
        guild, player = self._get_guild_and_player(request)
        return web.json_response(self._guild_state(guild, player))

    async def api_action(self, request):
        guild, player = self._get_guild_and_player(request)
        try:
            body = await request.json()
        except Exception:
            raise web.HTTPBadRequest(text='invalid json')
        action = body.get('action')
        vc = guild.voice_client
        cog = self.cog

        try:
            if action == 'pause':
                if vc and vc.is_playing():
                    player.mark_paused()
                    vc.pause()
            elif action == 'resume':
                if vc and vc.is_paused():
                    player.mark_resumed()
                    vc.resume()
            elif action == 'skip':
                player.loop = False
                if vc:
                    vc.stop()
            elif action == 'previous':
                history = list(player.history)
                previous = None
                if history:
                    if player.current and history[0].url == player.current.url:
                        previous = history[1] if len(history) > 1 else history[0]
                    else:
                        previous = history[0]
                if previous:
                    player.loop = False
                    player.queue.appendleft(previous)
                    player.clear_preloads()
                    if vc and (vc.is_playing() or vc.is_paused()):
                        vc.stop()
                    elif vc:
                        await player.play_next()
            elif action == 'stop':
                for stopper in ('_stop_now_playing_task', '_stop_lyricsnow_task'):
                    fn = getattr(cog, stopper, None)
                    if fn:
                        fn(guild.id)
                player.queue.clear()
                player.current = None
                player.loop = False
                player.loop_queue = False
                player.pending_playlist = None
                player.web_playlist_name = None
                player.web_playlist_cover = None
                player.web_playlist_urls = None
                player.clear_preloads()
                player.cancel_autoplay_prefetch()
                player.reset_playback_clock()
                if vc:
                    vc.stop()
            elif action == 'shuffle':
                import random
                queue_list = list(player.queue)
                random.shuffle(queue_list)
                from collections import deque
                player.queue = deque(queue_list)
                player.clear_preloads()
                asyncio.create_task(player.preload_next_song())
            elif action == 'clear':
                player.queue.clear()
                player.clear_preloads()
                player.schedule_idle_disconnect()
            elif action == 'volume':
                level = max(0, min(100, int(body.get('level', 50))))
                player.volume = level / 100
                if vc and vc.source:
                    vc.source.volume = player.volume
                cog.save_player_settings(player)
            elif action == 'loop':
                mode = body.get('mode', 'off')
                player.loop = mode == 'song'
                player.loop_queue = mode == 'queue'
                cog.save_player_settings(player)
            elif action == 'autoplay':
                player.autoplay = not player.autoplay
                if player.autoplay:
                    player.schedule_autoplay_prefetch()
                else:
                    player.cancel_autoplay_prefetch()
                cog.save_player_settings(player)
            elif action == 'artist_diversity':
                cog.set_artist_diversity(player, not player.artist_diversity)
            elif action == 'vibe_match':
                player.vibe_match = not player.vibe_match
                cog.save_player_settings(player)
            elif action == 'filter':
                preset = body.get('preset', 'off')
                if preset not in ('off', 'bassboost', 'nightcore', 'slowed', '8d', 'karaoke'):
                    return web.json_response({'error': 'invalid filter preset'}, status=400)
                player.audio_filter = None if preset == 'off' else preset
                if player.karaoke_mode and preset != 'karaoke':
                    player.karaoke_mode = False
                    cog._stop_lyricsnow_task(guild.id)
                player.clear_preloads()
                cog.save_player_settings(player)
                if vc and (vc.is_playing() or vc.is_paused()) and player.current:
                    await player.seek_to(int(player.get_playback_position_seconds()))
            elif action == 'crossfade':
                seconds = max(0, min(10, int(body.get('seconds', 0))))
                player.crossfade_seconds = seconds
                player.clear_preloads()
                cog.save_player_settings(player)
            elif action == 'automix':
                player.automix_enabled = not player.automix_enabled
                player.clear_preloads()
                if player.automix_enabled:
                    player.schedule_automix()
                else:
                    player.cancel_automix()
                cog.save_player_settings(player)
            elif action == 'automix_blend':
                seconds = max(4, min(15, int(body.get('seconds', 8))))
                player.automix_blend_seconds = seconds
                cog.save_player_settings(player)
            elif action == 'karaoke':
                player.karaoke_mode = not player.karaoke_mode
                if player.karaoke_mode:
                    player.audio_filter = 'karaoke'
                else:
                    if player.audio_filter == 'karaoke':
                        player.audio_filter = None
                    cog._stop_lyricsnow_task(guild.id)
                player.clear_preloads()
                cog.save_player_settings(player)
                if vc and (vc.is_playing() or vc.is_paused()) and player.current:
                    await player.seek_to(int(player.get_playback_position_seconds()))
                    if player.karaoke_mode and player.last_message_channel:
                        await cog._start_lyricsnow_for_current(player)
            elif action == 'idle_disconnect':
                minutes = max(0, min(60, int(body.get('minutes', 5))))
                player.idle_disconnect_seconds = minutes * 60
                player.cancel_idle_disconnect()
                if minutes:
                    player.schedule_idle_disconnect()
                cog.save_player_settings(player)
            elif action == 'sleep_timer':
                minutes = max(0, min(480, int(body.get('minutes', 0))))
                task = player.sleep_timer_task
                if task and not task.done():
                    task.cancel()
                player.sleep_timer_task = None
                player.sleep_timer_ends_at = None
                if minutes:
                    if not vc or not vc.is_connected():
                        return web.json_response(
                            {'error': 'Bot must be in voice to start a sleep timer'}, status=409
                        )
                    player.sleep_timer_ends_at = time.time() + minutes * 60
                    player.sleep_timer_task = asyncio.create_task(
                        cog._run_sleep_timer(player, minutes)
                    )
            elif action == 'remove':
                index = int(body.get('index', -1))
                queue_list = list(player.queue)
                if 0 <= index < len(queue_list):
                    queue_list.pop(index)
                    from collections import deque
                    player.queue = deque(queue_list)
            elif action == 'top':
                index = int(body.get('index', -1))
                queue_list = list(player.queue)
                if 0 <= index < len(queue_list):
                    song = queue_list.pop(index)
                    queue_list.insert(0, song)
                    from collections import deque
                    player.queue = deque(queue_list)
                    player.clear_preloads()
            elif action == 'skipto':
                index = int(body.get('index', -1))
                if 0 <= index < len(player.queue) and vc:
                    for _ in range(index):
                        player.queue.popleft()
                    player.loop = False
                    vc.stop()  # after_playing advances to the new queue head
            elif action == 'seek':
                seconds = body.get('seconds')
                if seconds is not None:
                    try:
                        seconds = int(seconds)
                        success = await player.seek_to(seconds)
                        if not success:
                            return web.json_response({'error': 'Seeking failed (only cached/downloaded songs support seeking)'}, status=400)
                    except Exception as e:
                        logger.error(f"Failed to seek: {e}")
                        return web.json_response({'error': str(e)}, status=400)
            else:
                return web.json_response({'error': f'unknown action {action!r}'}, status=400)
        except Exception as e:
            logger.error(f"Web action {action!r} failed for {guild.name}: {e}", exc_info=True)
            return web.json_response({'error': str(e)}, status=500)

        return web.json_response(self._guild_state(guild, player))

    async def api_play(self, request):
        guild, player = self._get_guild_and_player(request)
        cog = self.cog
        try:
            body = await request.json()
        except Exception:
            raise web.HTTPBadRequest(text='invalid json')
        query = (body.get('query') or '').strip()
        if not query:
            return web.json_response({'error': 'empty query'}, status=400)

        vc = guild.voice_client
        if not vc or not vc.is_connected():
            return web.json_response(
                {'error': 'Bot is not in a voice channel. Use /join in Discord first.'},
                status=409,
            )

        requester = guild.me
        songs = []
        try:
            if 'spotify.com' in query or query.startswith('spotify:'):
                if 'playlist' in query or 'album' in query:
                    songs, _total = await cog.process_spotify_playlist_fast(query, requester)
                else:
                    songs = await cog.process_spotify(query, requester)
            elif 'list=' in query:
                songs, _total = await cog.process_youtube_playlist_fast(query, requester)
            else:
                song = await cog.process_youtube(query, requester)
                if song:
                    songs = [song]
        except Exception as e:
            logger.error(f"Web play failed for {query!r}: {e}", exc_info=True)
            return web.json_response({'error': str(e)}, status=500)

        if not songs:
            return web.json_response({'error': 'No results found for that query.'}, status=404)

        player.web_playlist_name = None
        player.web_playlist_cover = None
        player.web_playlist_urls = None

        if body.get('next'):
            for song in reversed(songs):
                player.queue.appendleft(song)
        else:
            for song in songs:
                player.queue.append(song)

        started = False
        if not vc.is_playing() and not vc.is_paused():
            await player.play_next()
            started = True

        return web.json_response({
            'added': len(songs),
            'started': started,
            'first_title': songs[0].title,
            'state': self._guild_state(guild, player),
        })

    async def api_lyrics(self, request):
        guild, player = self._get_guild_and_player(request)
        if not player.current:
            return web.json_response({'track': None, 'artist': None, 'lines': []})
            
        song_title = player.current.title
        
        # Check cache
        if song_title in self.lyrics_cache:
            return web.json_response(self.lyrics_cache[song_title])
            
        # Fetch synced lyrics
        cog = self.cog
        search_query = song_title
        cleaned_query = cog._clean_lyrics_query(search_query) if hasattr(cog, '_clean_lyrics_query') else search_query
        candidates = [search_query]
        if cleaned_query and cleaned_query.lower() != search_query.lower():
            candidates.append(cleaned_query)
            
        lyrics_data = None
        for candidate in candidates:
            if hasattr(cog, '_fetch_synced_lyrics'):
                lyrics_data = await cog._fetch_synced_lyrics(candidate)
                if lyrics_data:
                    break
                    
        if not lyrics_data:
            # Store empty result in cache to avoid spamming lrclib for unfound tracks
            self.lyrics_cache[song_title] = {'track': song_title, 'artist': 'Unknown', 'lines': []}
            return web.json_response(self.lyrics_cache[song_title])
            
        # Cache and return
        res = {
            'track': lyrics_data.get('track'),
            'artist': lyrics_data.get('artist'),
            'lines': lyrics_data.get('lines') # list of [timestamp, text]
        }
        self.lyrics_cache[song_title] = res
        return web.json_response(res)

    async def api_autocomplete(self, request):
        query = request.query.get('q', '').strip()
        suggestions = await asyncio.get_event_loop().run_in_executor(None, get_autocomplete_suggestions, query)
        return web.json_response(suggestions)

    def _read_playlist_meta(self):
        return load_json(PLAYLIST_META_FILE, {}, logger)

    def _write_playlist_meta(self, data):
        save_json(PLAYLIST_META_FILE, data, logger)

    def _playlist_json(self, guild_id):
        playlists = self.cog._read_playlists().get(str(guild_id), {})
        meta = self._read_playlist_meta().get(str(guild_id), {})
        def track_json(entry):
            return {
                'title': entry.get('title', 'Unknown'),
                'duration': entry.get('duration', 'Unknown'),
                'thumbnail': _thumbnail_from_url(entry.get('url'), entry.get('thumbnail')),
                'source_type': entry.get('source_type', 'youtube'),
            }
        return [
            {
                'name': name,
                'count': len(entries),
                'cover': meta.get(name) or next(
                    (_thumbnail_from_url(entry.get('url'), entry.get('thumbnail')) for entry in entries
                     if _thumbnail_from_url(entry.get('url'), entry.get('thumbnail'))), None),
                'custom_cover': bool(meta.get(name)),
                'tracks': [track_json(entry) for entry in entries[:200]],
            }
            for name, entries in sorted(playlists.items(), key=lambda item: item[0].lower())
        ]

    async def api_playlists(self, request):
        guild, _player = self._get_guild_and_player(request)
        return web.json_response({'playlists': self._playlist_json(guild.id)})

    async def api_playlist_action(self, request):
        guild, player = self._get_guild_and_player(request)
        try:
            body = await request.json()
        except Exception:
            raise web.HTTPBadRequest(text='invalid json')

        action = body.get('action')
        name = (body.get('name') or '').strip()[:50]
        if not name:
            return web.json_response({'error': 'Give the playlist a name.'}, status=400)

        data = self.cog._read_playlists()
        guild_lists = data.setdefault(str(guild.id), {})

        if action == 'create':
            if name in guild_lists:
                return web.json_response({'error': f'A playlist named {name} already exists.'}, status=409)
            guild_lists[name] = []
            self.cog._write_playlists(data)
            message = f'Created {name}.'
        elif action == 'save':
            songs = ([player.current] if player.current else []) + list(player.queue)
            songs = [song for song in songs if song.source_type != 'local'][:200]
            if not songs:
                return web.json_response({'error': 'There is nothing in the player to save yet.'}, status=409)
            guild_lists[name] = [
                {
                    'title': song.title,
                    'url': song.url,
                    'duration': song.duration,
                    'source_type': song.source_type,
                    'thumbnail': _thumbnail_from_url(song.url, song.thumbnail),
                }
                for song in songs
            ]
            self.cog._write_playlists(data)
            message = f'Saved {name} with {len(songs)} songs.'
        elif action == 'delete':
            if name not in guild_lists:
                return web.json_response({'error': f'No playlist named {name}.'}, status=404)
            del guild_lists[name]
            self.cog._write_playlists(data)
            meta_data = self._read_playlist_meta()
            guild_meta = meta_data.get(str(guild.id), {})
            cover_url = guild_meta.pop(name, None)
            if cover_url:
                try:
                    os.remove(os.path.join(PLAYLIST_COVERS_DIR, os.path.basename(cover_url)))
                except FileNotFoundError:
                    pass
                self._write_playlist_meta(meta_data)
            message = f'Deleted {name}.'
        elif action == 'add_song':
            entries = guild_lists.get(name)
            if entries is None:
                return web.json_response({'error': f'No playlist named {name}.'}, status=404)
            query = (body.get('query') or '').strip()
            if not query:
                return web.json_response({'error': 'Search for a song first.'}, status=400)
            try:
                if 'spotify.com' in query or query.startswith('spotify:'):
                    if 'playlist' in query or 'album' in query:
                        songs, _total = await self.cog.process_spotify_playlist_fast(query, guild.me)
                    else:
                        songs = await self.cog.process_spotify(query, guild.me)
                elif 'list=' in query:
                    songs, _total = await self.cog.process_youtube_playlist_fast(query, guild.me)
                else:
                    song = await self.cog.process_youtube(query, guild.me)
                    songs = [song] if song else []
            except Exception as error:
                logger.error(f"Playlist search failed for {query!r}: {error}", exc_info=True)
                return web.json_response({'error': str(error)}, status=500)
            if not songs:
                return web.json_response({'error': 'No songs found.'}, status=404)
            room = max(0, 200 - len(entries))
            songs = songs[:room]
            if not songs:
                return web.json_response({'error': 'This playlist already has 200 songs.'}, status=409)
            entries.extend({
                'title': song.title,
                'url': song.url,
                'duration': song.duration,
                'source_type': song.source_type,
                'thumbnail': _thumbnail_from_url(song.url, song.thumbnail),
            } for song in songs)
            self.cog._write_playlists(data)
            message = f'Added {len(songs)} song' + ('' if len(songs) == 1 else 's') + f' to {name}.'
        elif action == 'remove_song':
            entries = guild_lists.get(name)
            if entries is None:
                return web.json_response({'error': f'No playlist named {name}.'}, status=404)
            index = int(body.get('index', -1))
            if not 0 <= index < len(entries):
                return web.json_response({'error': 'That song is no longer in the playlist.'}, status=404)
            removed = entries.pop(index)
            self.cog._write_playlists(data)
            message = f"Removed {removed.get('title', 'song')} from {name}."
        elif action in ('load', 'play'):
            entries = guild_lists.get(name)
            if entries is None:
                return web.json_response({'error': f'No playlist named {name}.'}, status=404)
            vc = guild.voice_client
            if not vc or not vc.is_connected():
                return web.json_response(
                    {'error': 'Bot is not in a voice channel. Use /join in Discord first.'},
                    status=409,
                )
            if action == 'play':
                player.queue.clear()
                player.loop = False
                player.loop_queue = False
                player.pending_playlist = None
                player.clear_preloads()
                playlist_info = next((item for item in self._playlist_json(guild.id) if item['name'] == name), None)
                player.web_playlist_name = name
                player.web_playlist_cover = playlist_info.get('cover') if playlist_info else None
                player.web_playlist_urls = {entry.get('url') for entry in entries}
                player.web_playlist_started_at = time.time()
            for entry in entries:
                player.queue.append(Song(
                    title=entry.get('title', 'Unknown'),
                    url=entry['url'],
                    duration=entry.get('duration', 'Unknown'),
                    requester=guild.me,
                    source_type=entry.get('source_type', 'youtube'),
                    thumbnail=entry.get('thumbnail'),
                ))
            if action == 'play' and (vc.is_playing() or vc.is_paused()):
                vc.stop()
            elif not vc.is_playing() and not vc.is_paused():
                await player.play_next()
            message = ('Playing' if action == 'play' else 'Added') + f' {name} ({len(entries)} songs).'
        else:
            return web.json_response({'error': f'unknown playlist action {action!r}'}, status=400)

        return web.json_response({
            'message': message,
            'playlists': self._playlist_json(guild.id),
            'state': self._guild_state(guild, player),
        })

    async def api_playlist_cover(self, request):
        guild, _player = self._get_guild_and_player(request)
        reader = await request.multipart()
        name = ''
        image = b''
        content_type = ''
        async for part in reader:
            if part.name == 'name':
                name = (await part.text()).strip()[:50]
            elif part.name == 'cover':
                content_type = part.headers.get('Content-Type', '')
                image = await part.read(decode=False)
        if not name or name not in self.cog._read_playlists().get(str(guild.id), {}):
            return web.json_response({'error': 'Playlist not found.'}, status=404)
        if not image or len(image) > 4 * 1024 * 1024:
            return web.json_response({'error': 'Choose an image smaller than 4 MB.'}, status=400)
        signatures = {
            'png': image.startswith(b'\x89PNG\r\n\x1a\n'),
            'jpg': image.startswith(b'\xff\xd8\xff'),
            'webp': image.startswith(b'RIFF') and image[8:12] == b'WEBP',
        }
        extension = next((ext for ext, matches in signatures.items() if matches), None)
        if not extension or not content_type.startswith('image/'):
            return web.json_response({'error': 'Use a PNG, JPEG, or WebP image.'}, status=400)
        os.makedirs(PLAYLIST_COVERS_DIR, exist_ok=True)
        filename = hashlib.sha256(f'{guild.id}:{name}'.encode()).hexdigest()[:28] + '.' + extension
        with open(os.path.join(PLAYLIST_COVERS_DIR, filename), 'wb') as file:
            file.write(image)
        meta_data = self._read_playlist_meta()
        guild_meta = meta_data.setdefault(str(guild.id), {})
        old_cover = guild_meta.get(name)
        guild_meta[name] = '/playlist-covers/' + filename
        self._write_playlist_meta(meta_data)
        if old_cover and old_cover != guild_meta[name]:
            try:
                os.remove(os.path.join(PLAYLIST_COVERS_DIR, os.path.basename(old_cover)))
            except FileNotFoundError:
                pass
        return web.json_response({'playlists': self._playlist_json(guild.id)})



async def start_web_server(bot):
    if not config.WEB_UI_ENABLED:
        logger.info("Web UI disabled (WEB_UI_ENABLED=0)")
        return
    if config.WEB_UI_HOST not in ('127.0.0.1', '::1', 'localhost') and \
            not config.WEB_UI_PASSWORD:
        raise RuntimeError(
            'WEB_UI_PASSWORD is required when WEB_UI_HOST is not loopback')

    ui = WebUI(bot)
    app = web.Application(middlewares=[ui.auth_middleware], client_max_size=6 * 1024 ** 2)
    app.router.add_get('/', ui.index)
    app.router.add_post('/api/session', ui.api_session)
    app.router.add_get('/api/guilds', ui.api_guilds)
    app.router.add_get('/api/guilds/{guild_id}', ui.api_guild_state)
    app.router.add_post('/api/guilds/{guild_id}/action', ui.api_action)
    app.router.add_post('/api/guilds/{guild_id}/play', ui.api_play)
    app.router.add_get('/api/guilds/{guild_id}/lyrics', ui.api_lyrics)
    app.router.add_get('/api/guilds/{guild_id}/playlists', ui.api_playlists)
    app.router.add_post('/api/guilds/{guild_id}/playlists', ui.api_playlist_action)
    app.router.add_post('/api/guilds/{guild_id}/playlist-cover', ui.api_playlist_cover)
    app.router.add_get('/api/autocomplete', ui.api_autocomplete)
    os.makedirs(PLAYLIST_COVERS_DIR, exist_ok=True)
    app.router.add_static('/playlist-covers/', PLAYLIST_COVERS_DIR)

    try:
        import watchtogether
        watchtogether.setup(app, bot)
    except Exception as e:
        logger.error(f"Watch Together failed to mount (dashboard still works): {e}", exc_info=True)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, config.WEB_UI_HOST, config.WEB_UI_PORT)
    await site.start()
    protected = "password-protected" if config.WEB_UI_PASSWORD else "no password set"
    logger.info(f"🌐 Web UI running on http://{config.WEB_UI_HOST}:{config.WEB_UI_PORT} ({protected})")


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kivi Yesili</title>
<style>
  :root {
    --bg: #12121a; --panel: #1c1c28; --panel2: #24243a; --text: #e8e8f0;
    --muted: #9a9ab0; --accent: #7c6cf0; --accent2: #4ec9a0; --danger: #e06c75;
    --border: #333350; --border2: #2a2a40; --card-border: transparent;
    --bg-image: none; --radius: 12px;
  }
  body[data-theme="light"] {
    --bg: #eef0f5; --panel: #ffffff; --panel2: #e7e9f2; --text: #23233a;
    --muted: #6b6b80; --accent: #6a5ae0; --accent2: #1f9e77; --danger: #c94f4f;
    --border: #d5d8e6; --border2: #e6e8f2; --card-border: #e0e2ee;
  }
  body[data-theme="modern"] {
    --bg: #07090f; --panel: #10161f; --panel2: #1a2230; --text: #e6edf5;
    --muted: #8b98ab; --accent: #38bdf8; --accent2: #4ade80; --danger: #f87171;
    --border: #243044; --border2: #1c2636; --card-border: #ffffff12; --radius: 16px;
    --bg-image: radial-gradient(900px 500px at 15% -10%, rgba(56,189,248,.13), transparent 60%),
                radial-gradient(800px 500px at 100% 0%, rgba(124,108,240,.10), transparent 60%);
  }
  body[data-theme="vinyl-modern"] {
    --bg: #161210; --panel: #211a15; --panel2: #2c231b; --text: #f2e9dd;
    --muted: #a89a88; --accent: #e8843c; --accent2: #d4b06a; --danger: #d05f4a;
    --border: #3a2f24; --border2: #332920; --card-border: #3a2f24; --radius: 14px;
  }
  body[data-theme="vinyl-classic"] {
    --bg: #241708; --panel: #3b2713; --panel2: #4a3118; --text: #f4e6c8;
    --muted: #c2a878; --accent: #c9932a; --accent2: #8fae5d; --danger: #c05436;
    --border: #5a3d1e; --border2: #4d341a; --card-border: #5a3d1e;
    --bg-image: repeating-linear-gradient(90deg, rgba(0,0,0,.10) 0px, rgba(0,0,0,.10) 2px, transparent 2px, transparent 7px),
                linear-gradient(180deg, #2b1b0b, #201306);
  }
  body[data-theme="glass"], body[data-theme="vinyl-glass"] {
    --bg: #090a0e; --panel: rgba(23,24,30,.58); --panel2: rgba(255,255,255,.09);
    --text: #f8f8fb; --muted: #b8bac5; --accent: #a887ff; --accent2: #89f2d0;
    --danger: #ff8290; --border: rgba(255,255,255,.16); --border2: rgba(255,255,255,.09);
    --card-border: rgba(255,255,255,.14); --radius: 22px;
    --bg-image: radial-gradient(900px 600px at 15% 5%, rgba(145,92,255,.24), transparent 65%),
                radial-gradient(900px 650px at 92% 18%, rgba(55,195,203,.20), transparent 64%),
                linear-gradient(145deg, #090a10, #121018 55%, #07090d);
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); background-image: var(--bg-image);
         background-attachment: fixed; color: var(--text); min-height: 100vh;
         font-family: system-ui, -apple-system, 'Segoe UI', sans-serif; }
  body[data-theme="vinyl-classic"] { font-family: Georgia, 'Palatino Linotype', 'Times New Roman', serif; }
  button, input, select { font-family: inherit; }
  .wrap { max-width: 1180px; margin: 0 auto; padding: 16px 16px 136px; }
  h1 { font-size: 20px; margin: 8px 0 16px; display: flex; align-items: center; gap: 10px; }
  h1 .dot { width: 10px; height: 10px; border-radius: 50%; background: var(--danger); }
  h1 .dot.on { background: var(--accent2); }
  h1 select { margin-left: auto; background: var(--panel); color: var(--text);
              border: 1px solid var(--border); border-radius: 8px; padding: 6px 8px;
              font-size: 13px; cursor: pointer; }
  h1 .settings-toggle { white-space: nowrap; padding: 7px 11px; font-size: 13px; }
  .tabs { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 16px; }
  .tab { background: var(--panel); border: 1px solid transparent; color: var(--text);
         padding: 8px 14px; border-radius: 999px; cursor: pointer; font-size: 14px;
         display: flex; align-items: center; gap: 8px; }
  .tab img { width: 20px; height: 20px; border-radius: 50%; }
  .tab.active { border-color: var(--accent); background: var(--panel2); }
  .tab .live { color: var(--accent2); font-size: 11px; }
  .card { background: var(--panel); border: 1px solid var(--card-border);
          border-radius: var(--radius); padding: 16px; margin-bottom: 14px; }
  body[data-theme="light"] .card { box-shadow: 0 1px 3px rgba(30,30,60,.08); }
  body[data-theme="vinyl-classic"] .card { box-shadow: inset 0 1px 0 rgba(255,235,200,.05), 0 2px 8px rgba(0,0,0,.4); }
  .np { display: flex; gap: 16px; align-items: center; }
  .np img { width: 110px; height: 82px; object-fit: cover; border-radius: 8px; background: var(--panel2); }
  .np .title { font-size: 17px; font-weight: 600; margin-bottom: 4px; }
  .np .sub { color: var(--muted); font-size: 13px; }
  .bar { height: 6px; background: var(--panel2); border-radius: 3px; margin: 10px 0 4px; overflow: hidden; cursor: pointer; }
  .bar > div { height: 100%; background: var(--accent); border-radius: 3px; width: 0%; transition: width .5s linear; }
  .times { display: flex; justify-content: space-between; color: var(--muted); font-size: 12px; }
  .controls { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-top: 12px; }
  button { background: var(--panel2); color: var(--text); border: none; border-radius: 8px;
           padding: 9px 14px; cursor: pointer; font-size: 14px; }
  button:hover { filter: brightness(1.2); }
  button.primary { background: var(--accent); }
  body[data-theme="light"] button.primary { color: #fff; }
  body[data-theme="modern"] button:not(.primary):not(.danger) { border: 1px solid var(--border); }
  body[data-theme="modern"] button.primary { background: linear-gradient(135deg, #38bdf8, #6366f1); color: #fff; }
  button.danger { background: transparent; color: var(--danger); border: 1px solid var(--danger); }
  button.toggled { outline: 2px solid var(--accent2); }
  .vol { display: flex; align-items: center; gap: 8px; margin-left: auto; color: var(--muted); font-size: 13px; }
  input[type=range] { accent-color: var(--accent); width: 120px; }
  .addrow { display: flex; gap: 8px; }
  .addrow input[type=text] { flex: 1; background: var(--panel2); border: 1px solid var(--border);
      color: var(--text); border-radius: 8px; padding: 10px 12px; font-size: 14px; }
  .addrow input[type=text]:focus { outline: 1px solid var(--accent); }
  .qhead { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
  .qhead h2 { font-size: 15px; margin: 0; }
  .section-actions { display:flex; align-items:center; gap:5px; }
  .section-actions button { padding:6px 9px; font-size:12px; }
  .setting { display: flex; align-items: center; gap: 14px; padding: 10px 0;
             border-bottom: 1px solid var(--border2); }
  .setting:last-child { border-bottom: none; }
  .setting .setting-text { flex: 1; min-width: 0; }
  .setting .setting-title { font-size: 14px; font-weight: 600; }
  .setting .setting-desc { color: var(--muted); font-size: 12px; margin-top: 3px; }
  .setting button { min-width: 82px; }
  .setting select { min-width: 110px; max-width: 170px; background: var(--panel2);
                    color: var(--text); border: 1px solid var(--border);
                    border-radius: 8px; padding: 8px; }
  .qitem { display: flex; align-items: center; gap: 10px; padding: 8px 6px;
           border-bottom: 1px solid var(--border2); font-size: 14px; }
  .qitem:last-child { border-bottom: none; }
  .qitem .n { color: var(--muted); min-width: 22px; text-align: right; }
  .qitem .t { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .qitem .d { color: var(--muted); font-size: 12px; }
  .qitem .btns { display: flex; gap: 4px; }
  .qitem .btns button { padding: 4px 8px; font-size: 12px; }
  .empty { color: var(--muted); text-align: center; padding: 18px 0; font-size: 14px; }
  .msg { position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%);
         background: var(--panel2); border: 1px solid var(--accent); padding: 10px 18px;
         border-radius: 10px; font-size: 14px; opacity: 0; transition: opacity .3s; pointer-events: none; }
  .msg.show { opacity: 1; }
  #login { position: fixed; inset: 0; background: rgba(10,10,16,.92); display: none;
           align-items: center; justify-content: center; z-index: 10; }
  #login .box { background: var(--panel); padding: 24px; border-radius: var(--radius); width: 300px; }
  #login input { width: 100%; margin: 12px 0; background: var(--panel2); border: 1px solid var(--border);
                 color: var(--text); border-radius: 8px; padding: 10px; }
  details summary { cursor: pointer; color: var(--muted); font-size: 14px; }
  .lyric-line {
    padding: 8px 0;
    font-size: 15px;
    color: var(--muted);
    transition: all 0.3s ease;
    text-align: center;
  }
  .lyric-line.active {
    color: var(--accent2);
    font-weight: bold;
    font-size: 18px;
    transform: scale(1.05);
  }
  body[data-theme="glass"]::before, body[data-theme="vinyl-glass"]::before {
    content: ''; position: fixed; inset: -50px; z-index: -1; pointer-events: none;
    background-image: linear-gradient(rgba(8,9,13,.55), rgba(8,9,13,.82)), var(--ambient-art, none);
    background-size: cover; background-position: center; filter: blur(38px) saturate(1.35);
    transform: scale(1.08); opacity: .82;
  }
  body[data-theme="glass"] .card, body[data-theme="vinyl-glass"] .card,
  body[data-theme="glass"] h1 select, body[data-theme="vinyl-glass"] h1 select,
  body[data-theme="glass"] .tab, body[data-theme="vinyl-glass"] .tab {
    background: linear-gradient(145deg, rgba(255,255,255,.12), rgba(255,255,255,.045));
    border: 1px solid rgba(255,255,255,.16);
    box-shadow: inset 0 1px 0 rgba(255,255,255,.18), 0 18px 50px rgba(0,0,0,.28);
    -webkit-backdrop-filter: blur(26px) saturate(1.35); backdrop-filter: blur(26px) saturate(1.35);
  }
  .playlist-grid { display: grid; grid-template-columns: repeat(auto-fill,minmax(190px,1fr)); gap: 14px; }
  .playlist-card { min-width: 0; padding: 12px; border-radius: 18px; background: var(--panel2);
                   border: 1px solid var(--border2); transition: transform .2s, border-color .2s; }
  .playlist-card:hover { transform: translateY(-3px); border-color: var(--accent); }
  .playlist-disc { position: relative; width: 100%; aspect-ratio: 1; border-radius: 12px; overflow: hidden;
                   background: linear-gradient(145deg,#282834,#111117);
                   box-shadow: 0 14px 26px rgba(0,0,0,.38), inset 0 0 0 2px rgba(255,255,255,.08); }
  .playlist-disc .cover { position: absolute; inset: 0; background-size: cover;
                          background-position: center; }
  .playlist-card.no-cover .playlist-disc::after { content:'♫'; position:absolute; inset:0; display:grid; place-items:center;
                                                    font-size:54px; color:var(--muted); }
  .playlist-name { margin-top: 12px; font-weight: 700; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .playlist-meta { color: var(--muted); font-size: 12px; margin: 4px 0 10px; }
  .playlist-actions { display: grid; grid-template-columns: 1fr 1fr auto; gap: 6px; }
  .playlist-actions button { padding: 7px 8px; font-size: 12px; }
  .playlist-create { display:flex; gap:8px; margin-bottom:14px; }
  .playlist-create input { flex:1; min-width:0; background:var(--panel2); color:var(--text);
                           border:1px solid var(--border); border-radius:10px; padding:10px 12px; }
  .view-nav { display:flex; gap:8px; margin:0 0 16px; }
  .view-nav button { border-radius:999px; padding:9px 18px; border:1px solid var(--border); }
  .view-nav button.active { background:var(--text); color:var(--bg); font-weight:750; }
  .library-head { display:flex; align-items:flex-end; justify-content:space-between; gap:18px; margin-bottom:20px; }
  .library-head h2 { font-size:30px; margin:0 0 4px; }
  .library-head p { color:var(--muted); margin:0; }
  .library-create { display:flex; gap:8px; width:min(420px,100%); }
  .library-create input, .playlist-add input { flex:1; min-width:0; background:var(--panel2); color:var(--text);
    border:1px solid var(--border); border-radius:999px; padding:11px 16px; }
  .playlist-card { cursor:pointer; }
  .playlist-card .playlist-actions { opacity:0; transition:opacity .18s; }
  .playlist-card:hover .playlist-actions, .playlist-card:focus-within .playlist-actions { opacity:1; }
  .playlist-page { overflow:hidden; padding:0; }
  .playlist-hero { min-height:310px; display:flex; align-items:flex-end; gap:26px; padding:34px;
    background:linear-gradient(180deg,rgba(255,255,255,.13),rgba(0,0,0,.34)); }
  .playlist-cover-large { position:relative; flex:0 0 230px; width:230px; height:230px; border-radius:12px;
    background:linear-gradient(145deg,#31313d,#111117); background-size:cover; background-position:center;
    box-shadow:0 22px 55px rgba(0,0,0,.5); overflow:hidden; }
  .playlist-cover-large.empty::after { content:'♫'; position:absolute; inset:0; display:grid; place-items:center;
    font-size:74px; color:var(--muted); }
  .cover-upload { position:absolute; inset:auto 12px 12px; z-index:2; display:block; text-align:center; cursor:pointer;
    background:rgba(10,10,14,.76); color:white; border:1px solid rgba(255,255,255,.25); border-radius:999px;
    padding:9px 12px; font-size:12px; backdrop-filter:blur(12px); }
  .cover-upload input { display:none; }
  .playlist-eyebrow { text-transform:uppercase; letter-spacing:.11em; font-size:11px; font-weight:800; }
  .playlist-hero h2 { font-size:clamp(32px,6vw,68px); line-height:.96; margin:10px 0 14px; letter-spacing:-.04em; }
  .playlist-hero .playlist-meta { font-size:14px; }
  .playlist-body { padding:24px 34px 34px; background:linear-gradient(180deg,rgba(0,0,0,.24),transparent 220px); }
  .playlist-toolbar { display:flex; gap:10px; align-items:center; margin-bottom:22px; flex-wrap:wrap; }
  .playlist-play { width:56px; height:56px; padding:0; border-radius:50%; background:#1ed760; color:#07130a;
    font-size:22px; font-weight:900; box-shadow:0 12px 30px rgba(30,215,96,.24); }
  .playlist-add { display:flex; gap:8px; flex:1; min-width:min(100%,340px); }
  .track-head, .playlist-track { display:grid; grid-template-columns:34px minmax(220px,2fr) minmax(120px,1fr) 90px 42px;
    gap:12px; align-items:center; padding:10px 8px; }
  .track-head { color:var(--muted); font-size:12px; border-bottom:1px solid var(--border2); }
  .playlist-track { border-radius:9px; }
  .playlist-track:hover { background:var(--panel2); }
  .track-main { display:flex; align-items:center; gap:11px; min-width:0; }
  .track-art { position:relative; flex:0 0 44px; width:44px; height:44px; display:grid; place-items:center;
               overflow:hidden; border-radius:5px; background:linear-gradient(145deg,var(--panel2),var(--panel)); }
  .track-art img { position:absolute; inset:0; width:100%; height:100%; object-fit:cover; }
  .track-art.missing::after { content:'♫'; color:var(--muted); font-size:17px; }
  .track-title { min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .track-source, .track-duration, .track-number { color:var(--muted); font-size:13px; }
  .controls { position: fixed; z-index: 20; left: 14px; right: 14px; bottom: 12px; margin: 0;
              min-height: 88px; padding: 12px 18px; border: 1px solid var(--border);
              border-radius: 24px; background: color-mix(in srgb, var(--panel) 88%, transparent);
              box-shadow: 0 20px 70px rgba(0,0,0,.55), inset 0 1px 0 rgba(255,255,255,.08);
              -webkit-backdrop-filter: blur(28px) saturate(1.35); backdrop-filter: blur(28px) saturate(1.35); }
  .bottom-track { display:flex; align-items:center; gap:10px; width:min(280px,24vw); min-width:180px; }
  .bottom-track img { width:52px; height:52px; border-radius:12px; object-fit:cover; background:var(--panel2); }
  .bottom-track .bt-copy { min-width:0; }
  .bottom-track .bt-title { font-weight:700; font-size:13px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .bottom-track .bt-sub { color:var(--muted); font-size:11px; margin-top:4px; }
  .transport { display:flex; gap:7px; align-items:center; margin:auto; }
  .transport .play-main { width:46px; height:46px; border-radius:50%; padding:0; font-size:18px;
                          color:#111; background:var(--text); }
  .bottom-progress { position:absolute; height:4px; left:18px; right:18px; bottom:6px; background:var(--panel2); border-radius:5px; cursor:pointer; overflow:hidden; }
  .bottom-progress > div { height:100%; width:0; background:linear-gradient(90deg,var(--accent),var(--accent2)); }
  @media (max-width: 760px) {
    .wrap { padding-left:10px; padding-right:10px; padding-bottom:150px; }
    h1 { flex-wrap:wrap; } h1 select { margin-left:0; flex:1; }
    .controls { left:7px; right:7px; bottom:7px; padding:10px; justify-content:center; }
    .bottom-track { width:100%; min-width:0; } .bottom-track img { width:40px; height:40px; }
    .transport { margin:0; } .controls .vol { margin-left:0; } .controls .vol span { display:none; }
    .controls .vol input { width:76px; } .playlist-grid { grid-template-columns:repeat(2,minmax(0,1fr)); }
    .library-head { align-items:stretch; flex-direction:column; } .library-create { width:100%; }
    .playlist-hero { min-height:0; align-items:center; flex-direction:column; text-align:center; padding:24px 18px; }
    .playlist-cover-large { width:190px; height:190px; flex-basis:190px; }
    .playlist-body { padding:18px 14px 28px; }
    .track-head { display:none; }
    .playlist-track { grid-template-columns:26px minmax(0,1fr) 52px 36px; gap:7px; }
    .playlist-track .track-source { display:none; }
  }

  /* ---------- turntable (vinyl themes) ---------- */
  .deck { position: relative; width: 380px; height: 336px; max-width: 100%;
          margin: 0 auto; user-select: none; -webkit-user-select: none; }
  .plinth { position: absolute; inset: 0 0 20px 0; border-radius: 18px; }
  .platter { position: absolute; left: 22px; top: 28px; width: 264px; height: 264px; border-radius: 50%; }
  .vinyl-wrap { position: absolute; left: 34px; top: 40px; width: 240px; height: 240px; }
  .vinyl { position: absolute; inset: 0; border-radius: 50%; cursor: pointer;
           box-shadow: 0 5px 16px rgba(0,0,0,.55); }
  /* Grooves are concentric (symmetric), so a rotating conic glint + a couple of
     off-centre specks are what actually make the spin visible to the eye. */
  .disc { position: absolute; inset: 0; border-radius: 50%; will-change: transform;
          background:
            conic-gradient(from 0deg, rgba(255,255,255,.11), transparent 22%, transparent 48%,
              rgba(255,255,255,.06) 60%, transparent 78%, rgba(255,255,255,.11) 100%),
            repeating-radial-gradient(circle at 50% 50%, #101010 0px, #1b1b1b 1px, #0e0e0e 2px, #161616 3px);
          box-shadow: inset 0 0 0 2px rgba(0,0,0,.8); }
  .disc .label { position: absolute; inset: 33%; border-radius: 50%;
                 background-size: cover; background-position: center;
                 box-shadow: 0 0 0 3px rgba(0,0,0,.75), inset 0 0 10px rgba(0,0,0,.3); }
  /* tiny reference mark near the rim — gives the eye something to track while spinning */
  .disc .mark { position: absolute; left: 50%; top: 7%; width: 5px; height: 5px;
                margin-left: -2.5px; border-radius: 50%; background: rgba(200,200,210,.5); }
  .vinyl .sheen { position: absolute; inset: 0; border-radius: 50%; pointer-events: none;
    background: conic-gradient(from 40deg, transparent 0deg, rgba(255,255,255,.07) 24deg, transparent 60deg,
                transparent 175deg, rgba(255,255,255,.05) 205deg, transparent 245deg); }
  .vinyl .spindle { position: absolute; left: 50%; top: 50%; width: 9px; height: 9px;
    margin: -4.5px 0 0 -4.5px; border-radius: 50%; z-index: 2;
    background: radial-gradient(circle at 35% 30%, #eee, #888); box-shadow: 0 1px 2px rgba(0,0,0,.7); }
  .arm-mount { position: absolute; right: 26px; top: 22px; width: 40px; height: 40px; z-index: 5; }
  .tonearm { position: absolute; left: 50%; top: 50%; width: 12px; height: 204px;
             margin-left: -6px; margin-top: -6px; transform-origin: 6px 6px;
             cursor: grab; touch-action: none; will-change: transform; }
  .tonearm:active { cursor: grabbing; }
  .tonearm::before { content: ''; position: absolute; left: -14px; right: -14px; top: -32px; bottom: -8px; }
  .tonearm .pivot { position: absolute; left: -4px; top: -4px; width: 20px; height: 20px;
                    border-radius: 50%; box-shadow: 0 2px 5px rgba(0,0,0,.5); }
  .tonearm .weight { position: absolute; left: -2px; top: -28px; width: 16px; height: 24px;
                     border-radius: 4px; box-shadow: 0 2px 4px rgba(0,0,0,.4); }
  .tonearm .shaft { position: absolute; left: 4px; top: 12px; width: 4px; height: 156px; border-radius: 2px; }
  .tonearm .head { position: absolute; left: -2px; top: 166px; width: 16px; height: 28px;
                   border-radius: 3px; box-shadow: 0 2px 4px rgba(0,0,0,.4); }
  .tonearm .needle { position: absolute; left: 5px; top: 193px; width: 2px; height: 7px; }
  /* lift shading transitions smoothly; the vertical rise itself is eased in JS */
  .tonearm { transition: filter .28s ease; }
  .tonearm.lifted { filter: brightness(1.16) drop-shadow(0 15px 10px rgba(0,0,0,.55)); }
  .tonearm.lifted .head { box-shadow: 0 16px 12px rgba(0,0,0,.55); }
  .tonearm.lifted .needle { box-shadow: 0 0 6px 1px rgba(255,255,255,.4); }
  .deck-hint { position: absolute; left: 0; right: 0; bottom: 0; text-align: center;
               font-size: 11px; color: var(--muted); min-height: 14px; }
  .vinyl-info { text-align: center; margin-top: 6px; }
  .vinyl-info .title { font-size: 17px; font-weight: 600; margin-bottom: 4px; }
  .vinyl-info .sub { color: var(--muted); font-size: 13px; }
  .vinyl-info .times { justify-content: center; gap: 6px; margin-top: 6px; }
  .vinyl-info .times span:first-child::after { content: ' /'; }
  @media (max-width: 430px) {
    .deck { transform: scale(.8); transform-origin: top center; margin-bottom: -60px; }
  }

  body[data-theme="vinyl-modern"] .plinth {
    background: linear-gradient(180deg, #2b221a, #201913);
    border: 1px solid #3d3126;
    box-shadow: 0 12px 28px rgba(0,0,0,.45), inset 0 1px 0 rgba(255,255,255,.04);
  }
  body[data-theme="vinyl-modern"] .platter {
    background: radial-gradient(circle, #0d0d0f 62%, #23232a 63%, #101014 72%, #17171c 100%);
    box-shadow: 0 4px 12px rgba(0,0,0,.5), inset 0 0 0 1px rgba(255,255,255,.05);
  }
  body[data-theme="vinyl-modern"] .label { background-color: #e8843c; }
  body[data-theme="vinyl-modern"] .pivot { background: radial-gradient(circle at 35% 30%, #d9d9de, #74747c); }
  body[data-theme="vinyl-modern"] .weight { background: linear-gradient(90deg, #46464e, #2c2c33); }
  body[data-theme="vinyl-modern"] .shaft { background: linear-gradient(90deg, #c2c2c9, #8b8b94); }
  body[data-theme="vinyl-modern"] .head { background: #2b2b31; border: 1px solid #56565e; }
  body[data-theme="vinyl-modern"] .needle { background: #e8e8ee; }

  body[data-theme="vinyl-classic"] .plinth {
    background:
      repeating-linear-gradient(94deg, rgba(0,0,0,.13) 0px, rgba(0,0,0,.13) 3px, transparent 3px, transparent 10px),
      linear-gradient(180deg, #6b4423, #4a2c12);
    border: 1px solid #7a5230;
    box-shadow: 0 14px 30px rgba(0,0,0,.55), inset 0 1px 0 rgba(255,220,160,.15);
  }
  body[data-theme="vinyl-classic"] .platter {
    background: radial-gradient(circle at 50% 46%, #3c3c40 57%, #c9c9cf 58%, #7e7e86 62%, #b9b9c0 65%, #55555c 69%, #3a3a40 100%);
    box-shadow: 0 5px 14px rgba(0,0,0,.6), inset 0 0 0 1px rgba(255,255,255,.08);
  }
  body[data-theme="vinyl-classic"] .label { background-color: #8a2a1e; }
  body[data-theme="vinyl-classic"] .pivot { background: radial-gradient(circle at 35% 30%, #f3d98a, #a3781c); }
  body[data-theme="vinyl-classic"] .weight { background: radial-gradient(circle at 40% 30%, #eecf7d, #93701c); }
  body[data-theme="vinyl-classic"] .shaft { background: linear-gradient(90deg, #e6c268, #a9821f); }
  body[data-theme="vinyl-classic"] .head { background: linear-gradient(180deg, #caa14a, #7d6015); }
  body[data-theme="vinyl-classic"] .needle { background: #f0e2b0; }
  body[data-theme="vinyl-glass"] .plinth {
    background: linear-gradient(145deg,rgba(255,255,255,.16),rgba(255,255,255,.045));
    border: 1px solid rgba(255,255,255,.22);
    box-shadow: 0 24px 60px rgba(0,0,0,.45), inset 0 1px 0 rgba(255,255,255,.25);
    -webkit-backdrop-filter: blur(26px); backdrop-filter: blur(26px);
  }
  body[data-theme="vinyl-glass"] .platter { background:radial-gradient(circle,#08090c 60%,#8d91a0 61%,#282a31 68%,#08090c 73%); }
  body[data-theme="vinyl-glass"] .pivot { background:radial-gradient(circle at 35% 30%,#fff,#777b88); }
  body[data-theme="vinyl-glass"] .weight { background:linear-gradient(90deg,#737784,#292b32); }
  body[data-theme="vinyl-glass"] .shaft { background:linear-gradient(90deg,#f5f7ff,#9499a8); }
  body[data-theme="vinyl-glass"] .head { background:rgba(20,22,28,.82); border:1px solid rgba(255,255,255,.3); }
  body[data-theme="vinyl-glass"] .needle { background:#fff; }
  .disc .album-art { position:absolute; inset:0; border-radius:50%; background-size:cover; background-position:center; }
  .disc .label { inset:44%; z-index:1; background:rgba(8,9,12,.42) !important;
                 box-shadow:0 0 0 3px rgba(255,255,255,.2), inset 0 0 8px rgba(0,0,0,.45); }
  .disc::after { content:''; position:absolute; inset:0; border-radius:50%; pointer-events:none;
    background:repeating-radial-gradient(circle,transparent 0 5px,rgba(255,255,255,.055) 6px,rgba(0,0,0,.10) 7px),
               radial-gradient(circle,transparent 0 13%,rgba(0,0,0,.28) 13.5% 16%,transparent 16.5%),
               conic-gradient(from 12deg,rgba(255,255,255,.14),transparent 14%,transparent 48%,rgba(255,255,255,.09),transparent 68%); }
</style>
</head>
<body>
<div class="wrap">
  <h1><span class="dot" id="statusDot"></span>🎵 Kivi Yeşili
    <select id="themeSel" onchange="applyTheme(this.value)" title="Theme">
      <option value="default">🌙 Kivi Dark</option>
      <option value="vinyl-modern">💿 Vinyl · Modern</option>
      <option value="vinyl-classic">🎻 Vinyl · Classic</option>
      <option value="vinyl-glass">🫧 Vinyl · Liquid Glass</option>
      <option value="glass">💎 Liquid Glass</option>
      <option value="modern">✨ Modern</option>
      <option value="light">☀️ Light</option>
    </select>
    <button id="settingsBtn" class="settings-toggle" onclick="toggleSettings()"
            aria-expanded="false" aria-controls="settingsPanel">⚙️ Settings</button>
  </h1>
  <div class="tabs" id="tabs"></div>
  <nav class="view-nav" aria-label="Main views">
    <button id="playerViewBtn" class="active" onclick="showView('player')">▶ Player</button>
    <button id="playlistViewBtn" onclick="showView('playlists')">▦ Playlists</button>
  </nav>
  <div id="content"><div class="empty">Loading…</div></div>
</div>
<div class="msg" id="msg"></div>
<div id="login"><div class="box">
  <b>Password required</b>
  <input type="password" id="pw" placeholder="Web UI password">
  <button class="primary" onclick="savePw()" style="width:100%">Unlock</button>
</div></div>
<script>
const urlParams = new URLSearchParams(window.location.search);
const urlGuild = urlParams.get('guild_id');
const linkToken = new URLSearchParams(window.location.hash.slice(1)).get('token') || '';

if (urlGuild) {
  sessionStorage.setItem('mb_guild', urlGuild);
} else {
  sessionStorage.removeItem('mb_guild');
}

let guilds = [], selected = sessionStorage.getItem('mb_guild') || localStorage.getItem('mb_guild') || null;
let state = null, lastStateAt = 0, playlists = [], playlistsGuild = null;
let activeView = localStorage.getItem('mb_view') || 'player', openPlaylistName = null;
let lyricsData = null, lastLyricsTitle = null, lyricsVisible = localStorage.getItem('mb_lyrics_hidden') !== '1';
let settingsOpen = false; // intentionally closed on every fresh page load
let queueCollapsed = localStorage.getItem('mb_queue_collapsed') === '1';
let historyCollapsed = localStorage.getItem('mb_history_collapsed') === '1';
const DEFAULT_SECTION_ORDER = ['queue', 'history', 'lyrics'];
let sectionOrder;
try {
  const savedOrder = JSON.parse(localStorage.getItem('mb_section_order') || 'null');
  sectionOrder = Array.isArray(savedOrder) && DEFAULT_SECTION_ORDER.every(key => savedOrder.includes(key))
    ? savedOrder.filter(key => DEFAULT_SECTION_ORDER.includes(key)).slice(0, 3) : [...DEFAULT_SECTION_ORDER];
} catch (_) { sectionOrder = [...DEFAULT_SECTION_ORDER]; }

// ---------- themes ----------
const THEMES = ['default', 'vinyl-modern', 'vinyl-classic', 'vinyl-glass', 'glass', 'modern', 'light'];
let theme = localStorage.getItem('mb_theme') || 'default';
function isVinyl() { return theme.startsWith('vinyl-'); }
function applyTheme(t) {
  if (!THEMES.includes(t)) t = 'default';
  theme = t;
  document.body.dataset.theme = t;
  localStorage.setItem('mb_theme', t);
  const sel = document.getElementById('themeSel');
  if (sel && sel.value !== t) sel.value = t;
  if (state) render();
}
applyTheme(theme);

const basePath = window.location.pathname.endsWith('/') ? window.location.pathname : window.location.pathname + '/';

function hdrs() { return {'Content-Type': 'application/json'}; }
async function api(path, opts) {
  const r = await fetch(path, Object.assign({headers: hdrs()}, opts || {}));
  if (r.status === 401) { document.getElementById('login').style.display = 'flex'; throw new Error('auth'); }
  document.getElementById('login').style.display = 'none';
  if (!r.ok) {
    let e = 'Request failed'; try { e = (await r.json()).error || e; } catch(_){}
    throw new Error(e);
  }
  return r.json();
}
async function exchangeLinkToken() {
  if(!linkToken || !urlGuild)return true;
  const r = await fetch(basePath + 'api/session', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({token:linkToken,guild_id:urlGuild})
  });
  history.replaceState(null,'',location.pathname+'?guild_id='+encodeURIComponent(urlGuild));
  if(!r.ok){document.getElementById('login').style.display='flex';return false}
  return true;
}
async function savePw() {
  const password = document.getElementById('pw').value;
  const r = await fetch(basePath + 'api/session', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({password})
  });
  if(!r.ok){ toast('❌ Invalid password'); return; }
  document.getElementById('pw').value = '';
  document.getElementById('login').style.display = 'none';
  refreshGuilds();
}
function toast(text) {
  const el = document.getElementById('msg');
  el.textContent = text; el.classList.add('show');
  clearTimeout(el._t); el._t = setTimeout(() => el.classList.remove('show'), 2500);
}
function esc(s) { const d = document.createElement('div'); d.textContent = s == null ? '' : s; return d.innerHTML; }
function fmt(sec) {
  if (sec == null || isNaN(sec)) return '?:??';
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec/3600), m = Math.floor(sec%3600/60), s = sec%60;
  return (h ? h + ':' + String(m).padStart(2,'0') : m) + ':' + String(s).padStart(2,'0');
}
function cssUrl(url) {
  return encodeURI(String(url || '')).replace(/['()]/g, ch => '%' + ch.charCodeAt(0).toString(16));
}
function mediaUrl(url) {
  const value = String(url || '');
  if (!value || /^(?:data:|blob:|https?:)/i.test(value)) return value;
  return basePath + value.replace(/^\/+/, '');
}

function showView(view) {
  activeView = view === 'playlists' ? 'playlists' : 'player';
  localStorage.setItem('mb_view', activeView);
  if (activeView === 'player') openPlaylistName = null;
  renderViewNav();
  render();
}
function renderViewNav() {
  const playerBtn = document.getElementById('playerViewBtn');
  const playlistBtn = document.getElementById('playlistViewBtn');
  if (playerBtn) playerBtn.classList.toggle('active', activeView === 'player');
  if (playlistBtn) playlistBtn.classList.toggle('active', activeView === 'playlists');
  const settingsBtn = document.getElementById('settingsBtn');
  if (settingsBtn) settingsBtn.style.display = activeView === 'player' ? '' : 'none';
}

function playerControlsHTML(s) {
  const current = s.current;
  return `<div class="controls">
    <div class="bottom-track">${current && current.thumbnail ? `<img src="${esc(mediaUrl(current.thumbnail))}" alt="">` : '<img alt="">'}
      <div class="bt-copy"><div class="bt-title">${current ? esc(current.title) : 'Nothing playing'}</div>
      <div class="bt-sub">${current ? esc(current.requester) : (s.connected ? 'Choose something to play' : 'Join voice in Discord')}</div></div></div>
    <div class="transport">
      <button class="${s.autoplay ? 'toggled' : ''}" onclick="act('autoplay')" title="Autoplay" aria-label="Autoplay">🎲</button>
      <button onclick="act('shuffle')" title="Shuffle" aria-label="Shuffle">🔀</button>
      <button onclick="act('previous')" title="Previous track" aria-label="Previous track">⏮</button>
      <button class="play-main" onclick="act('${s.paused ? 'resume' : 'pause'}')" title="${s.paused ? 'Resume' : 'Pause'}">${s.paused ? '▶' : 'Ⅱ'}</button>
      <button onclick="doSkip()" title="Next track" aria-label="Next track">⏭</button>
      <button class="${s.loop !== 'off' ? 'toggled' : ''}" onclick="cycleLoop()" title="${s.loop === 'song' ? 'Repeat song' : s.loop === 'queue' ? 'Repeat queue' : 'Loop off'}" aria-label="Loop">${s.loop === 'song' ? '🔂' : '🔁'}</button>
    </div>
    <button class="danger" onclick="if(confirm('Stop and clear the queue?'))act('stop')" title="Stop" aria-label="Stop">⏹</button>
    <div class="vol">🔊 <input type="range" min="0" max="100" value="${s.volume}" onchange="act('volume',{level:+this.value})"><span>${s.volume}%</span></div>
    <div class="bottom-progress" onclick="seekProgress(event)"><div id="bottomPbar"></div></div>
  </div>`;
}

function playlistCardsHTML() {
  if (!playlists.length) return '<div class="empty">No playlists yet. Create one, then search for songs to add.</div>';
  return playlists.map((playlist, index) => {
    const cover = playlist.cover ? ` style="background-image:url('${cssUrl(mediaUrl(playlist.cover))}')"` : '';
    return `<article class="playlist-card ${playlist.cover ? '' : 'no-cover'}" tabindex="0" onclick="openPlaylist(${index})" onkeydown="if(event.key==='Enter')openPlaylist(${index})">
      <div class="playlist-disc"><div class="cover"${cover}></div></div>
      <div class="playlist-name">${esc(playlist.name)}</div>
      <div class="playlist-meta">${playlist.count} song${playlist.count === 1 ? '' : 's'}</div>
      <div class="playlist-actions">
        <button class="primary" onclick="event.stopPropagation();playlistByIndex('play',${index})">▶ Play</button>
        <button onclick="event.stopPropagation();playlistByIndex('load',${index})">＋ Queue</button>
        <button class="danger" onclick="event.stopPropagation();deletePlaylist(${index})" title="Delete playlist">✕</button>
      </div></article>`;
  }).join('');
}

function renderPlaylistPage() {
  const content = document.getElementById('content');
  const oldSearch = document.getElementById('playlistSongSearch');
  const oldValue = oldSearch ? oldSearch.value : '';
  const wasFocused = document.activeElement === oldSearch;
  const playlist = playlists.find(item => item.name === openPlaylistName);
  let html = '';
  if (!playlist) {
    openPlaylistName = null;
    html = `<section class="card"><div class="library-head"><div><h2>Your library</h2><p>Create a playlist, then fill it one search at a time.</p></div>
      <div class="library-create"><input id="playlistName" maxlength="50" placeholder="New playlist name" onkeydown="if(event.key==='Enter')createPlaylist()">
      <button class="primary" onclick="createPlaylist()">＋ Create</button></div></div>
      <div class="playlist-grid">${playlistCardsHTML()}</div></section>`;
  } else {
    const cover = playlist.cover ? `style="background-image:url('${cssUrl(mediaUrl(playlist.cover))}')"` : '';
    const rows = playlist.tracks.length ? playlist.tracks.map((track, index) => `<div class="playlist-track">
      <div class="track-number">${index + 1}</div>
      <div class="track-main"><span class="track-art ${track.thumbnail ? '' : 'missing'}">${track.thumbnail ? `<img src="${esc(mediaUrl(track.thumbnail))}" alt="" onerror="this.parentElement.classList.add('missing');this.remove()">` : ''}</span><div class="track-title">${esc(track.title)}</div></div>
      <div class="track-source">${esc(track.source_type === 'spotify' ? 'Spotify' : 'YouTube')}</div>
      <div class="track-duration">${esc(track.duration)}</div>
      <button class="danger" onclick="removePlaylistSong(${index})" title="Remove song">✕</button>
    </div>`).join('') : '<div class="empty">This playlist is empty. Search above to add its first song.</div>';
    html = `<section class="card playlist-page"><div class="playlist-hero">
      <div class="playlist-cover-large ${playlist.cover ? '' : 'empty'}" ${cover}>
        <label class="cover-upload">▣ Change photo<input type="file" accept="image/png,image/jpeg,image/webp" onchange="uploadPlaylistCover(this.files[0])"></label>
      </div><div><div class="playlist-eyebrow">Playlist</div><h2>${esc(playlist.name)}</h2>
      <div class="playlist-meta">${playlist.count} song${playlist.count === 1 ? '' : 's'} · Kivi Yeşili library</div></div></div>
      <div class="playlist-body"><div class="playlist-toolbar">
        <button class="playlist-play" onclick="playlistAction('play',openPlaylistName)" title="Play playlist">▶</button>
        <button onclick="playlistAction('load',openPlaylistName)">＋ Add to queue</button>
        <button onclick="openPlaylistName=null;render()">← Library</button>
        <div class="playlist-add"><input id="playlistSongSearch" list="playlistSuggestions" placeholder="Search a song or paste a Spotify / YouTube link" oninput="handleAutocomplete(this.value,'playlistSuggestions')" onkeydown="if(event.key==='Enter')addSongToPlaylist()">
          <datalist id="playlistSuggestions"></datalist><button class="primary" onclick="addSongToPlaylist()">＋ Add song</button></div>
      </div><div class="track-head"><span>#</span><span>Title</span><span>Source</span><span>Duration</span><span></span></div>${rows}</div>
    </section>`;
  }
  content.innerHTML = html + playerControlsHTML(state);
  const restored = document.getElementById('playlistSongSearch');
  if (restored) { restored.value = oldValue; if (wasFocused) restored.focus(); }
  const lyricsCard = document.getElementById('lyricsCard');
  if (lyricsCard) lyricsCard.style.display = 'none';
  tickProgress();
}

async function playlistAction(action, name, extra) {
  try {
    const res = await api(basePath + 'api/guilds/' + selected + '/playlists',
      {method:'POST', body:JSON.stringify(Object.assign({action, name}, extra || {}))});
    playlists = res.playlists || playlists;
    if (res.state) { state = res.state; lastStateAt = Date.now(); }
    if (action === 'add_song') {
      const search = document.getElementById('playlistSongSearch');
      if (search) search.value = '';
    }
    toast((action === 'play' ? '▶ ' : '✓ ') + res.message);
    render();
  } catch (e) { toast('❌ ' + e.message); }
}
function playlistByIndex(action, index) {
  const playlist = playlists[index];
  if (playlist) playlistAction(action, playlist.name);
}
function openPlaylist(index) {
  const playlist = playlists[index];
  if (playlist) { openPlaylistName = playlist.name; activeView = 'playlists'; render(); }
}
function createPlaylist() {
  const input = document.getElementById('playlistName');
  const name = input ? input.value.trim() : '';
  if (!name) { toast('Name the playlist first.'); if (input) input.focus(); return; }
  openPlaylistName = name;
  playlistAction('create', name);
}
function addSongToPlaylist() {
  const input = document.getElementById('playlistSongSearch');
  const query = input ? input.value.trim() : '';
  if (!query) { toast('Search for a song first.'); if (input) input.focus(); return; }
  toast('⏳ Finding that song…');
  playlistAction('add_song', openPlaylistName, {query});
}
function removePlaylistSong(index) {
  playlistAction('remove_song', openPlaylistName, {index});
}
async function uploadPlaylistCover(file) {
  if (!file) return;
  if (file.size > 4 * 1024 * 1024) { toast('❌ Cover must be smaller than 4 MB.'); return; }
  const form = new FormData(); form.append('name', openPlaylistName); form.append('cover', file);
  try {
    toast('⏳ Uploading cover…');
    const response = await fetch(basePath + 'api/guilds/' + selected + '/playlist-cover', {method:'POST', body:form});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Upload failed');
    playlists = result.playlists || playlists; toast('✓ Playlist cover updated.'); render();
  } catch (error) { toast('❌ ' + error.message); }
}
function deletePlaylist(index) {
  const playlist = playlists[index];
  if (playlist && confirm(`Delete “${playlist.name}”?`)) playlistAction('delete', playlist.name);
}

function toggleSettings() {
  settingsOpen = !settingsOpen;
  if (state) render();
}
function toggleQueue() {
  queueCollapsed = !queueCollapsed;
  localStorage.setItem('mb_queue_collapsed', queueCollapsed ? '1' : '0');
  render();
}
function toggleHistory() {
  historyCollapsed = !historyCollapsed;
  localStorage.setItem('mb_history_collapsed', historyCollapsed ? '1' : '0');
  render();
}
function moveSection(key, direction) {
  const index = sectionOrder.indexOf(key);
  const target = index + direction;
  if (index < 0 || target < 0 || target >= sectionOrder.length) return;
  [sectionOrder[index], sectionOrder[target]] = [sectionOrder[target], sectionOrder[index]];
  localStorage.setItem('mb_section_order', JSON.stringify(sectionOrder));
  render();
}
function sectionActions(key, extra) {
  const index = sectionOrder.indexOf(key);
  return `<div class="section-actions">${extra || ''}
    <button onclick="moveSection('${key}',-1)" ${index === 0 ? 'disabled' : ''} title="Move section up">↑</button>
    <button onclick="moveSection('${key}',1)" ${index === sectionOrder.length - 1 ? 'disabled' : ''} title="Move section down">↓</button>
  </div>`;
}

async function refreshGuilds() {
  try {
    const data = await api(basePath + 'api/guilds');
    document.getElementById('statusDot').classList.toggle('on', data.ready);
    guilds = data.guilds;
    if (!selected && guilds.length) selected = (guilds.find(g => g.connected) || guilds[0]).id;
    renderTabs();
    if (selected) refreshState();
    else document.getElementById('content').innerHTML = '<div class="card empty">Bot is not in any server yet.</div>';
  } catch (e) { if (e.message !== 'auth') console.error(e); }
}
function renderTabs() {
  const tabsEl = document.getElementById('tabs');
  if (guilds.length <= 1) {
    tabsEl.style.display = 'none';
  } else {
    tabsEl.style.display = 'flex';
    tabsEl.innerHTML = guilds.map(g =>
      `<div class="tab ${g.id === selected ? 'active' : ''}" onclick="pick('${g.id}')">` +
      (g.icon ? `<img src="${g.icon}">` : '') + esc(g.name) +
      (g.playing ? ' <span class="live">● live</span>' : '') + `</div>`).join('');
  }
}
function pick(id) { selected = id; playlists = []; playlistsGuild = null; localStorage.setItem('mb_guild', id); renderTabs(); refreshState(); }

async function refreshPlaylists() {
  if (!selected) return;
  try {
    const data = await api(basePath + 'api/guilds/' + selected + '/playlists');
    playlists = data.playlists || [];
    playlistsGuild = selected;
    if (state) render();
  } catch (e) { if (e.message !== 'auth') console.error(e); }
}

async function refreshState() {
  if (!selected) return;
  try {
    state = await api(basePath + 'api/guilds/' + selected);
    lastStateAt = Date.now();
    render();
    if (playlistsGuild !== selected) refreshPlaylists();
    
    // Fetch lyrics if song changed
    if (state.current) {
      if (state.current.title !== lastLyricsTitle) {
        lastLyricsTitle = state.current.title;
        lyricsData = null;
        try {
          lyricsData = await api(basePath + 'api/guilds/' + selected + '/lyrics');
        } catch (e) {
          console.error("Failed to load lyrics:", e);
        }
      }
    } else {
      lastLyricsTitle = null;
      lyricsData = null;
    }
  } catch (e) { if (e.message !== 'auth') console.error(e); }
}

function render() {
  if (!state) return;
  
  // Preserve input value and focus state before rendering to prevent wipes
  const oldInput = document.getElementById('q');
  const inputVal = oldInput ? oldInput.value : '';
  const isFocused = (document.activeElement === oldInput);
  const oldPlaylistInput = document.getElementById('playlistName');
  const playlistInputVal = oldPlaylistInput ? oldPlaylistInput.value : '';
  const playlistInputFocused = (document.activeElement === oldPlaylistInput);
  const s = state;
  const ambientArt = s.current && s.current.thumbnail ? `url("${cssUrl(s.current.thumbnail)}")` : 'none';
  document.body.style.setProperty('--ambient-art', ambientArt);
  renderViewNav();
  if (activeView === 'playlists') {
    renderPlaylistPage();
    return;
  }
  const sleepMinutes = s.sleep_timer_ends_at
    ? Math.max(0, Math.ceil((s.sleep_timer_ends_at * 1000 - Date.now()) / 60000)) : 0;
  let html = '';

  // Detect track change for the vinyl record-swap animation
  const curTitle = s.current ? s.current.title : null;
  if (isVinyl() && curTitle && lastVinylTitle && curTitle !== lastVinylTitle &&
      !swapAnim && Date.now() - lastManualSkipAt > 2500) {
    swapAnim = {phase: 'in', start: performance.now()};
  }
  lastVinylTitle = curTitle;

  html += '<div class="card">';
  if (isVinyl()) {
    html += turntableHTML(s);
    if (s.current) {
      const c = s.current;
      html += `<div class="vinyl-info">
        <div class="title">${esc(c.title)}</div>
        <div class="sub">requested by ${esc(c.requester)}${s.channel ? ' • 🔊 ' + esc(s.channel) : ''}${s.is_247 ? ' • 🔄 24/7' : ''}</div>
        <div class="times"><span id="pos"></span><span>${esc(c.duration)}</span></div>
      </div>`;
    } else {
      html += `<div class="empty">${s.connected ? 'No record on the platter — add a song below.' : 'Not in a voice channel. Use /join in Discord.'}</div>`;
    }
  } else if (s.current) {
    const c = s.current;
    html += `<div class="np">` +
      (c.thumbnail ? `<img src="${esc(mediaUrl(c.thumbnail))}" alt="">` : '<img alt="">') +
      `<div style="flex:1;min-width:0">
        <div class="title">${esc(c.title)}</div>
        <div class="sub">requested by ${esc(c.requester)}${s.channel ? ' • 🔊 ' + esc(s.channel) : ''}` +
        `${s.is_247 ? ' • 🔄 24/7' : ''}</div>
        <div class="bar" onclick="seekProgress(event)"><div id="pbar"></div></div>
        <div class="times"><span id="pos"></span><span>${esc(c.duration)}</span></div>
      </div></div>`;
  } else {
    html += `<div class="empty">${s.connected ? 'Nothing playing — add a song below.' : 'Not in a voice channel. Use /join in Discord.'}</div>`;
  }
  html += '</div>';
  html += playerControlsHTML(s);

  html += `<div class="card"><div class="addrow" style="position:relative;">
      <input type="text" id="q" list="suggestions" placeholder="Song name or YouTube / Spotify link…" onkeydown="if(event.key==='Enter')addSong(false)" oninput="handleAutocomplete(this.value)" autocomplete="off">
      <datalist id="suggestions"></datalist>
      <button class="primary" onclick="addSong(false)">Add</button>
      <button onclick="addSong(true)">Play next</button>
    </div></div>`;

  const settingsHtml = `<div class="card" id="settingsPanel"><div class="qhead"><h2>⚙️ All settings</h2>
      <button onclick="toggleSettings()" aria-label="Close settings">✕ Close</button></div>
    <div class="setting">
      <div class="setting-text"><div class="setting-title">Volume</div>
        <div class="setting-desc">Current playback level: ${s.volume}%.</div></div>
      <input type="range" min="0" max="100" value="${s.volume}"
        onchange="act('volume',{level:+this.value})">
    </div>
    <div class="setting">
      <div class="setting-text"><div class="setting-title">Loop mode</div>
        <div class="setting-desc">Repeat one song, the full queue, or neither.</div></div>
      <select onchange="act('loop',{mode:this.value})">
        <option value="off" ${s.loop==='off'?'selected':''}>Off</option>
        <option value="song" ${s.loop==='song'?'selected':''}>Song</option>
        <option value="queue" ${s.loop==='queue'?'selected':''}>Queue</option>
      </select>
    </div>
    <div class="setting">
      <div class="setting-text"><div class="setting-title">Smart Autoplay</div>
        <div class="setting-desc">Keep playing related music when the queue runs out.</div></div>
      <button class="${s.autoplay ? 'toggled' : ''}" onclick="act('autoplay')">${s.autoplay ? 'Enabled' : 'Disabled'}</button>
    </div>
    <div class="setting">
      <div class="setting-text"><div class="setting-title">Artist variety after 3 songs</div>
        <div class="setting-desc">When AutoPlay plays the same artist three times in a row, prefer a related artist next.</div></div>
      <button class="${s.artist_diversity ? 'toggled' : ''}" onclick="act('artist_diversity')">${s.artist_diversity ? 'Enabled' : 'Disabled'}</button>
    </div>
    <div class="setting">
      <div class="setting-text"><div class="setting-title">Vibe matching</div>
        <div class="setting-desc">AutoPlay follows the current song's energy and genre and weighs ❤️ likes. Off = classic artist/album radio.</div></div>
      <button class="${s.vibe_match ? 'toggled' : ''}" onclick="act('vibe_match')">${s.vibe_match ? 'Enabled' : 'Disabled'}</button>
    </div>
    <div class="setting">
      <div class="setting-text"><div class="setting-title">Audio filter</div>
        <div class="setting-desc">Apply an effect to playback.</div></div>
      <select onchange="act('filter',{preset:this.value})">
        <option value="off" ${s.audio_filter==='off'?'selected':''}>Off</option>
        <option value="bassboost" ${s.audio_filter==='bassboost'?'selected':''}>Bass Boost</option>
        <option value="nightcore" ${s.audio_filter==='nightcore'?'selected':''}>Nightcore</option>
        <option value="slowed" ${s.audio_filter==='slowed'?'selected':''}>Slowed</option>
        <option value="8d" ${s.audio_filter==='8d'?'selected':''}>8D</option>
        <option value="karaoke" ${s.audio_filter==='karaoke'?'selected':''}>Karaoke filter</option>
      </select>
    </div>
    <div class="setting">
      <div class="setting-text"><div class="setting-title">Crossfade</div>
        <div class="setting-desc">Fade songs in and out; AutoMix takes priority when enabled.</div></div>
      <select onchange="act('crossfade',{seconds:+this.value})">
        ${[0,1,2,3,4,5,6,8,10].map(n=>`<option value="${n}" ${s.crossfade_seconds===n?'selected':''}>${n?n+' seconds':'Off'}</option>`).join('')}
      </select>
    </div>
    <div class="setting">
      <div class="setting-text"><div class="setting-title">AutoMix</div>
        <div class="setting-desc">Beat-aware DJ transitions between songs.</div></div>
      <button class="${s.automix ? 'toggled' : ''}" onclick="act('automix')">${s.automix ? 'Enabled' : 'Disabled'}</button>
    </div>
    <div class="setting">
      <div class="setting-text"><div class="setting-title">AutoMix blend</div>
        <div class="setting-desc">Maximum overlap between outgoing and incoming tracks.</div></div>
      <select onchange="act('automix_blend',{seconds:+this.value})">
        ${Array.from({length:12},(_,i)=>i+4).map(n=>`<option value="${n}" ${s.automix_blend_seconds===n?'selected':''}>${n} seconds</option>`).join('')}
      </select>
    </div>
    <div class="setting">
      <div class="setting-text"><div class="setting-title">Karaoke mode</div>
        <div class="setting-desc">Remove vocals and post live synced lyrics for every song.</div></div>
      <button class="${s.karaoke ? 'toggled' : ''}" onclick="act('karaoke')">${s.karaoke ? 'Enabled' : 'Disabled'}</button>
    </div>
    <div class="setting">
      <div class="setting-text"><div class="setting-title">Idle disconnect</div>
        <div class="setting-desc">Leave voice after the player has been idle.</div></div>
      <select onchange="act('idle_disconnect',{minutes:+this.value})">
        ${[0,1,5,10,15,30,60].map(n=>`<option value="${n}" ${s.idle_disconnect_minutes===n?'selected':''}>${n?n+' min':'Never'}</option>`).join('')}
      </select>
    </div>
    <div class="setting">
      <div class="setting-text"><div class="setting-title">Sleep timer</div>
        <div class="setting-desc">${sleepMinutes ? sleepMinutes+' min remaining' : 'No timer active'}; fades out, stops, and leaves.</div></div>
      <select onchange="if(this.value!=='')act('sleep_timer',{minutes:+this.value})">
        <option value="">Choose…</option>
        <option value="0">Cancel</option>
        <option value="15">15 min</option><option value="30">30 min</option>
        <option value="45">45 min</option><option value="60">1 hour</option>
        <option value="120">2 hours</option><option value="240">4 hours</option>
        <option value="480">8 hours</option>
      </select>
    </div>
    <div class="setting">
      <div class="setting-text"><div class="setting-title">24/7 mode</div>
        <div class="setting-desc">Admin-managed continuous library playback; use /247start or /247stop in Discord.</div></div>
      <button disabled class="${s.is_247 ? 'toggled' : ''}">${s.is_247 ? 'Active' : 'Off'}</button>
    </div>
  </div>`;

  const queueButtons = `<button onclick="toggleQueue()">${queueCollapsed ? '▾ Show queue' : '▴ Hide queue'}</button>` +
    (s.queue_length ? `<button class="danger" onclick="if(confirm('Clear the queue?'))act('clear')">Clear</button>` : '');
  let queueHtml = `<div class="card"><div class="qhead"><h2>📜 Queue (${s.queue_length})</h2>${sectionActions('queue', queueButtons)}</div>`;
  if (!queueCollapsed && s.queue.length) {
    queueHtml += s.queue.map((q, i) =>
      `<div class="qitem"><span class="n">${i + 1}.</span><span class="t">${esc(q.title)}</span>` +
      `<span class="d">${esc(q.duration)}</span><span class="btns">` +
      `<button title="Play now" onclick="act('skipto',{index:${i}})">▶</button>` +
      `<button title="Move to top" onclick="act('top',{index:${i}})">⬆</button>` +
      `<button title="Remove" onclick="act('remove',{index:${i}})">✖</button></span></div>`).join('');
    if (s.queue_length > s.queue.length) html += `<div class="empty">…and ${s.queue_length - s.queue.length} more</div>`;
  } else if (!queueCollapsed) {
    queueHtml += '<div class="empty">Queue is empty.</div>';
  }
  queueHtml += '</div>';

  const historyButtons = `<button onclick="toggleHistory()">${historyCollapsed ? '▾ Show history' : '▴ Hide history'}</button>`;
  const historyHtml = `<div class="card"><div class="qhead"><h2>🕘 Recently played (${s.history.length})</h2>
    ${sectionActions('history', historyButtons)}</div>` +
    (!historyCollapsed && s.history.length
      ? s.history.map((q, i) => `<div class="qitem"><span class="n">${i + 1}.</span><span class="t">${esc(q.title)}</span><span class="d">${esc(q.duration)}</span></div>`).join('')
      : !historyCollapsed ? '<div class="empty">Your listening history will appear here after the first song finishes.</div>' : '') +
    '</div>';

  const lyricsButtons = `<button id="lyricsToggleBtn" onclick="toggleLyrics()">${lyricsVisible ? '▴ Hide lyrics' : '▾ Show lyrics'}</button>`;
  const lyricsHtml = `<div class="card" id="lyricsCard" ${s.current ? '' : 'style="display:none"'}>
    <div class="qhead"><h2>🎙️ LyricsNow</h2>${sectionActions('lyrics', lyricsButtons)}</div>
    <div id="lyricsContent" style="margin-top:10px;display:${lyricsVisible ? 'block' : 'none'}"></div></div>`;

  const movableSections = {queue: queueHtml, history: historyHtml, lyrics: lyricsHtml};
  html += sectionOrder.map(key => movableSections[key]).join('');

  if (settingsOpen) html = settingsHtml + html;
  document.getElementById('content').innerHTML = html;
  const settingsBtn = document.getElementById('settingsBtn');
  if (settingsBtn) {
    settingsBtn.classList.toggle('toggled', settingsOpen);
    settingsBtn.setAttribute('aria-expanded', settingsOpen ? 'true' : 'false');
  }
  
  // Restore input value and focus state
  const newInput = document.getElementById('q');
  if (newInput) {
    newInput.value = inputVal;
    if (isFocused) newInput.focus();
  }
  const newPlaylistInput = document.getElementById('playlistName');
  if (newPlaylistInput) {
    newPlaylistInput.value = playlistInputVal;
    if (playlistInputFocused) newPlaylistInput.focus();
  }
  
  // Show/hide lyrics card based on playing state
  const lyricsCard = document.getElementById('lyricsCard');
  if (lyricsCard) {
    if (s.current) {
      lyricsCard.style.display = 'block';
    } else {
      lyricsCard.style.display = 'none';
    }
  }
  
  tickProgress();
}

function tickProgress() {
  if (!state || !state.current) {
    if (lyricsVisible && document.getElementById('lyricsContent')) {
      document.getElementById('lyricsContent').innerHTML = '<div class="empty">Nothing playing.</div>';
    }
    return;
  }
  const c = state.current;
  let pos = c.position_seconds || 0;
  if (state.playing && !state.paused) pos += (Date.now() - lastStateAt) / 1000;
  const total = c.duration_seconds;
  const bar = document.getElementById('pbar'), bottomBar = document.getElementById('bottomPbar'), posEl = document.getElementById('pos');
  if (bar && total) bar.style.width = Math.min(100, pos / total * 100) + '%';
  if (bottomBar && total) bottomBar.style.width = Math.min(100, pos / total * 100) + '%';
  if (posEl) posEl.textContent = fmt(pos);
  
  // Update lyrics scroll progress
  updateLyricsProgress(pos);
}

async function act(action, extra) {
  try {
    state = await api(basePath + 'api/guilds/' + selected + '/action',
      {method: 'POST', body: JSON.stringify(Object.assign({action}, extra || {}))});
    lastStateAt = Date.now();
    render();
  } catch (e) { toast('❌ ' + e.message); }
}

async function addSong(playNext) {
  const input = document.getElementById('q');
  const query = input.value.trim();
  if (!query) return;
  input.disabled = true;
  toast('⏳ Searching…');
  try {
    const res = await api(basePath + 'api/guilds/' + selected + '/play',
      {method: 'POST', body: JSON.stringify({query, next: playNext})});
    toast(res.started ? '▶️ Playing: ' + res.first_title
        : '✅ Added ' + (res.added > 1 ? res.added + ' songs' : res.first_title));
    state = res.state; lastStateAt = Date.now(); render();
    const el = document.getElementById('q'); if (el) { el.value = ''; el.disabled = false; }
  } catch (e) {
    toast('❌ ' + e.message);
    input.disabled = false;
  }
}

function seekProgress(event) {
  if (!state || !state.current || !state.current.duration_seconds) return;
  const rect = event.currentTarget.getBoundingClientRect();
  const clickX = event.clientX - rect.left;
  const pct = clickX / rect.width;
  const sec = Math.round(pct * state.current.duration_seconds);
    act('seek', {seconds: sec});
}

function toggleLyrics() {
  lyricsVisible = !lyricsVisible;
  localStorage.setItem('mb_lyrics_hidden', lyricsVisible ? '0' : '1');
  const content = document.getElementById('lyricsContent');
  const button = document.getElementById('lyricsToggleBtn');
  if (content) content.style.display = lyricsVisible ? 'block' : 'none';
  if (button) button.textContent = lyricsVisible ? '▴ Hide lyrics' : '▾ Show lyrics';
}

function updateLyricsProgress(pos) {
  const contentEl = document.getElementById('lyricsContent');
  if (!contentEl) return;
  
  if (!lyricsVisible) return;
  
  if (!lyricsData || !lyricsData.lines || lyricsData.lines.length === 0) {
    contentEl.innerHTML = '<div class="empty">No synced lyrics available.</div>';
    return;
  }
  
  const lines = lyricsData.lines;
  let currentIdx = -1;
  
  for (let i = 0; i < lines.length; i++) {
    if (lines[i][0] <= pos) {
      currentIdx = i;
    } else {
      break;
    }
  }
  
  if (currentIdx === -1 && lines.length > 0) {
    currentIdx = 0;
  }
  
  const startIndex = Math.max(0, currentIdx - 2);
  const endIndex = Math.min(lines.length, currentIdx + 3);
  
  let html = '';
  for (let i = startIndex; i < endIndex; i++) {
    const isActive = (i === currentIdx);
    const lineText = lines[i][1];
    html += `<div class="lyric-line ${isActive ? 'active' : ''}">${isActive ? '▶ ' : ''}${esc(lineText)}</div>`;
  }
  
  contentEl.innerHTML = html || '<div class="empty">Instrumental</div>';
}

let autocompleteTimeout = null;

function handleAutocomplete(val, targetId) {
  targetId = targetId || 'suggestions';
  clearTimeout(autocompleteTimeout);
  if (!val || val.trim().length < 2) {
    const dl = document.getElementById(targetId);
    if (dl) dl.innerHTML = '';
    return;
  }
  
  autocompleteTimeout = setTimeout(async () => {
    try {
      const suggestions = await api(basePath + 'api/autocomplete?q=' + encodeURIComponent(val));
      const dl = document.getElementById(targetId);
      if (dl) {
        dl.innerHTML = suggestions.map(s => `<option value="${esc(s)}">`).join('');
      }
    } catch (e) {
      console.error(e);
    }
  }, 250); // 250ms debounce
}

// ---------- turntable engine (vinyl themes) ----------
// Geometry: tonearm pivot sits top-right; rotate() angle 0 = arm hanging straight
// down, positive = tip swings left onto the record. Angles below are derived from
// the .deck CSS layout (pivot at (334,42), record centre (154,160), needle 200px
// from pivot) — keep them in sync if the deck geometry changes.
const ARM_REST = 6, ARM_OUT = 25.5, ARM_IN = 44;
let discAngle = 0, armAngle = ARM_REST, armLift = 0, armDrag = null, swapAnim = null;
let lastVinylTitle = null, lastManualSkipAt = 0, lastFrame = null;

function turntableHTML(s) {
  const c = s.current;
  const vinylCover = mediaUrl(s.active_playlist_cover || (c && c.thumbnail));
  const art = vinylCover ? ` style="background-image:url('${cssUrl(vinylCover)}')"` : '';
  return `<div class="deck">
      <div class="plinth"></div>
      <div class="platter"></div>
      <div class="vinyl-wrap" id="vinylWrap">
        <div class="vinyl" onclick="vinylClick()" title="Click record: pause / resume">
          <div class="disc" id="disc"><div class="album-art"${art}></div><div class="mark"></div><div class="label"></div></div>
          <div class="sheen"></div>
          <div class="spindle"></div>
        </div>
      </div>
      <div class="arm-mount" id="armMount">
        <div class="tonearm" id="tonearm" title="Drag the arm to seek">
          <div class="weight"></div><div class="pivot"></div>
          <div class="shaft"></div><div class="head"></div><div class="needle"></div>
        </div>
      </div>
      <div class="deck-hint" id="deckHint"></div>
    </div>`;
}

function vinylClick() {
  if (!state || !state.current || swapAnim || armDrag) return;
  act(state.paused ? 'resume' : 'pause');
}

function doSkip() {
  if (isVinyl() && state && state.current && !swapAnim) {
    lastManualSkipAt = Date.now();
    swapAnim = {phase: 'out', start: performance.now()};
    act('skip');
  } else {
    act('skip');
  }
}

function currentPosSeconds() {
  const c = state && state.current;
  if (!c) return 0;
  let pos = c.position_seconds || 0;
  if (state.playing && !state.paused) pos += (Date.now() - lastStateAt) / 1000;
  return pos;
}

function armTargetAngle() {
  if (!state || !state.current || (swapAnim && swapAnim.phase === 'out')) return ARM_REST;
  if (armDrag) return armDrag.angle;
  const c = state.current;
  const p = c.duration_seconds ? Math.min(1, currentPosSeconds() / c.duration_seconds) : 0;
  return ARM_OUT + p * (ARM_IN - ARM_OUT);
}

function updateDragAngle(e) {
  const mount = document.getElementById('armMount');
  if (!mount || !armDrag) return;
  const r = mount.getBoundingClientRect();
  const dx = e.clientX - (r.left + r.width / 2);
  const dy = e.clientY - (r.top + r.height / 2);
  let a = Math.atan2(-dx, dy) * 180 / Math.PI;
  a = Math.max(ARM_OUT, Math.min(ARM_IN, a));
  armDrag.angle = a;
  const c = state && state.current;
  const hint = document.getElementById('deckHint');
  if (hint && c && c.duration_seconds) {
    const p = (a - ARM_OUT) / (ARM_IN - ARM_OUT);
    hint.textContent = '⏩ ' + fmt(p * c.duration_seconds);
  }
}

document.addEventListener('pointerdown', e => {
  if (!isVinyl() || !state || !state.current || swapAnim) return;
  if (!e.target.closest('#tonearm')) return;
  e.preventDefault();
  armDrag = {angle: Math.max(ARM_OUT, Math.min(ARM_IN, armAngle))};
  updateDragAngle(e);
});
document.addEventListener('pointermove', e => { if (armDrag) { e.preventDefault(); updateDragAngle(e); } });
document.addEventListener('pointerup', () => {
  if (!armDrag) return;
  const p = (armDrag.angle - ARM_OUT) / (ARM_IN - ARM_OUT);
  armDrag = null;
  const c = state && state.current;
  if (c && c.duration_seconds) {
    const sec = Math.round(Math.max(0, Math.min(0.995, p)) * c.duration_seconds);
    toast('⏩ Dropping the needle at ' + fmt(sec) + '…');
    act('seek', {seconds: sec});
  }
});
document.addEventListener('pointercancel', () => { armDrag = null; });

function deckFrame(ts) {
  requestAnimationFrame(deckFrame);
  const dt = lastFrame ? Math.min(0.1, (ts - lastFrame) / 1000) : 0;
  lastFrame = ts;
  if (!isVinyl()) return;
  const disc = document.getElementById('disc');
  const tonearm = document.getElementById('tonearm');
  const wrap = document.getElementById('vinylWrap');
  if (!disc || !tonearm || !wrap) return;

  // record spin (33 1/3 rpm = 200 deg/s)
  const spinning = state && state.current && state.playing && !state.paused && !armDrag;
  if (spinning) discAngle = (discAngle + dt * 200) % 360;
  // real records are slightly warped: they bob up/down and tilt once per revolution.
  // translateY (screen-vertical bob) before rotate; skewX fakes the tilt of the far edge.
  const rad = discAngle * Math.PI / 180;
  const bob = Math.sin(rad) * 2.2;          // px — the "up and down just a bit"
  const warp = Math.cos(rad) * 0.8;         // deg — subtle wobble of the plane
  disc.style.transform = 'translateY(' + bob + 'px) skewX(' + warp + 'deg) rotate(' + discAngle + 'deg)';

  // tonearm follows its target with easing; lifted when not tracking a groove
  const target = armTargetAngle();
  armAngle += (target - armAngle) * Math.min(1, dt * 5);
  const lifted = !state || !state.current || state.paused || !!armDrag || !!swapAnim;
  // ease the physical lift so the arm visibly rises off / lands on the record
  armLift += ((lifted ? 1 : 0) - armLift) * Math.min(1, dt * 7);
  // rise straight up on screen (translateY before rotate) and swing the tip
  // a few degrees back off the groove — reads as the needle clearing the record
  const rise = armLift * 16;
  const tilt = armAngle - armLift * 5;
  tonearm.style.transform = 'translateY(' + (-rise) + 'px) rotate(' + tilt + 'deg)';
  tonearm.classList.toggle('lifted', armLift > 0.15);

  // record swap animation (skip / track change)
  const hasTrack = !!(state && state.current);
  wrap.style.display = (hasTrack || (swapAnim && swapAnim.phase === 'out')) ? 'block' : 'none';
  if (swapAnim) {
    const now = performance.now();
    if (swapAnim.phase === 'out') {
      const t = (now - swapAnim.start) / 550;
      if (t >= 1) {
        swapAnim = hasTrack ? {phase: 'in', start: now} : null;
        if (!swapAnim) { wrap.style.transform = ''; wrap.style.opacity = '1'; }
      } else {
        const lift = Math.min(1, t * 2.5);
        wrap.style.transform = 'translate(' + (-t * t * 320) + 'px, ' + (-26 * lift) + 'px)';
        wrap.style.opacity = String(1 - Math.max(0, t - 0.55) / 0.45);
      }
    } else {
      const t = (now - swapAnim.start) / 550;
      if (t >= 1 || !hasTrack) {
        swapAnim = null;
        wrap.style.transform = '';
        wrap.style.opacity = '1';
      } else {
        const ease = 1 - Math.pow(1 - t, 3);
        wrap.style.transform = 'translate(0px, ' + (-140 * (1 - ease)) + 'px) scale(' + (0.96 + 0.04 * ease) + ')';
        wrap.style.opacity = String(Math.min(1, t * 2));
      }
    }
  }

  const hint = document.getElementById('deckHint');
  if (hint && !armDrag) {
    const def = hasTrack ? 'tap the record to pause · drag the arm to seek' : '';
    if (hint.textContent !== def) hint.textContent = def;
  }
}
requestAnimationFrame(deckFrame);

setInterval(refreshGuilds, 10000);
setInterval(refreshState, 3000);
setInterval(tickProgress, 500);
exchangeLinkToken().then(ok=>{if(ok)refreshGuilds()});
</script>
</body>
</html>
"""
