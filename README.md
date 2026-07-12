# Music + Watch Together bot

A Discord music bot (`music.py`) with a browser-based control dashboard and a
**Watch Together** stack served by the same process.

## Features

- 🎵 **Music** — YouTube / Spotify / local library playback, queue, lyrics,
  24/7 mode, web dashboard (`webui.py`).
- 🎬 **Watch Together** (`watchtogether.py`) — synced video rooms with chat.
  Add anything yt-dlp resolves (YouTube, Shorts, Reels, TikTok, Twitter…).
  Long videos stream while they download (progressive HLS). Per-room settings
  for quality, SponsorBlock, and adblock.
- 📱 **ReelsTogether** — a synced, swipeable short-form feed with backward
  navigation through recent reels and a per-room taste algorithm. Watch and
  Reels rooms share a hard 10-video download cache.
- 🌐 **Shared co-browser** (`cobrowser.py`) — when yt-dlp can't grab something,
  the room opens a real Firefox on the server, streamed to everyone (MPEG-TS
  over websocket) with one person driving. AdGuard adblock built in.
- Rooms work with **or without** Discord — `deeppixel.online/watch` creates a
  shareable room directly.

## Setup

```bash
python3 -m venv env && ./env/bin/pip install -r requirements.txt
cp .env.example .env         # fill in DISCORD_TOKEN (+ optional Spotify)
./env/bin/python webstatic/... # see webstatic/README.md for adguard.xpi
./start_bot.sh
```

The web stack listens on `WEB_UI_PORT` (default 8722) and is meant to sit
behind a reverse proxy — see `setup_watch_nginx.sh` and `ultimate-fix.conf`
for the `/watch/` + websocket routing.

## Notes

Secrets (`.env`, `*.key`, `friend.conf`), the music library, caches, and
per-deployment runtime state are gitignored. Configure everything through
`.env` — see `.env.example`.
