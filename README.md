# Music + Watch Together bot

A Discord music bot (`music.py`) with a browser-based control dashboard and a
**Watch Together** stack served by the same process.

## Features

- 🎵 **Music** — YouTube / Spotify / local library playback, queue, lyrics,
  24/7 mode, web dashboard (`webui.py`). `/settings` exposes every safe
  per-server playback option; the dashboard mirrors the playback controls.
- 🎧 **Spotify playlist import** — paste a Spotify playlist or album link in
  the dashboard library to create a personal playlist with up to 200 tracks,
  Spotify artwork, durations, and automatic duplicate-name handling.

### Watch Together and Reels Together (Hoffle only)

These are activities of a [Hoffle](https://hoffle.online) voice room, not
Discord commands. Hoffle checks who someone is and that they may be in the
room, then asks the dashboard for a one-time ticket
(`POST /api/admin/watch-ticket`, master session); the page trades it for a
session. There is one room per voice room and mode, and nothing under
`/watch/` works without a ticket from Hoffle.

- 🎬 **Watch Together** (`watchtogether.py`, `webstatic/watch.*`) — one synced
  player for the whole voice room. Search YouTube right in the panel or paste
  anything yt-dlp resolves; long videos stream while they download (HLS).
  Reactions, saved room playlists, vote-to-skip, and per-room quality,
  SponsorBlock and adblock. The host (whoever started it) and Hoffle's
  moderators can lock controls and make others moderators of the room.
- 📱 **Reels Together** (`reels_brain.py`, `webstatic/reels.*`) — a synced,
  swipeable Shorts feed. It pulls real Shorts from hashtag and creator feeds,
  learns each person's taste from likes, "show less", full watches and fast
  skips (your taste follows you between rooms), and takes turns choosing for
  everyone in the room. Auto-scroll, swipe back, "more from this creator".
- 🌐 **Shared co-browser** (`cobrowser.py`) — when yt-dlp can't grab something,
  the room opens a real Firefox on the server, streamed to everyone (MPEG-TS
  over websocket) with one person driving. AdGuard adblock built in.

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
