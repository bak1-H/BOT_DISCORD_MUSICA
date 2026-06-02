# Correr el bot en un servidor Linux (casa)

Guía para dejar el bot corriendo 24/7 en un mini PC con Lubuntu/Ubuntu/Debian.

**Por qué en casa:** al usar tu IP residencial, YouTube no aplica el bloqueo "bot-check"
que sí afecta a los datacenters (Fly.io, AWS, etc.). El bot funciona sin pelear con cookies.

---

## Requisitos

- Linux (Ubuntu/Lubuntu/Debian) con acceso `sudo`
- Acceso por SSH o terminal local
- Tus tokens: `DISCORD_TOKEN` y `GENIUS_TOKEN`

---

## Instalación (paso a paso)

Conectado por SSH al mini PC, ejecutá:

### 1. Descargar e instalar dependencias + el bot

```bash
# Bajar el script de instalación y ejecutarlo
curl -fsSL https://raw.githubusercontent.com/bak1-H/BOT_DISCORD_MUSICA/main/install_linux.sh -o install_linux.sh
bash install_linux.sh
```

Esto instala ffmpeg, Python, Node y clona el repo en `~/BOT_DISCORD_MUSICA`.

### 2. Crear el archivo `.env` con los tokens

```bash
nano ~/BOT_DISCORD_MUSICA/.env
```

Pegá adentro (con tus valores reales):

```env
DISCORD_TOKEN=tu_token_de_discord
GENIUS_TOKEN=tu_token_de_genius
```

Guardá con `Ctrl+O`, `Enter`, y salí con `Ctrl+X`.

### 3. Instalar el servicio para que arranque solo

```bash
bash ~/BOT_DISCORD_MUSICA/install_service.sh
```

Listo. El bot ya está corriendo y se va a iniciar solo cada vez que prendas el equipo
(y se reinicia automáticamente si se cae o si vuelve la luz tras un corte).

---

## Comandos útiles

```bash
# Ver si está corriendo
sudo systemctl status discord-musica

# Ver los logs en vivo (Ctrl+C para salir)
journalctl -u discord-musica -f

# Reiniciar el bot
sudo systemctl restart discord-musica

# Detenerlo
sudo systemctl stop discord-musica
```

---

## Actualizar el bot (cuando haya cambios nuevos)

```bash
bash ~/BOT_DISCORD_MUSICA/update_linux.sh
```

Eso baja los últimos cambios, actualiza yt-dlp (importante, YouTube cambia seguido)
y reinicia el servicio.

---

## (Opcional) Cookies de YouTube

En casa normalmente **no hacen falta**. Si algún video puntual te diera bot-check,
copiá un `cookies.txt` exportado del navegador a `~/BOT_DISCORD_MUSICA/cookies.txt`
(desde tu PC Windows, con el equipo en la misma red):

```powershell
scp cookies_small.txt minimaxi@minimaxi-pc:/home/minimaxi/BOT_DISCORD_MUSICA/cookies.txt
```

Luego reiniciá: `sudo systemctl restart discord-musica`

---

## (Opcional pero recomendado) IP fija y mantener encendido

- **IP fija / reserva DHCP**: entrá al router y reservá una IP para `minimaxi-pc`
  por su MAC, así siempre te conectás por SSH a la misma dirección.
- **Que no se suspenda**: para que el equipo no se duerma al cerrar la tapa o por inactividad:

  ```bash
  sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
  ```
