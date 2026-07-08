# webstatic

Vendored, self-hosted browser assets served under `/watch/static/`.

- `jsmpeg.min.js` — MPEG-TS player for the shared co-browser stream
- `hls.min.js` — HLS player for progressive (stream-while-downloading) videos
- `adguard.xpi` — **not committed** (17 MB). Fetch it before using the shared
  browser's adblock:

  ```bash
  curl -sL "https://addons.mozilla.org/firefox/downloads/latest/adguard-adblocker/latest.xpi" \
    -o webstatic/adguard.xpi
  ```
