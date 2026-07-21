#!/bin/bash
# DeepPixel Watch Together - Cloudflare + Home Server Setup

echo "DeepPixel Watch Together - Cloudflare Setup"
echo "================================================"

# 1. Nginx kur
echo "[1/7] Installing Nginx..."
sudo apt update
sudo apt install -y nginx

# 2. Nginx config oluştur (Cloudflare uyumlu)
echo "[2/7] Creating Nginx configuration..."
sudo tee /etc/nginx/sites-available/deeppixel.online > /dev/null << 'EOF'
server {
    listen 80;
    server_name deeppixel.online www.deeppixel.online;
    
    # Cloudflare Real IP
    set_real_ip_from 173.245.48.0/20;
    set_real_ip_from 103.21.244.0/22;
    set_real_ip_from 103.22.200.0/22;
    set_real_ip_from 103.31.4.0/22;
    set_real_ip_from 141.101.64.0/18;
    set_real_ip_from 108.162.192.0/18;
    set_real_ip_from 190.93.240.0/20;
    set_real_ip_from 188.114.96.0/20;
    set_real_ip_from 197.234.240.0/22;
    set_real_ip_from 198.41.128.0/17;
    set_real_ip_from 162.158.0.0/15;
    set_real_ip_from 104.16.0.0/13;
    set_real_ip_from 104.24.0.0/14;
    set_real_ip_from 172.64.0.0/13;
    set_real_ip_from 131.0.72.0/22;
    set_real_ip_from 2400:cb00::/32;
    set_real_ip_from 2606:4700::/32;
    set_real_ip_from 2803:f800::/32;
    set_real_ip_from 2405:b500::/32;
    set_real_ip_from 2405:8100::/32;
    set_real_ip_from 2c0f:f248::/32;
    set_real_ip_from 2a06:98c0::/29;
    real_ip_header CF-Connecting-IP;
    
    location / {
        proxy_pass http://127.0.0.1:8722;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header CF-Connecting-IP $http_cf_connecting_ip;
        
        # WebSocket timeout
        proxy_read_timeout 86400;
    }
    
}
EOF

# 3. Nginx config'i aktifleştir
echo "[3/7] Activating Nginx configuration..."
sudo ln -sf /etc/nginx/sites-available/deeppixel.online /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default

# 4. Nginx test et ve restart
echo "[4/7] Restarting Nginx..."
sudo nginx -t
sudo systemctl restart nginx
sudo systemctl enable nginx

echo ""
echo "*** Cloudflare SSL/TLS Settings:"
echo "   1. Cloudflare Dashboard > SSL/TLS > Overview"
echo "   2. Encryption mode: Full (strict)"
echo "   3. Never use Flexible mode for authenticated pages"
echo ""

# 5. Systemd service oluştur
echo "[5/7] Creating systemd service..."
CURRENT_DIR=$(pwd)
PYTHON_PATH="$CURRENT_DIR/env/bin/python"

sudo tee /etc/systemd/system/deeppixel-watch.service > /dev/null << EOF
[Unit]
Description=DeepPixel Music and Watch Together Bot
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$CURRENT_DIR
Environment="PATH=$CURRENT_DIR/env/bin"
ExecStart=$PYTHON_PATH music.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# 6. Service başlat
echo "[6/7] Starting service..."
sudo systemctl daemon-reload
sudo systemctl enable deeppixel-watch
sudo systemctl start deeppixel-watch

# 7. Firewall ayarları (varsa)
if command -v ufw &> /dev/null; then
    echo "[7/7] Configuring firewall..."
    sudo ufw allow 80/tcp
    sudo ufw allow 443/tcp
fi

echo ""
echo "=================================================="
echo "           INSTALLATION COMPLETE!               "
echo "=================================================="
echo ""
echo "Cloudflare Checklist:"
echo "   [*] Go to Cloudflare Dashboard"
echo "   [*] DNS Records:"
echo "      - A Record: deeppixel.online -> $(curl -s ifconfig.me)"
echo "      - Proxy status: Proxied (orange cloud) - ON"
echo "   [*] SSL/TLS > Overview:"
echo "      - Encryption mode: Flexible veya Full"
echo "   [*] Speed > Optimization:"
echo "      - WebSockets: ON (enable)"
echo ""
echo "Web Server: https://deeppixel.online"
echo "Service status: sudo systemctl status deeppixel-watch"
echo "Logs: sudo journalctl -u deeppixel-watch -f"
echo ""
echo "Troubleshooting:"
echo "   - Nginx logs: sudo tail -f /var/log/nginx/error.log"
echo "   - Service logs: sudo journalctl -u deeppixel-watch -f"
echo ""
echo "Update .env file:"
echo "   WEB_SERVER_URL=https://deeppixel.online"
echo ""
echo "Restart bot: ./env/bin/python music.py"
echo ""
