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
import json
import logging
import os
import secrets
import time

from aiohttp import web

import config
from music import get_autocomplete_suggestions

logger = logging.getLogger('MusicBot.WebUI')

# Tokens persist to disk so /web links survive bot restarts
TOKENS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web_tokens.json')


def _load_tokens():
    try:
        with open(TOKENS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    except Exception as e:
        logger.warning(f"Could not load web tokens: {e}")
        return {}


def _save_tokens():
    try:
        with open(TOKENS_FILE, 'w', encoding='utf-8') as f:
            json.dump(temp_tokens, f)
    except Exception as e:
        logger.warning(f"Could not save web tokens: {e}")


temp_tokens = _load_tokens()

def generate_token(guild_id):
    token = secrets.token_urlsafe(16)
    # Clean up expired tokens (older than 24 hours)
    now = time.time()
    for k, v in list(temp_tokens.items()):
        if now - v['created_at'] > 86400:
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


def _song_json(song):
    return {
        'title': song.title,
        'duration': song.duration,
        'duration_seconds': _duration_to_seconds(song.duration),
        'requester': getattr(song.requester, 'display_name', str(song.requester)),
        'source_type': song.source_type,
        'thumbnail': song.thumbnail,
    }


class WebUI:
    def __init__(self, bot):
        self.bot = bot
        self.lyrics_cache = {}

    @property
    def cog(self):
        return self.bot.get_cog('MusicCog')

    # ---------- auth ----------

    def _authorized(self, request):
        supplied = request.headers.get('X-Auth-Token') or request.query.get('auth') or ''
        
        # If a token was supplied, it must be valid (either config password or temp token)
        if supplied:
            password = config.WEB_UI_PASSWORD
            if password and secrets.compare_digest(supplied, password):
                return True
            if supplied in temp_tokens:
                return True
            return False # Supplied token was invalid/expired
            
        # If no token was supplied:
        password = config.WEB_UI_PASSWORD
        if not password:
            return True # Allowed if no password is set in config
            
        return False


    def _get_allowed_guild_id(self, request):
        supplied = request.headers.get('X-Auth-Token') or request.query.get('auth') or ''
        if not supplied:
            return None
        # Master password bypasses restriction
        password = config.WEB_UI_PASSWORD
        if password and secrets.compare_digest(supplied, password):
            return None
        # Return guild ID for temp token
        token_info = temp_tokens.get(supplied)
        if token_info:
            return token_info['guild_id']
        return None

    @web.middleware
    async def auth_middleware(self, request, handler):
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
            'is_247': player.is_247_mode,
            'current': current,
            'queue': [_song_json(s) for s in list(player.queue)[:100]],
            'queue_length': len(player.queue),
            'history': [_song_json(s) for s in list(player.history)[:10]],
        }

    # ---------- routes ----------

    async def index(self, request):
        return web.Response(text=INDEX_HTML, content_type='text/html')

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
            elif action == 'stop':
                for stopper in ('_stop_now_playing_task', '_stop_lyricsnow_task'):
                    fn = getattr(cog, stopper, None)
                    if fn:
                        fn(guild.id)
                player.queue.clear()
                player.current = None
                player.loop = False
                player.loop_queue = False
                player.preloaded_sources.clear()
                player.reset_playback_clock()
                if vc:
                    vc.stop()
            elif action == 'shuffle':
                import random
                queue_list = list(player.queue)
                random.shuffle(queue_list)
                from collections import deque
                player.queue = deque(queue_list)
                player.preloaded_sources.clear()
                asyncio.create_task(player.preload_next_song())
            elif action == 'clear':
                player.queue.clear()
                player.preloaded_sources.clear()
                player.schedule_idle_disconnect()
            elif action == 'volume':
                level = max(0, min(100, int(body.get('level', 50))))
                player.volume = level / 100
                if vc and vc.source:
                    vc.source.volume = player.volume
            elif action == 'loop':
                mode = body.get('mode', 'off')
                player.loop = mode == 'song'
                player.loop_queue = mode == 'queue'
            elif action == 'autoplay':
                player.autoplay = not player.autoplay
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
                    player.preloaded_sources.clear()
            elif action == 'skipto':
                index = int(body.get('index', -1))
                if 0 <= index < len(player.queue) and vc:
                    for _ in range(index):
                        player.queue.popleft()
                    player.loop = False
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
        suggestions = get_autocomplete_suggestions(query)
        return web.json_response(suggestions)



