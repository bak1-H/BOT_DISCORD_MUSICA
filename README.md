# 🎵 Bot de Música para Discord

Bot de Discord para reproducir música de YouTube con cola, autoplay y letras de canciones.

---

## Características

- Reproducción de audio desde YouTube (búsqueda por nombre o URL directa)
- Cola de reproducción por servidor
- Autoplay: reproduce canciones similares automáticamente al vaciar la cola
- Letras de canciones via Genius API
- Pausa, reanuda, salta y detiene la reproducción
- Embeds visuales con thumbnail, duración y canal
- Soporte para cookies y PO Token de YouTube (bypass de bot-check)
- Fallback automático entre clients de YouTube: `web → android → ios`
- Soporte para proxy residencial

---

## Requisitos del sistema

- Python 3.11+
- FFmpeg
- Node.js 20 (requerido por yt-dlp para el runtime de JavaScript)
- libopus (codec de audio para Discord)

---

## Instalación local

```bash
# 1. Clonar el repositorio
git clone https://github.com/bak1-H/BOT_DISCORD_MUSICA
cd BOT_DISCORD_MUSICA

# 2. Instalar dependencias Python
pip install -r requirements.txt

# 3. Crear archivo .env con las variables (ver sección Variables de entorno)
cp .env.example .env

# 4. Ejecutar
python bot.py
```

---

## Variables de entorno

Crea un archivo `.env` en la raíz del proyecto:

```env
# Obligatorias
DISCORD_TOKEN=tu_token_de_discord
GENIUS_TOKEN=tu_token_de_genius

# YouTube — necesarias si el bot se ejecuta en servidores cloud (Railway, Render, etc.)
YOUTUBE_COOKIES_B64=cookies_en_base64     # Archivo cookies.txt exportado desde el navegador, codificado en base64
YOUTUBE_PO_TOKEN=po_token_de_youtube      # Proof-of-Origin token para el client web
YOUTUBE_VISITOR_DATA=visitor_data         # Visitor data asociado al PO token

# Opcional — proxy residencial para evitar bloqueos de YouTube
YTDLP_PROXY=http://usuario:password@host:puerto
```

### Cómo obtener las cookies de YouTube

1. Inicia sesión en YouTube en tu navegador
2. Exporta las cookies con una extensión como **Get cookies.txt LOCALLY**
3. Codifica el archivo en base64:
   ```bash
   base64 -w 0 cookies.txt
   ```
4. Pega el resultado en `YOUTUBE_COOKIES_B64`

---

## Comandos

| Comando | Descripción |
|---|---|
| `!play <canción o URL>` | Busca en YouTube y reproduce. Si ya hay algo sonando, lo añade a la cola. |
| `!skip` | Salta la canción actual y reproduce la siguiente en cola. |
| `!stop` | Detiene la reproducción, limpia la cola y desconecta el bot del canal. |
| `!pause` | Pausa la reproducción. |
| `!resume` | Reanuda la reproducción pausada. |
| `!queue` / `!q` | Muestra la canción actual y hasta 10 canciones en cola. |
| `!np` / `!nowplaying` | Muestra un embed con la canción que suena ahora. |
| `!lyrics [canción]` | Muestra la letra. Si no se especifica canción, usa la que suena. |
| `!autoplay <on/off>` | Activa o desactiva el autoplay. Sin argumento muestra el estado actual. |
| `!clear <n>` | Elimina los últimos `n` mensajes del canal (requiere permiso `Manage Messages`). |
| `!repo` | Muestra el enlace al repositorio. |
| `!comandos` | Muestra la ayuda con todos los comandos. |

---

## Despliegue

### Docker

```bash
docker build -t bot-musica .
docker run --env-file .env bot-musica
```

El `Dockerfile` incluye todas las dependencias del sistema: Python 3.11, FFmpeg, Node.js 20 y libopus.

### Railway / Nixpacks

El archivo `nixpacks.toml` configura el build automáticamente:
- **setup:** instala Python 3, FFmpeg y Node.js 20
- **install:** crea el entorno virtual e instala `requirements.txt`
- **start:** ejecuta `python bot.py`

Solo necesitas configurar las variables de entorno en el panel de Railway.

---

## Arquitectura del código

Todo el bot vive en un único archivo `bot.py`, organizado en las siguientes secciones:

### Inicialización

```
load_dotenv()                  → carga variables del archivo .env
COOKIES_FILE                   → decodifica YOUTUBE_COOKIES_B64 y escribe cookies.txt en disco
genius = lyricsgenius.Genius() → cliente de la API de Genius
bot = commands.Bot()           → instancia del bot de Discord
```

### Configuración (`_YTDLP_BASE`)

Diccionario base con las opciones comunes de yt-dlp. Nunca se usa directamente: `build_ytdlp_opts()` lo copia con `deepcopy` antes de añadir el `player_client` y opciones de búsqueda específicas para cada llamada.

### Estado en memoria

