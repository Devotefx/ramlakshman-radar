# DHAN ALPHA — Ubuntu Server Deployment Guide
# Tested on Ubuntu 22.04 LTS / 24.04 LTS

## ─────────────────────────────────────────────────────────────
## 1. SYSTEM SETUP
## ─────────────────────────────────────────────────────────────

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install core deps
sudo apt install -y python3.11 python3.11-venv python3.11-dev \
  git nginx redis-server postgresql postgresql-contrib \
  build-essential libpq-dev curl supervisor certbot python3-certbot-nginx

# Install Node.js 20 (for any frontend build tools)
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs
```

---

## ─────────────────────────────────────────────────────────────
## 2. CLONE & CONFIGURE
## ─────────────────────────────────────────────────────────────

```bash
# Clone repo
git clone https://github.com/YOUR_ORG/dhan-alpha.git /opt/dhan-alpha
cd /opt/dhan-alpha

# Create Python virtualenv
python3.11 -m venv venv
source venv/bin/activate

# Install Python dependencies
pip install --upgrade pip
pip install -r requirements.txt
```

### requirements.txt
```
fastapi==0.111.0
uvicorn[standard]==0.30.1
websockets==12.0
aiohttp==3.9.5
redis[hiredis]==5.0.4
asyncpg==0.29.0
sqlalchemy[asyncio]==2.0.30
python-dotenv==1.0.1
httpx==0.27.0
pydantic==2.7.1
python-jose[cryptography]==3.3.0
passlib[bcrypt]==1.7.4
```

### Create .env file
```bash
cat > /opt/dhan-alpha/.env << 'EOF'
# Dhan API credentials
DHAN_CLIENT_ID=YOUR_CLIENT_ID
DHAN_ACCESS_TOKEN=YOUR_ACCESS_TOKEN

# Database
DATABASE_URL=postgresql+asyncpg://dhan:STRONG_PASSWORD@localhost/dhan_alpha
REDIS_URL=redis://localhost:6379/0

# App
SECRET_KEY=GENERATE_WITH_openssl_rand_hex_32
APP_ENV=production
ALLOWED_ORIGINS=https://yourdomain.com

# Logging
LOG_LEVEL=INFO
EOF
chmod 600 /opt/dhan-alpha/.env
```

---

## ─────────────────────────────────────────────────────────────
## 3. DATABASE SETUP
## ─────────────────────────────────────────────────────────────

```bash
# PostgreSQL setup
sudo -u postgres psql << 'EOF'
CREATE USER dhan WITH PASSWORD 'STRONG_PASSWORD';
CREATE DATABASE dhan_alpha OWNER dhan;
GRANT ALL PRIVILEGES ON DATABASE dhan_alpha TO dhan;
\q
EOF

# Run schema
sudo -u postgres psql -d dhan_alpha -f /opt/dhan-alpha/docs/schema.sql

# Redis — tune for low latency
sudo tee -a /etc/redis/redis.conf << 'EOF'
maxmemory 1gb
maxmemory-policy allkeys-lru
save ""
appendonly no
tcp-keepalive 60
EOF
sudo systemctl restart redis-server
```

---

## ─────────────────────────────────────────────────────────────
## 4. SYSTEMD SERVICE
## ─────────────────────────────────────────────────────────────

```bash
sudo tee /etc/systemd/system/dhan-alpha.service << 'EOF'
[Unit]
Description=DHAN ALPHA Options Intelligence Terminal
After=network.target redis.service postgresql.service

[Service]
Type=exec
User=ubuntu
WorkingDirectory=/opt/dhan-alpha/backend
Environment="PATH=/opt/dhan-alpha/venv/bin"
EnvironmentFile=/opt/dhan-alpha/.env
ExecStart=/opt/dhan-alpha/venv/bin/uvicorn main:app \
    --host 0.0.0.0 \
    --port 8000 \
    --workers 1 \
    --loop uvloop \
    --http h11 \
    --log-level info \
    --access-log
Restart=always
RestartSec=5
StandardOutput=append:/var/log/dhan-alpha/app.log
StandardError=append:/var/log/dhan-alpha/error.log

# Security hardening
NoNewPrivileges=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
EOF

# Create log dir
sudo mkdir -p /var/log/dhan-alpha
sudo chown ubuntu:ubuntu /var/log/dhan-alpha

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable dhan-alpha
sudo systemctl start dhan-alpha
sudo systemctl status dhan-alpha
```

---

## ─────────────────────────────────────────────────────────────
## 5. NGINX REVERSE PROXY
## ─────────────────────────────────────────────────────────────

```bash
sudo tee /etc/nginx/sites-available/dhan-alpha << 'EOF'
upstream dhan_backend {
    server 127.0.0.1:8000;
    keepalive 32;
}

