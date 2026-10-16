# Music + Watch Together bot

A Discord music bot (`music.py`) with a browser-based control dashboard and a
**Watch Together** stack served by the same process.

## Features

- 🎵 **Music** — YouTube / Spotify / local library playback, queue, lyrics,
  24/7 mode, web dashboard (`webui.py`). `/settings` exposes every safe
  per-server playback option plus this channel's WatchTogether preferences;
  the dashboard mirrors the playback controls.
- 🎬 **Watch Together** (`watchtogether.py`) — synced video rooms with chat.
  Add anything yt-dlp resolves (YouTube, Shorts, Reels, TikTok, Twitter…).
  Long videos stream while they download (progressive HLS). Per-room settings
  for quality, SponsorBlock, and adblock. Rooms have host, moderator, and viewer
  roles; moderators manage playback and destructive queue actions, while the
  host can assign roles.
- 📚 **Collaborative room playlists** — participants can save the current
  queue as a persistent shared playlist and load it together later. Moderators
  can remove obsolete room playlists.
- 🎧 **Spotify playlist import** — paste a Spotify playlist or album link in
  the dashboard library to create a personal playlist with up to 200 tracks,
  Spotify artwork, durations, and automatic duplicate-name handling.
- 📱 **ReelsTogether** — a synced, swipeable short-form feed with backward
  navigation through recent reels and a per-room taste algorithm. Watch and
  Reels rooms share a hard 10-video download cache.
- 🌐 **Shared co-browser** (`cobrowser.py`) — when yt-dlp can't grab something,
  the room opens a real Firefox on the server, streamed to everyone (MPEG-TS
  over websocket) with one person driving. AdGuard adblock built in.
- Rooms work with **or without** Discord — `deeppixel.online/watch` creates a
  shareable room directly.

## Quick Start with Docker (GHCR)

The fastest way to run the bot is with Docker using the prebuilt image from the GitHub Container Registry:

### Using Docker Compose (Recommended)

1. Use the provided [docker-compose.yml](file:///mnt/harddisk/data/bot/musicbot-main/docker-compose.yml):

```yaml
services:
  musicbot:
    image: ghcr.io/coxaexs/musicwatchtogether:latest
    container_name: musicbot
    restart: unless-stopped
    ports:
      - "8722:8722"
    environment:
      - DISCORD_TOKEN=your_discord_bot_token_here
      - WEB_UI_PASSWORD=your_dashboard_password_here
      - WEB_SERVER_URL=https://your-domain-or-ip:8722
      - DJ_ROLE_NAME=DJ
      # Optional: Spotify credentials
      # - SPOTIFY_CLIENT_ID=your_id
      # - SPOTIFY_CLIENT_SECRET=your_secret
    volumes:
      - ./data:/data
```

2. Start the container:

```bash
docker compose up -d
```

All persistent data (database, playlists, settings, caches, and downloaded tracks) is automatically stored in `./data`.

### Using `docker run`

```bash
docker run -d \
  --name musicbot \
  --restart unless-stopped \
  -p 8722:8722 \
  -e DISCORD_TOKEN="your_discord_bot_token_here" \
  -e WEB_UI_PASSWORD="your_dashboard_password_here" \
  -e WEB_SERVER_URL="https://your-domain-or-ip:8722" \
  -v $(pwd)/data:/data \
  ghcr.io/coxaexs/musicwatchtogether:latest
```

## Manual Setup (Without Docker)

Python 3.11 or newer is required.

```bash
python3 -m venv env && ./env/bin/pip install -r requirements.lock
cp .env.example .env         # fill in DISCORD_TOKEN (+ optional Spotify)
./env/bin/python webstatic/... # see webstatic/README.md for adguard.xpi
./start_bot.sh
```

The web stack listens on `WEB_UI_PORT` (default 8722) and is meant to sit
behind a reverse proxy — see `setup_watch_nginx.sh` and `ultimate-fix.conf`
for the `/watch/` + websocket routing.

Set a strong `WEB_UI_PASSWORD` whenever `WEB_UI_HOST` is not loopback. Room
and dashboard invitation links exchange their fragment token for a scoped,
HttpOnly session cookie. Room roles use a separate private member cookie, so a
participant cannot claim a privileged role by changing browser-side state. URL
fragments are never sent to the reverse proxy.

`GET /healthz` provides public liveness/readiness data for a reverse proxy or
service manager. `GET /api/metrics` exposes Prometheus text metrics and follows
the dashboard API authentication policy.

## Development

```bash
./env/bin/python -m unittest discover -s tests -v
./env/bin/ruff check .
```

Runtime dependency ranges are maintained in `requirements.txt`. CI and
deployments use the exact versions in `requirements.lock`; regenerate it with
`pip-compile --generate-hashes --output-file=requirements.lock requirements-dev.txt`
on Linux after an intentional dependency update (the lock includes the
Linux-only `python-xlib` co-browser dependency).

## Notes

Secrets (`.env`, `*.key`, `friend.conf`), the music library, caches, and
per-deployment runtime state are gitignored. Configure everything through
`.env` — see `.env.example`.
