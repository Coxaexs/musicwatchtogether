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

# Supported local file formats
SUPPORTED_FORMATS = ['.mp3', '.wav', '.ogg', '.flac', '.m4a', '.aac', '.opus', '.webm']

# Optional media library integrations
# Path where downloaded music files are stored for external media servers (Navidrome/Lidarr)
MUSIC_LIBRARY_PATH = os.getenv("MUSIC_LIBRARY_PATH", "/mnt/harddisk/data/bot/musicbot-main/musics")
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

# Web dashboard (served by the bot itself)
WEB_UI_ENABLED = os.getenv("WEB_UI_ENABLED", "1") == "1"
WEB_UI_HOST = os.getenv("WEB_UI_HOST", "0.0.0.0")
WEB_UI_PORT = int(os.getenv("WEB_UI_PORT", "8722"))
WEB_UI_PASSWORD = os.getenv("WEB_UI_PASSWORD", "")

# yt-dlp stream resolver shared with Huddle (huddle_music_helper.py)
MUSIC_HELPER_URL = os.getenv("MUSIC_HELPER_URL", "http://127.0.0.1:8731")
