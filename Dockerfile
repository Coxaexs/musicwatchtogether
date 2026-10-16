# syntax=docker/dockerfile:1
FROM python:3.13-slim-bookworm

# Prevent python from writing pyc files and buffering stdout/stderr
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data \
    COBROWSER_PROFILE_ROOT=/tmp/wt-rooms \
    WEB_UI_HOST=0.0.0.0 \
    WEB_UI_PORT=8722

# Install system dependencies:
# - ffmpeg: audio and video transcoding / playback
# - libopus0, libopus-dev: Discord voice Opus audio codec
# - curl, ca-certificates: health checks, TLS certs, asset fetching
# - dumb-init: lightweight init system for proper signal forwarding & process reaping
# - xvfb, xfwm4, firefox-esr: shared co-browser support
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libopus0 \
    libopus-dev \
    curl \
    ca-certificates \
    dumb-init \
    xvfb \
    xfwm4 \
    firefox-esr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy dependency specifications first for Docker layer caching
COPY requirements.lock requirements.txt ./

# Install python dependencies from lockfile
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.lock

# Pre-fetch AdGuard Firefox extension for the co-browser
RUN mkdir -p /app/webstatic && \
    curl -fsSL "https://addons.mozilla.org/firefox/downloads/latest/adguard-adblocker/latest.xpi" \
    -o /app/webstatic/adguard.xpi || true

# Copy application source code
COPY . /app

# Setup entrypoint script
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Create persistent data directory and declare volume
RUN mkdir -p /data
VOLUME ["/data"]

# Expose web dashboard / watch together port
EXPOSE 8722

# Container healthcheck using the built-in /healthz endpoint
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:${WEB_UI_PORT:-8722}/healthz || exit 1

ENTRYPOINT ["/usr/bin/dumb-init", "--", "/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "music.py"]
