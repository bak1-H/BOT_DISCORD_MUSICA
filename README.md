# Bot de Música Discord

Bot de música para Discord escrito en Python. Reproduce audio de YouTube en canales de voz, gestiona una cola de canciones, incluye modo radio por género y búsqueda de letras.

## Tecnologías

- **discord.py 2.7.1+** con soporte DAVE (protocolo de voz encriptado)
- **yt-dlp** para extracción y streaming de audio de YouTube
- **FFmpeg** para decodificación de audio (pipe yt-dlp → FFmpeg)
- **LyricsGenius** para búsqueda de letras
- **Fly.io** como plataforma de hosting

## Comandos

| Comando | Descripción |
|---|---|
| `!play <nombre o URL>` | Reproduce una canción o la añade a la cola |
| `!skip` | Salta la canción actual |
| `!stop` | Detiene la reproducción, limpia la cola y desconecta el bot |
| `!pause` | Pausa la reproducción |
| `!resume` | Reanuda la reproducción pausada |
| `!queue` / `!q` | Muestra la cola y la canción actual |
| `!np` / `!nowplaying` | Muestra la canción que se está reproduciendo |
| `!lyrics [canción]` | Muestra la letra. Sin argumento usa la canción actual |
| `!radio <estilo>` | Activa la radio: busca y reproduce canciones del género en bucle |
| `!radio off` | Desactiva la radio |
| `!clear <n>` | Elimina los últimos n mensajes (requiere permiso Manage Messages) |
| `!repo` | Muestra el enlace al repositorio |
| `!comandos` | Lista todos los comandos en Discord |

## Variables de entorno

Crear un archivo `.env` en la raíz del proyecto:

```env
DISCORD_TOKEN=tu_token_de_discord
GENIUS_TOKEN=tu_token_de_genius
```

Variables opcionales:

| Variable | Descripción |
|---|---|
| `YOUTUBE_COOKIES_B64` | Cookies de YouTube en base64 (necesario en servidores cloud) |
| `YTDLP_PROXY` | Proxy para yt-dlp (formato `http://host:puerto`) |
| `YOUTUBE_PO_TOKEN` | PO Token de YouTube para evitar bot-check |
| `YOUTUBE_VISITOR_DATA` | Visitor Data de YouTube (complementa el PO Token) |
| `PORT` | Puerto del health check HTTP (por defecto `8080`) |

## Instalación local

### Requisitos

- Python 3.11+
- FFmpeg en el PATH del sistema (o `ffmpeg.exe` en la carpeta del proyecto)
- Node.js 20+ (usado por yt-dlp para descifrar algunos streams)

### Pasos

```bash
pip install -r requirements.txt
```

Crear el archivo `.env` con los tokens y ejecutar:

```bash
python bot.py
```

En Windows, si FFmpeg no está en el PATH, basta con copiar `ffmpeg.exe` y `ffprobe.exe` a la carpeta del proyecto. El bot los detecta automáticamente.

## Deploy en Fly.io

### Primera vez

```bash
fly launch
```

### Configurar secrets

```bash
fly secrets set DISCORD_TOKEN="tu_token" GENIUS_TOKEN="tu_token" -a bot-discord-musica
```

### Deploy

```bash
fly deploy
fly scale count 1 -a bot-discord-musica
```

### Cookies de YouTube (recomendado en cloud)

Los servidores de datacenter son frecuentemente bloqueados por YouTube. Para evitarlo hay que exportar las cookies del navegador y subirlas como secret.

**Con el script incluido** (Chrome o Edge con sesión de YouTube activa):

```powershell
.\refresh_cookies.ps1
```

O manualmente:

```powershell
# Exportar cookies
.\yt-dlp.exe --cookies-from-browser chrome --skip-download "https://www.youtube.com" --cookies cookies_temp.txt

# Codificar y subir
$b64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes("cookies_temp.txt"))
fly secrets set YOUTUBE_COOKIES_B64="$b64" -a bot-discord-musica
```

Las cookies expiran cada varios días. Para automatizar el refresco, programar el script con el Programador de Tareas de Windows:

```powershell
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NonInteractive -File `"ruta\al\refresh_cookies.ps1`""
$trigger = New-ScheduledTaskTrigger -RepetitionInterval (New-TimeSpan -Days 5) -Once -At (Get-Date)
Register-ScheduledTask -TaskName "RefreshYTCookies" -Action $action -Trigger $trigger -RunLevel Highest
```

### Ver logs

```bash
fly logs -a bot-discord-musica
```

## Arquitectura

El bot usa un único archivo `bot.py` con la siguiente estructura:

```
bot.py
├── Health check HTTP (arranca primero, puerto 8080 para Fly.io)
├── Configuración y variables de entorno
├── Estado por servidor (queues, current_song, radio_query, radio_played)
├── Helpers (format_duration, make_song_embed, etc.)
├── Extracción de audio
│   ├── ytdlp_extract()                — búsqueda e info via Python API
│   ├── extract_audio_with_fallback()  — prueba clientes web/android/ios
│   └── pick_best_audio_url()          — selecciona el mejor stream
├── Radio
│   └── radio_next()    — busca por género, evita repetir canciones
├── Reproducción
│   └── play_next()     — pipe yt-dlp → FFmpeg → Discord voice
└── Comandos (!play, !skip, !stop, !radio, etc.)
```

### Streaming de audio

El audio se reproduce mediante un pipe: `yt-dlp` descarga el stream en formato WebM/Opus y lo pasa directamente a `FFmpegPCMAudio` sin escribir a disco. Esto evita el problema de los contenedores M4A/MP4 que requieren seek y son incompatibles con pipes.

```
yt-dlp -o - [url]  →  stdout  →  FFmpegPCMAudio(pipe=True)  →  Discord voice
```

### Modo Radio

`!radio trap` activa la radio con el contexto "trap":

1. Busca `ytsearch5:trap` en YouTube
2. Filtra canciones ya reproducidas en esta sesión
3. Elige una al azar y la encola
4. Al terminar cada canción repite el proceso automáticamente
5. Cuando se agota el historial lo resetea para no quedarse sin canciones

### Multi-servidor

Todo el estado se guarda en diccionarios con clave `guild.id`, por lo que el bot puede estar en múltiples servidores simultáneamente con colas y estados independientes.

## Archivos ignorados por git

```
.env
cookies.txt
ffmpeg.exe
ffprobe.exe
yt-dlp.exe
ytdlp_cache/
refresh_cookies.ps1
```
