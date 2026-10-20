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
- 🎛️ **DJ booth** (`dj.py`, `webstatic/dj/`) — a two-deck mixer at `/dj/`
  (Huddle's `/dj` opens it for a voice room) with **Easy**, **Moderate** and
  **Advanced** modes:
  - **Stems**: every track can be split into vocals / drums / bass / melody
    (plus guitar and piano with a 6-stem model). Mute, solo or ride each stem,
    one-tap vibes (acapella, instrumental, drums + bass…), stem-targeted FX
    ("echo out the vocal only") and stem-lane waveforms.
  - **Auto DJ**: beat-matched transitions on the bar — stem swap (drums in →
    vocals out → bass swap → vocals in), bass-swap blend, filter fade, echo out
    or cut — with a smart next-track picker that matches Camelot key, tempo and
    energy (keep / build up / wind down) and says why it picked a song.
  - **Decks**: 3-band waveforms with beat grid, CUE / hot cues (saved per
    track), auto loops, loop roll, beat jump, sync + sync lock, tempo ranges,
    key lock, key shift, nudge, quantize, tap-tempo / grid editing.
  - **Mixer & FX**: EQ with kills, one-knob filter, gain, crossfader curves,
    echo / reverb / flanger / filter sweep / trance gate / bitcrush, brake,
    headphone cue (PFL) on a separate output device, master limiter.
  - **Go live** streams the master mix into the Huddle voice room; **Rec**
    saves the mix to a file; **MIDI learn** maps any controller; keyboard
    shortcuts.
  - **Line in** brings in another audio source — a DJ app such as djay,
    Serato or rekordbox playing TIDAL (with its own stem features) through a
    loopback device, a controller's sound card or a turntable — so it is
    mixed, recorded and sent live to the room like the decks. The dialog
    shows the loopback setup for macOS (BlackHole), Windows (VB-Cable /
    Stereo Mix) and Linux (PipeWire/Pulse null sink).
  - Library: search songs, paste YouTube links, import Spotify playlists and
    albums (each becomes a crate; "Prepare all" downloads, analyses and splits
    the whole crate ahead of the set), add from the local music folder, or
    upload your own files and stems.
- Rooms work with **or without** Discord — `deeppixel.online/watch` creates a
  shareable room directly.

## Setup

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

### DJ booth stems (optional)

Stem separation uses [Demucs](https://github.com/adefossez/demucs) and runs on
the server, one track at a time (≈1× real time on a modern CPU, much faster on
a CUDA GPU). It pulls in PyTorch, so keep it in its own virtualenv:

```bash
python3 -m venv stems-env
./stems-env/bin/pip install --index-url https://download.pytorch.org/whl/cpu torch torchaudio
./stems-env/bin/pip install -r requirements-stems.txt
```

Then set in `.env`:

| Variable | Default | Meaning |
| --- | --- | --- |
| `DJ_DEMUCS_PYTHON` | the bot's Python | interpreter that has Demucs (`stems-env/bin/python`) |
| `DJ_STEMS_MODEL` | `htdemucs` | `htdemucs_ft` (slower, cleaner) or `htdemucs_6s` (adds guitar + piano) |
| `DJ_STEMS_DEVICE` | auto | `cpu` or `cuda` |
| `DJ_STEMS_JOBS` | min(4, CPUs) | Demucs worker threads |
| `DJ_CACHE_DIR` | `dj_cache/` | where prepared tracks, waveforms and stems live |

Without Demucs the booth still works with full tracks, and stems you already
own (stem packs, purchased stems) can be uploaded per track from its ⋯ menu —
one at a time, or a whole pack at once ("Song (Vocals).wav", "03 DRUMS.flac",
"Keys.aif"… are recognised from the file names).

### Preparing playlists ahead of a set

`dj_import.py` fills the booth library without opening the browser, e.g. to
download, analyse and split several Spotify playlists overnight:

```bash
./env/bin/python dj_import.py https://open.spotify.com/playlist/AAA https://open.spotify.com/playlist/BBB
```

Each playlist becomes a crate. Spotify links need `SPOTIFY_CLIENT_ID` and
`SPOTIFY_CLIENT_SECRET`; the audio comes from the same YouTube matching as
`/play`. Use `--no-stems` to skip separation or `--metadata-only` to create the
crates and prepare them later from the booth. Run it while the bot is stopped
(it refuses otherwise, since both write `dj_cache/index.json`) — with the bot
running, use "Prepare all" on a crate in the booth instead.

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
