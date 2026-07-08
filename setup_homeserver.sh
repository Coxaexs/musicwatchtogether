#!/bin/bash
# 🏠 Home Server Kurulum Scripti - deeppixel.online

echo "🔧 DeepPixel Watch Together - Home Server Setup"
echo "================================================"

# 1. Nginx kur
echo "📦 Nginx kuruluyor..."
sudo apt update
sudo apt install -y nginx

# 2. Certbot kur (SSL için)
echo "🔒 Certbot kuruluyor..."
sudo apt install -y certbot python3-certbot-nginx

# 3. Nginx config oluştur
echo "⚙️ Nginx config oluşturuluyor..."
sudo tee /etc/nginx/sites-available/deeppixel.online > /dev/null << 'EOF'
server {
    listen 80;
    server_name deeppixel.online www.deeppixel.online;
    
    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
    
    location /socket.io {
        proxy_pass http://127.0.0.1:5000/socket.io;
        proxy_http_version 1.1;
        proxy_buffering off;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "Upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
EOF

# 4. Nginx config'i aktifleştir
echo "✅ Nginx config aktifleştiriliyor..."
sudo ln -sf /etc/nginx/sites-available/deeppixel.online /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default

# 5. Nginx test et ve restart
echo "🔄 Nginx yeniden başlatılıyor..."
sudo nginx -t
sudo systemctl restart nginx
sudo systemctl enable nginx

# 6. SSL Sertifikası al
echo "🔐 SSL sertifikası alınıyor..."
echo "📧 Email adresinizi girin (Let's Encrypt için):"
read email
sudo certbot --nginx -d deeppixel.online -d www.deeppixel.online --non-interactive --agree-tos -m "$email"

# 7. Systemd service oluştur
echo "🚀 Systemd service oluşturuluyor..."
CURRENT_DIR=$(pwd)
PYTHON_PATH="$CURRENT_DIR/env/bin/python"

sudo tee /etc/systemd/system/deeppixel-watch.service > /dev/null << EOF
[Unit]
Description=DeepPixel Watch Together Server
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$CURRENT_DIR
Environment="PATH=$CURRENT_DIR/env/bin"
ExecStart=$PYTHON_PATH web_player.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# 8. Service başlat
echo "▶️ Service başlatılıyor..."
sudo systemctl daemon-reload
sudo systemctl enable deeppixel-watch
sudo systemctl start deeppixel-watch

# 9. Firewall ayarları (varsa)
if command -v ufw &> /dev/null; then
    echo "🔥 Firewall ayarlanıyor..."
    sudo ufw allow 80/tcp
    sudo ufw allow 443/tcp
    sudo ufw allow 5000/tcp
fi

echo ""
echo "✅ ✅ ✅ KURULUM TAMAMLANDI! ✅ ✅ ✅"
echo ""
echo "🌐 Web Server: https://deeppixel.online"
echo "📊 Service durumu: sudo systemctl status deeppixel-watch"
echo "📝 Loglar: sudo journalctl -u deeppixel-watch -f"
echo ""
echo "🎬 .env dosyasındaki WEB_SERVER_URL'i güncellemeyi unutma:"
echo "   WEB_SERVER_URL=https://deeppixel.online"
echo ""
echo "🤖 Botu yeniden başlat: ./env/bin/python music_bot.py"
echo ""