async def start_web_server(bot):
    if not config.WEB_UI_ENABLED:
        logger.info("Web UI disabled (WEB_UI_ENABLED=0)")
        return

    ui = WebUI(bot)
    app = web.Application(middlewares=[ui.auth_middleware])
    app.router.add_get('/', ui.index)
    app.router.add_get('/api/guilds', ui.api_guilds)
    app.router.add_get('/api/guilds/{guild_id}', ui.api_guild_state)
    app.router.add_post('/api/guilds/{guild_id}/action', ui.api_action)
    app.router.add_post('/api/guilds/{guild_id}/play', ui.api_play)
    app.router.add_get('/api/guilds/{guild_id}/lyrics', ui.api_lyrics)
    app.router.add_get('/api/autocomplete', ui.api_autocomplete)

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
    --radius: 12px;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text);
         font-family: system-ui, -apple-system, 'Segoe UI', sans-serif; }
  .wrap { max-width: 860px; margin: 0 auto; padding: 16px; }
  h1 { font-size: 20px; margin: 8px 0 16px; display: flex; align-items: center; gap: 10px; }
  h1 .dot { width: 10px; height: 10px; border-radius: 50%; background: var(--danger); }
  h1 .dot.on { background: var(--accent2); }
  .tabs { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 16px; }
  .tab { background: var(--panel); border: 1px solid transparent; color: var(--text);
         padding: 8px 14px; border-radius: 999px; cursor: pointer; font-size: 14px;
         display: flex; align-items: center; gap: 8px; }
  .tab img { width: 20px; height: 20px; border-radius: 50%; }
  .tab.active { border-color: var(--accent); background: var(--panel2); }
  .tab .live { color: var(--accent2); font-size: 11px; }
  .card { background: var(--panel); border-radius: var(--radius); padding: 16px; margin-bottom: 14px; }
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
  button.danger { background: transparent; color: var(--danger); border: 1px solid var(--danger); }
  button.toggled { outline: 2px solid var(--accent2); }
  .vol { display: flex; align-items: center; gap: 8px; margin-left: auto; color: var(--muted); font-size: 13px; }
  input[type=range] { accent-color: var(--accent); width: 120px; }
  .addrow { display: flex; gap: 8px; }
  .addrow input[type=text] { flex: 1; background: var(--panel2); border: 1px solid #333350;
      color: var(--text); border-radius: 8px; padding: 10px 12px; font-size: 14px; }
  .addrow input[type=text]:focus { outline: 1px solid var(--accent); }
  .qhead { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
  .qhead h2 { font-size: 15px; margin: 0; }
  .qitem { display: flex; align-items: center; gap: 10px; padding: 8px 6px;
           border-bottom: 1px solid #2a2a40; font-size: 14px; }
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
  #login input { width: 100%; margin: 12px 0; background: var(--panel2); border: 1px solid #333350;
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
</style>
</head>
<body>
<div class="wrap">
  <h1><span class="dot" id="statusDot"></span>🎵 Kivi Yeşili</h1>
  <div class="tabs" id="tabs"></div>
  <div id="content"><div class="empty">Loading…</div></div>
  
  <div class="card" id="lyricsCard" style="display: none;">
    <div class="qhead" style="cursor:pointer;" onclick="toggleLyrics()">
      <h2>🎙️ LyricsNow</h2>
      <span id="lyricsToggleBtn">▼ Hide</span>
    </div>
    <div id="lyricsContent" style="margin-top: 10px;"></div>
  </div>
</div>
<div class="msg" id="msg"></div>
<div id="login"><div class="box">
  <b>Password required</b>
  <input type="password" id="pw" placeholder="Web UI password">
  <button class="primary" onclick="savePw()" style="width:100%">Unlock</button>
</div></div>
<script>
const urlParams = new URLSearchParams(window.location.search);
const urlToken = urlParams.get('token');
const urlGuild = urlParams.get('guild_id');

if (urlToken) {
  sessionStorage.setItem('mb_token', urlToken);
} else {
  sessionStorage.removeItem('mb_token');
}

if (urlGuild) {
  sessionStorage.setItem('mb_guild', urlGuild);
} else {
  sessionStorage.removeItem('mb_guild');
}

let token = sessionStorage.getItem('mb_token') || localStorage.getItem('mb_token') || '';
let guilds = [], selected = sessionStorage.getItem('mb_guild') || localStorage.getItem('mb_guild') || null;
let state = null, lastStateAt = 0;
let lyricsData = null, lastLyricsTitle = null, lyricsVisible = true;

const basePath = window.location.pathname.endsWith('/') ? window.location.pathname : window.location.pathname + '/';

function hdrs() { return token ? {'X-Auth-Token': token, 'Content-Type': 'application/json'}
                               : {'Content-Type': 'application/json'}; }
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
function savePw() {
  token = document.getElementById('pw').value;
  localStorage.setItem('mb_token', token);
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
function pick(id) { selected = id; localStorage.setItem('mb_guild', id); renderTabs(); refreshState(); }

async function refreshState() {
  if (!selected) return;
  try {
    state = await api(basePath + 'api/guilds/' + selected);
    lastStateAt = Date.now();
    render();
    
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
  const s = state;
  let html = '';

  html += '<div class="card">';
  if (s.current) {
    const c = s.current;
    html += `<div class="np">` +
      (c.thumbnail ? `<img src="${c.thumbnail}">` : '<img>') +
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
  html += `<div class="controls">
      <button onclick="act('${s.paused ? 'resume' : 'pause'}')">${s.paused ? '▶️ Resume' : '⏸ Pause'}</button>
      <button onclick="act('skip')">⏭ Skip</button>
      <button onclick="act('shuffle')">🔀 Shuffle</button>
      <button class="${s.loop !== 'off' ? 'toggled' : ''}" onclick="cycleLoop()">${s.loop === 'song' ? '🔂 Song' : s.loop === 'queue' ? '🔁 Queue' : '➡️ No loop'}</button>
      <button class="${s.autoplay ? 'toggled' : ''}" onclick="act('autoplay')">🎲 Autoplay</button>
      <button class="danger" onclick="if(confirm('Stop and clear the queue?'))act('stop')">⏹ Stop</button>
      <div class="vol">🔊 <input type="range" min="0" max="100" value="${s.volume}"
        onchange="act('volume',{level:+this.value})"><span>${s.volume}%</span></div>
    </div></div>`;

  html += `<div class="card"><div class="addrow" style="position:relative;">
      <input type="text" id="q" list="suggestions" placeholder="Song name or YouTube / Spotify link…" onkeydown="if(event.key==='Enter')addSong(false)" oninput="handleAutocomplete(this.value)" autocomplete="off">
      <datalist id="suggestions"></datalist>
      <button class="primary" onclick="addSong(false)">Add</button>
      <button onclick="addSong(true)">Play next</button>
    </div></div>`;

  html += `<div class="card"><div class="qhead"><h2>📜 Queue (${s.queue_length})</h2>` +
    (s.queue_length ? `<button class="danger" onclick="if(confirm('Clear the queue?'))act('clear')">Clear</button>` : '') + `</div>`;
  if (s.queue.length) {
    html += s.queue.map((q, i) =>
      `<div class="qitem"><span class="n">${i + 1}.</span><span class="t">${esc(q.title)}</span>` +
      `<span class="d">${esc(q.duration)}</span><span class="btns">` +
      `<button title="Play now" onclick="act('skipto',{index:${i}})">▶</button>` +
      `<button title="Move to top" onclick="act('top',{index:${i}})">⬆</button>` +
      `<button title="Remove" onclick="act('remove',{index:${i}})">✖</button></span></div>`).join('');
    if (s.queue_length > s.queue.length) html += `<div class="empty">…and ${s.queue_length - s.queue.length} more</div>`;
  } else {
    html += '<div class="empty">Queue is empty.</div>';
  }
  html += '</div>';

  if (s.history.length) {
    html += `<div class="card"><details><summary>🕘 Recently played (${s.history.length})</summary>` +
      s.history.map((q, i) => `<div class="qitem"><span class="n">${i + 1}.</span><span class="t">${esc(q.title)}</span><span class="d">${esc(q.duration)}</span></div>`).join('') +
      '</details></div>';
  }

  document.getElementById('content').innerHTML = html;
  
  // Restore input value and focus state
  const newInput = document.getElementById('q');
  if (newInput) {
    newInput.value = inputVal;
    if (isFocused) newInput.focus();
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
  const bar = document.getElementById('pbar'), posEl = document.getElementById('pos');
  if (bar && total) bar.style.width = Math.min(100, pos / total * 100) + '%';
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
  document.getElementById('lyricsContent').style.display = lyricsVisible ? 'block' : 'none';
  document.getElementById('lyricsToggleBtn').textContent = lyricsVisible ? '▼ Hide' : '▲ Show';
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

function handleAutocomplete(val) {
  clearTimeout(autocompleteTimeout);
  if (!val || val.trim().length < 2) {
    const dl = document.getElementById('suggestions');
    if (dl) dl.innerHTML = '';
    return;
  }
  
  autocompleteTimeout = setTimeout(async () => {
    try {
      const suggestions = await api(basePath + 'api/autocomplete?q=' + encodeURIComponent(val));
      const dl = document.getElementById('suggestions');
      if (dl) {
        dl.innerHTML = suggestions.map(s => `<option value="${esc(s)}">`).join('');
      }
    } catch (e) {
      console.error(e);
    }
  }, 250); // 250ms debounce
}

setInterval(refreshGuilds, 10000);
setInterval(refreshState, 3000);
setInterval(tickProgress, 500);
refreshGuilds();
</script>
</body>
</html>
"""
