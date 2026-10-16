#!/bin/bash
set -e

# Data directory for persistent storage
DATA_DIR="${DATA_DIR:-/data}"

# Ensure data subdirectories exist
mkdir -p "${DATA_DIR}" \
         "${DATA_DIR}/musics" \
         "${DATA_DIR}/watch_cache" \
         "${DATA_DIR}/reels_cache" \
         "${DATA_DIR}/stems_cache" \
         "${DATA_DIR}/playlist_covers" \
         "${DATA_DIR}/lyrics" \
         "${DATA_DIR}/logs"

# Ensure X11 socket directory exists for virtual display
mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix

# Load .env file from data directory if present
if [ -f "${DATA_DIR}/.env" ]; then
    set -a
    # shellcheck disable=SC1090
    source "${DATA_DIR}/.env"
    set +a
fi

# Configuration checks & helpful warnings
if [ -z "$DISCORD_TOKEN" ]; then
    echo "⚠️  WARNING: DISCORD_TOKEN is not set!"
    echo "   Please set DISCORD_TOKEN in your environment or ${DATA_DIR}/.env"
    echo "   Example: docker run -e DISCORD_TOKEN=\"your_bot_token\" ..."
fi

if [ "${WEB_UI_HOST:-0.0.0.0}" != "127.0.0.1" ] && [ -z "$WEB_UI_PASSWORD" ]; then
    echo "⚠️  WARNING: WEB_UI_PASSWORD is not set."
    echo "   When binding beyond localhost (${WEB_UI_HOST:-0.0.0.0}), the web UI requires"
    echo "   WEB_UI_PASSWORD for authentication. Set it via -e WEB_UI_PASSWORD=\"...\""
fi

exec "$@"