server {
    listen 80;
    server_name yourdomain.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name yourdomain.com;

    ssl_certificate     /etc/letsencrypt/live/yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/yourdomain.com/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256;

    # Frontend static files
    root /opt/dhan-alpha/frontend;
    index index.html;

    # API
    location /api/ {
        proxy_pass         http://dhan_backend;
        proxy_http_version 1.1;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_buffering    off;
    }

    # WebSocket — CRITICAL settings
    location /ws {
        proxy_pass          http://dhan_backend;
        proxy_http_version  1.1;
        proxy_set_header    Upgrade    $http_upgrade;
        proxy_set_header    Connection "Upgrade";
        proxy_set_header    Host       $host;
        proxy_read_timeout  86400s;   # 24h keepalive
        proxy_send_timeout  86400s;
        proxy_buffering     off;
    }

    # Static files
    location / {
        try_files $uri $uri/ /index.html;
        expires 1h;
        add_header Cache-Control "public, must-revalidate";
    }

    # Logging
    access_log /var/log/nginx/dhan-alpha.access.log;
    error_log  /var/log/nginx/dhan-alpha.error.log;
}
EOF

sudo ln -s /etc/nginx/sites-available/dhan-alpha /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

# SSL cert
sudo certbot --nginx -d yourdomain.com
```

---

## ─────────────────────────────────────────────────────────────
## 6. LOG ROTATION
## ─────────────────────────────────────────────────────────────

```bash
sudo tee /etc/logrotate.d/dhan-alpha << 'EOF'
/var/log/dhan-alpha/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    create 0640 ubuntu ubuntu
    postrotate
        systemctl kill -s HUP dhan-alpha.service
    endscript
}
EOF
```

---

## ─────────────────────────────────────────────────────────────
## 7. MONITORING (optional but recommended)
## ─────────────────────────────────────────────────────────────

```bash
# Install htop, netstat tools
sudo apt install -y htop nethogs iotop

# Quick health check script
cat > /opt/dhan-alpha/health_check.sh << 'SCRIPT'
#!/bin/bash
STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/health)
if [ "$STATUS" != "200" ]; then
    echo "$(date): ALERT — DHAN ALPHA down (HTTP $STATUS)" >> /var/log/dhan-alpha/health.log
    systemctl restart dhan-alpha
fi
SCRIPT
chmod +x /opt/dhan-alpha/health_check.sh

# Cron: health check every 2 min
(crontab -l 2>/dev/null; echo "*/2 * * * * /opt/dhan-alpha/health_check.sh") | crontab -
```

---

## ─────────────────────────────────────────────────────────────
## 8. FIREWALL
## ─────────────────────────────────────────────────────────────

```bash
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'
sudo ufw deny 8000    # block direct backend access
sudo ufw deny 6379    # block Redis from outside
sudo ufw deny 5432    # block PostgreSQL from outside
sudo ufw enable
sudo ufw status
```

---

## ─────────────────────────────────────────────────────────────
## 9. SERVER SPECS (minimum recommended)
## ─────────────────────────────────────────────────────────────

| Component | Minimum        | Recommended       |
|-----------|----------------|-------------------|
| CPU       | 2 vCPU         | 4 vCPU            |
| RAM       | 4 GB           | 8 GB              |
| Disk      | 40 GB SSD      | 100 GB NVMe SSD   |
| Network   | 100 Mbps       | 1 Gbps            |
| OS        | Ubuntu 22.04   | Ubuntu 22.04 LTS  |
| Provider  | Any            | AWS (ap-south-1 Mumbai) ← closest to NSE |

**Critical**: Deploy in **Mumbai region (ap-south-1)** for lowest latency to Dhan API servers.
AWS EC2 c6i.xlarge or Hetzner dedicated server in India gives best latency.

---

## ─────────────────────────────────────────────────────────────
## 10. DHAN API TOKEN RENEWAL
## ─────────────────────────────────────────────────────────────

Dhan access tokens expire daily. Automate renewal:

```bash
# Create token refresh script
cat > /opt/dhan-alpha/refresh_token.py << 'PY'
"""
Auto-refresh Dhan access token.
Run via cron at 8:30 AM IST before market open.
"""
import os
import requests
from dotenv import load_dotenv, set_key

load_dotenv()

# Dhan token refresh (using TOTP / session approach)
# Visit https://dhanhq.co/docs/v2/access-token for exact flow
# Store new token in .env
PY

# Cron: refresh token at 8:30 AM IST (3:00 UTC)
(crontab -l 2>/dev/null; echo "0 3 * * 1-5 /opt/dhan-alpha/venv/bin/python /opt/dhan-alpha/refresh_token.py") | crontab -
```

---

## ─────────────────────────────────────────────────────────────
## QUICK START COMMANDS
## ─────────────────────────────────────────────────────────────

```bash
# Start all services
sudo systemctl start redis-server postgresql dhan-alpha nginx

# View live logs
sudo journalctl -u dhan-alpha -f --no-pager

# Restart backend
sudo systemctl restart dhan-alpha

# Test WebSocket
wscat -c wss://yourdomain.com/ws

# Test API
curl https://yourdomain.com/api/market-summary | python3 -m json.tool
```
