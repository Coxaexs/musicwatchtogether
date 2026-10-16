import os
from dotenv import load_dotenv

load_dotenv()

# Discord
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

# Spotify (optional)
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")

# Watch Together Web Server
WEB_SERVER_URL = os.getenv("WEB_SERVER_URL", "http://localhost:8722")

# FFmpeg options for audio streaming
FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn -filter:a "volume=0.5"'
}

# yt-dlp options
YTDL_OPTIONS = {
    'format': 'bestaudio/best',
    'extractaudio': True,
    'audioformat': 'opus',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': False,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'ytsearch',
    'source_address': '0.0.0.0',
}

# YouTube cookies (optional) for age-restricted videos. Point YTDLP_COOKIES at a
# Netscape-format cookies.txt exported from a logged-in browser. yt-dlp writes
# the jar back on exit, so it gets a private copy and the original stays intact.
YTDLP_COOKIEFILE = None
_cookies_source = os.getenv("YTDLP_COOKIES", "").strip()
if _cookies_source and os.path.isfile(_cookies_source):
    import shutil
    import tempfile
    YTDLP_COOKIEFILE = os.path.join(tempfile.mkdtemp(prefix="ytdlp-"), "cookies.txt")
    shutil.copyfile(_cookies_source, YTDLP_COOKIEFILE)
if YTDLP_COOKIEFILE:
    YTDL_OPTIONS['cookiefile'] = YTDLP_COOKIEFILE

# Data directory for persistent storage (used in Docker / deployments)
DATA_DIR = os.getenv("DATA_DIR", "").strip()
if DATA_DIR and os.path.isfile(os.path.join(DATA_DIR, ".env")):
    load_dotenv(os.path.join(DATA_DIR, ".env"))

# Supported local file formats
SUPPORTED_FORMATS = ['.mp3', '.wav', '.ogg', '.flac', '.m4a', '.aac', '.opus', '.webm']

# Optional media library integrations
# Path where downloaded music files are stored for external media servers (Navidrome/Lidarr)
_default_musics = (
    os.path.join(DATA_DIR, "musics")
    if DATA_DIR
    else (
        "/mnt/harddisk/data/bot/musicbot-main/musics"
        if os.path.exists("/mnt/harddisk/data/bot/musicbot-main/musics")
        else os.path.join(os.path.dirname(os.path.abspath(__file__)), "musics")
    )
)
MUSIC_LIBRARY_PATH = os.getenv("MUSIC_LIBRARY_PATH", _default_musics)
# Navidrome / Lidarr integration (optional). Set these in your .env if you want auto-refresh.
NAVIDROME_URL = os.getenv("NAVIDROME_URL")
NAVIDROME_API_KEY = os.getenv("NAVIDROME_API_KEY")
LIDARR_URL = os.getenv("LIDARR_URL")
LIDARR_API_KEY = os.getenv("LIDARR_API_KEY")
# Enable detailed cache lookup logging when set to '1'
CACHE_LOOKUP_DEBUG = os.getenv("CACHE_LOOKUP_DEBUG", "0")

# Role name that can always skip songs without a vote
DJ_ROLE_NAME = os.getenv("DJ_ROLE_NAME", "DJ")

# Huddle (the self-hosted chat app that shares this bot). When both are set the
# dashboard also lists Huddle voice rooms and can drive their playback.
HUDDLE_BASE_URL = os.getenv("HUDDLE_BASE_URL", "")
HUDDLE_BOT_TOKEN = os.getenv("HUDDLE_BOT_TOKEN", "")

def _bool_env(name, default=True, aliases=(), disable_aliases=()):
    # Check negative flags first (e.g. DISABLE_DJ=1)
    for n in disable_aliases:
        val = os.getenv(n)
        if val is not None and val.strip().lower() in ("1", "true", "yes", "on", "enable", "enabled"):
            return False
    # Check positive names/aliases for explicit disable (e.g. DJ_ENABLED=0 or ENABLE_DJ=false)
    names = (name, *aliases)
    for n in names:
        val = os.getenv(n)
        if val is not None and val.strip().lower() in ("0", "false", "no", "off", "disable", "disabled"):
            return False
    # Check positive names/aliases for explicit enable (e.g. DJ_ENABLED=1 or ENABLE_DJ=true)
    for n in names:
        val = os.getenv(n)
        if val is not None and val.strip().lower() in ("1", "true", "yes", "on", "enable", "enabled"):
            return True
    return default

# Feature Toggles (enable/disable major bot capabilities)
DJ_ENABLED = _bool_env("DJ_ENABLED", True, aliases=["ENABLE_DJ"], disable_aliases=["DISABLE_DJ"])
WATCHTOGETHER_ENABLED = _bool_env("WATCHTOGETHER_ENABLED", True, aliases=["ENABLE_WATCHTOGETHER", "ENABLE_WATCH_TOGETHER"], disable_aliases=["DISABLE_WATCHTOGETHER", "DISABLE_WATCH_TOGETHER"])
STEMS_ENABLED = _bool_env("STEMS_ENABLED", True, aliases=["ENABLE_STEMS", "DJ_STEMS"], disable_aliases=["DISABLE_STEMS"])
COBROWSER_ENABLED = _bool_env("COBROWSER_ENABLED", True, aliases=["ENABLE_COBROWSER", "ENABLE_CO_BROWSER"], disable_aliases=["DISABLE_COBROWSER", "DISABLE_CO_BROWSER"])
HUDDLE_ENABLED = _bool_env("HUDDLE_ENABLED", True, aliases=["ENABLE_HUDDLE"], disable_aliases=["DISABLE_HUDDLE"])
AUTOPLAY_ENABLED = _bool_env("AUTOPLAY_ENABLED", True, aliases=["ENABLE_AUTOPLAY"], disable_aliases=["DISABLE_AUTOPLAY"])
LYRICS_ENABLED = _bool_env("LYRICS_ENABLED", True, aliases=["ENABLE_LYRICS"], disable_aliases=["DISABLE_LYRICS"])

# Web dashboard (served by the bot itself)
WEB_UI_ENABLED = _bool_env("WEB_UI_ENABLED", True, aliases=["ENABLE_WEB_UI", "ENABLE_WEBUI", "ENABLE_DASHBOARD"])
WEB_UI_HOST = os.getenv("WEB_UI_HOST", "0.0.0.0")
WEB_UI_PORT = int(os.getenv("WEB_UI_PORT", "8722"))
WEB_UI_PASSWORD = os.getenv("WEB_UI_PASSWORD", "")

# yt-dlp stream resolver shared with Huddle (huddle_music_helper.py)
MUSIC_HELPER_URL = os.getenv("MUSIC_HELPER_URL", "http://127.0.0.1:8731")
