#!/usr/bin/env bash
# Instala el bot como servicio systemd para que arranque solo al encender
# y se reinicie si se cae o si vuelve la luz tras un corte.
# Uso:  bash install_service.sh
set -euo pipefail

# Detecta el directorio del repo (donde vive este script), funcione donde funcione.
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_USER="$(whoami)"
SERVICE_NAME="discord-musica"

if [ ! -f "$APP_DIR/.env" ]; then
    echo "ERROR: no existe $APP_DIR/.env"
    echo "Crealo primero con tus tokens:  nano $APP_DIR/.env"
    exit 1
fi

echo "==> Creando servicio systemd ($SERVICE_NAME)..."
sudo tee "/etc/systemd/system/${SERVICE_NAME}.service" > /dev/null <<EOF
[Unit]
Description=Bot de Musica Discord
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/.venv/bin/python ${APP_DIR}/bot.py
Restart=always
RestartSec=10
# Evita el health server en el puerto 8080 (solo lo necesita Fly.io)
Environment=PORT=8099

[Install]
WantedBy=multi-user.target
EOF

echo "==> Activando y arrancando el servicio..."
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"

echo ""
echo "============================================================"
echo " Servicio instalado y corriendo."
echo ""
echo " Comandos utiles:"
echo "   sudo systemctl status $SERVICE_NAME      # ver estado"
echo "   journalctl -u $SERVICE_NAME -f           # ver logs en vivo"
echo "   sudo systemctl restart $SERVICE_NAME     # reiniciar"
echo "   sudo systemctl stop $SERVICE_NAME        # detener"
echo "============================================================"
