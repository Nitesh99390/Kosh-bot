#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CURRENT_USER="$(whoami)"

echo "==> Installing Python 3 & venv..."
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip

echo "==> Creating virtual environment..."
cd "$APP_DIR"
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

if [ ! -f "$APP_DIR/.env" ]; then
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    echo ""
    echo "⚠️  Created .env from .env.example."
    echo "    Edit $APP_DIR/.env with your BOT_TOKEN, DUMP_CHANNEL_ID, and ADMIN_IDS first!"
    echo "    Then run: sudo systemctl restart koshbox"
fi

echo "==> Configuring systemd service..."
SERVICE_FILE="/etc/systemd/system/koshbox.service"
sudo bash -c "cat > $SERVICE_FILE" <<EOF
[Unit]
Description=KoshBox Telegram Inline Dump Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${CURRENT_USER}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/.venv/bin/python ${APP_DIR}/bot.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable koshbox
sudo systemctl restart koshbox

echo ""
echo "✅ KoshBox service installed!"
echo "   Check status : sudo systemctl status koshbox"
echo "   Live logs    : journalctl -u koshbox -f"