Cada clave es el ID del servidor de Discord (`guild.id`):

| Variable | Tipo | Descripción |
|---|---|---|
| `queues` | `dict[int, list[tuple[str, str]]]` | Cola de canciones: lista de `(url, título)` |
| `current_song` | `dict[int, dict]` | Info de la canción actual: `title`, `url`, `thumbnail`, `duration`, `uploader` |
| `autoplay_enabled` | `dict[int, bool]` | Estado del autoplay por servidor |
| `last_played_query` | `dict[int, str]` | Título de la última canción (usado por autoplay para buscar similares) |
| `last_video_id` | `dict[int, str]` | ID del último video (para no repetirlo en autoplay) |
| `playnext_fail_count` | `dict[int, int]` | Contador de fallos consecutivos (detiene la cola tras `MAX_PLAYNEXT_FAILS=3`) |

### Funciones helper

| Función | Descripción |
|---|---|
| `format_duration(seconds)` | Convierte segundos a formato `MM:SS` o `H:MM:SS` |
| `ffmpeg_headers_from_info(info)` | Extrae los headers HTTP del info de yt-dlp y los formatea para FFmpeg |
| `clean_title_for_lyrics(title)` | Limpia el título de la canción eliminando "Official Video", feat., etc. para mejorar la búsqueda en Genius |
| `normalize_youtube_url(value)` | Convierte un ID de video suelto a URL completa de YouTube |
| `build_ytdlp_opts(is_search, client, search_count)` | Construye las opciones para yt-dlp con el `player_client` correcto |
| `is_youtube_login_block(err)` | Detecta si el error es un bloqueo de bot por parte de YouTube |
| `make_song_embed(song, in_queue)` | Genera un `discord.Embed` para mostrar la canción actual o añadida a la cola |

### Extracción de audio

**`ytdlp_extract(query, is_search, client, search_count)`**
Ejecuta yt-dlp en un thread separado (para no bloquear el event loop de asyncio) usando `loop.run_in_executor()`. Retorna el diccionario `info` de yt-dlp.

**`extract_audio_with_fallback(query)`**
Itera sobre `YT_CLIENTS = ["web", "android", "ios"]`. Si un client falla (bloqueado por YouTube), prueba el siguiente. Retorna `(info, audio_url, client_usado)`.

**`pick_best_audio_url(info)`**
Selecciona la URL de audio de mayor calidad del resultado de yt-dlp:
1. Prioriza formatos solo-audio (`vcodec == "none"`) ordenados por `abr` (bitrate de audio)
2. Si no hay, usa formatos audio+video con mayor `tbr` (bitrate total)

### Flujo de reproducción

```
!play "nombre canción"
    └─ ytdlp_extract (búsqueda ytsearch1)
    └─ append (url, title) a queues[gid]
    └─ si no hay nada sonando → play_next(ctx)

play_next(ctx)
    ├─ si la cola está vacía:
    │       └─ autoplay_next() → busca canciones similares (ytsearch5)
    │                          → append a la cola y llama play_next de nuevo
    │       └─ si autoplay off o falla → desconectar
    └─ pop(0) de la cola → extract_audio_with_fallback()
                         → FFmpegPCMAudio
                         → voice_client.play(after=play_next)  ← encadena la siguiente
```

### Autoplay

Cuando la cola se vacía y el autoplay está activado, `autoplay_next()`:
1. Toma el título de la última canción reproducida
2. Lo limpia con `clean_title_for_lyrics()` (elimina "Official Video", etc.)
3. Busca 5 resultados en YouTube (`ytsearch5`)
4. Filtra el último video reproducido para no repetirlo
5. Elige uno al azar y lo añade a la cola

---

## Dependencias

| Paquete | Uso |
|---|---|
| `discord.py` | Framework para el bot de Discord y manejo de voz |
| `yt-dlp[default]` | Extracción de audio de YouTube |
| `lyricsgenius` | Cliente de la API de Genius para letras |
| `python-dotenv` | Carga de variables de entorno desde `.env` |
| `PyNaCl` | Encriptación requerida por discord.py para conexiones de voz |

---

## Solución de problemas

**El bot no reproduce en Railway/servidores cloud**
YouTube bloquea IPs de datacenter. Soluciones en orden de efectividad:
1. Configurar `YTDLP_PROXY` con un proxy residencial
2. Rotar/actualizar `YOUTUBE_COOKIES_B64` (las cookies caducan)
3. Ejecutar el bot en una IP residencial (VPS doméstico, Raspberry Pi, etc.)

**Error "sign in to confirm you're not a bot"**
Las cookies han caducado o rotado. Reexporta desde el navegador y actualiza `YOUTUBE_COOKIES_B64`.

**La cola se detiene sola**
Tras `3` fallos consecutivos (`MAX_PLAYNEXT_FAILS`) el bot limpia la cola y se desconecta para evitar bucles infinitos. Revisa los logs para ver el error específico.
