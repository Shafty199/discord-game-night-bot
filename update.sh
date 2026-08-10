#!/bin/bash
set -e

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
APP_DIR="${APP_DIR:-$SCRIPT_DIR}"
SERVICE_NAME="${SERVICE_NAME:-game-night-bot}"

echo "========================================"
echo "Updating Game Night Bot..."
echo "========================================"

cd "$APP_DIR"

echo
echo "Pulling latest changes from GitHub..."
git pull --ff-only origin main

echo
echo "Installing Python packages..."
"$APP_DIR/venv/bin/pip" install -r requirements.txt

echo
echo "Restarting bot..."
sudo systemctl restart "$SERVICE_NAME"

echo
echo "Waiting for startup..."
sleep 5

if sudo systemctl is-active --quiet "$SERVICE_NAME"; then
    echo
    echo "✓ Game Night Bot is running successfully."
    sudo systemctl status "$SERVICE_NAME" --no-pager
else
    echo
    echo "✗ Game Night Bot failed to start."
    echo
    echo "Recent logs:"
    sudo journalctl -u "$SERVICE_NAME" -n 50 --no-pager
    exit 1
fi

echo
echo "========================================"
echo "Update complete!"
echo "========================================"
