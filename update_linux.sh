#!/usr/bin/env bash
# Actualiza el bot: baja cambios del repo, actualiza yt-dlp y reinicia el servicio.
# Uso:  bash update_linux.sh
set -euo pipefail

APP_DIR="$HOME/BOT_DISCORD_MUSICA"
SERVICE_NAME="discord-musica"

cd "$APP_DIR"

echo "==> Bajando ultimos cambios..."
git pull --ff-only

echo "==> Actualizando dependencias y yt-dlp..."
./.venv/bin/pip install -r requirements.txt
./.venv/bin/pip install -U yt-dlp

echo "==> Reiniciando el servicio..."
sudo systemctl restart "$SERVICE_NAME"

echo "==> Listo. Estado:"
sudo systemctl status "$SERVICE_NAME" --no-pager
