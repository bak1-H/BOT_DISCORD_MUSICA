#!/usr/bin/env bash
# Instalador del bot de musica para Linux (Ubuntu/Lubuntu/Debian).
# Uso:  bash install_linux.sh
set -euo pipefail

APP_DIR="$HOME/BOT_DISCORD_MUSICA"
REPO="https://github.com/bak1-H/BOT_DISCORD_MUSICA"

echo "==> [1/5] Instalando dependencias del sistema (ffmpeg, python, node, git)..."
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip ffmpeg git nodejs npm

echo "==> [2/5] Clonando / actualizando el repositorio en $APP_DIR ..."
if [ -d "$APP_DIR/.git" ]; then
    git -C "$APP_DIR" pull --ff-only
else
    git clone "$REPO" "$APP_DIR"
fi

cd "$APP_DIR"

echo "==> [3/5] Creando entorno virtual de Python..."
python3 -m venv .venv
./.venv/bin/pip install --upgrade pip

echo "==> [4/5] Instalando dependencias de Python..."
./.venv/bin/pip install -r requirements.txt
./.venv/bin/pip install -U yt-dlp

echo "==> [5/5] Versiones instaladas:"
node --version
python3 --version
./.venv/bin/yt-dlp --version
ffmpeg -version | head -n 1

echo ""
echo "============================================================"
echo " Instalacion completa."
echo " FALTA crear el archivo .env con tus tokens. Ejecuta:"
echo ""
echo "   nano $APP_DIR/.env"
echo ""
echo " y pega dentro:"
echo "   DISCORD_TOKEN=tu_token_de_discord"
echo "   GENIUS_TOKEN=tu_token_de_genius"
echo ""
echo " Luego instala el servicio para que arranque solo:"
echo "   bash $APP_DIR/install_service.sh"
echo "============================================================"
