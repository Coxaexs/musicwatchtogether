#!/bin/bash
# Adds the deeppixel.online/watch/ route (with websocket support) to nginx.
# Run with: sudo bash setup_watch_nginx.sh
set -e

CONF=/etc/nginx/sites-available/ultimate-fix

if grep -q "location /watch/" "$CONF"; then
    echo "/watch/ route already present in $CONF"
else
    python3 - "$CONF" <<'EOF'
import sys
path = sys.argv[1]
src = open(path).read()
anchor = "    # 6. ANA SAYFA: DASHBOARD (Homepage) - En sonda olmalı"
block = """    # 5.6. WATCH TOGETHER / REELSTOGETHER (music bot aiohttp, websocket)
    location = /watch {
        return 301 $scheme://$http_host/watch/;
    }

    location /watch/ {
        proxy_pass http://127.0.0.1:8722/watch/;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 86400;
        proxy_buffering off;
    }


"""
assert anchor in src, "anchor comment not found in nginx config"
open(path, "w").write(src.replace(anchor, block + anchor))
print("nginx config updated")
EOF
fi

nginx -t
systemctl reload nginx
echo "✅ deeppixel.online/watch/ is live"
