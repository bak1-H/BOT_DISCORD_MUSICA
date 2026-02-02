import os
import asyncio
import random
import re
import copy
from collections import deque
import discord
from discord.ext import commands
from dotenv import load_dotenv
import base64
import yt_dlp
import lyricsgenius

load_dotenv()


COOKIES_FILE = None

cookies_b64 = os.getenv("YOUTUBE_COOKIES_B64")
if cookies_b64:
    try:
        with open("cookies.txt", "wb") as f:
            f.write(base64.b64decode(cookies_b64))
        COOKIES_FILE = "cookies.txt"
        print("🍪 Cookies cargadas desde variable de entorno")
    except Exception as e:
        print(f"❌ Error cargando cookies: {e}")



# ──────────────────── GENIUS ────────────────────
genius = lyricsgenius.Genius(
    os.getenv("GENIUS_TOKEN"),
    skip_non_songs=True,
    remove_section_headers=True,
    verbose=False
)

# ──────────────────── DISCORD ────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ──────────────────── CONFIG ────────────────────
# Si YouTube bloquea en Railway, la solución real suele ser PROXY residencial.
YTDLP_PROXY = os.getenv("YTDLP_PROXY", "").strip() or None

# Evita loops infinitos si YouTube bloquea o yt-dlp falla
MAX_PLAYNEXT_FAILS = 3

# ──────────────────── RADIO ────────────────────
RADIO_DEFAULT_SEEDS = [
    "lofi hip hop", "pop hits", "rock classics", "edm mix", "latin pop",
    "rap hits", "indie chill", "jazz instrumental",
]
RADIO_SEARCH_SIZE = 12
RADIO_HISTORY_SIZE = 20

# ──────────────────── YT-DLP ────────────────────

PO_TOKEN = os.getenv("YOUTUBE_PO_TOKEN", "").strip()
VISITOR_DATA = os.getenv("YOUTUBE_VISITOR_DATA", "").strip()
YT_CLIENTS = ["web", "android", "ios"]

ytdlp_common_opts = {
    "format": "bestaudio[acodec!=none]/bestaudio/best",
    "noplaylist": True,
    "nocheckcertificate": True,
    "quiet": True,
    "no_warnings": True,
    "proxy": YTDLP_PROXY,

    # JS runtime (necesario hoy)
    "js_runtimes": {"node": {}},
    "remote_components": {"ejs:github"},

    # 🍪 Cookies (archivo creado en runtime)
    "cookiefile": COOKIES_FILE,

    # 🔑 SOLO WEB + PO TOKEN
    "extractor_args": {
        "youtube": {
            "player_client": ["web"],
            "po_token": [f"web+{PO_TOKEN}"] if PO_TOKEN else [],
            "visitor_data": [VISITOR_DATA] if VISITOR_DATA else [],
        }
    },
}

# ──────────────────── ESTADO ────────────────────
queues = {}
current_song = {}
autoplay_enabled = {}
last_played_query = {}
last_video_id = {}
playnext_fail_count = {}
radio_enabled = {}
radio_seed = {}
radio_pool = {}
radio_recent_history = {}

# ──────────────────── HELPERS ────────────────────

def ffmpeg_headers_from_info(info: dict) -> str:
    headers = dict(info.get("http_headers") or {})

    # Defaults seguros (por si no vienen)
    headers.setdefault("User-Agent", "Mozilla/5.0")
    headers.setdefault("Accept-Language", "en-US,en;q=0.9")
    headers.setdefault("Referer", "https://www.youtube.com/")
    headers.setdefault("Origin", "https://www.youtube.com")

    # Armar string CRLF correcto y escapar comillas
    lines = []
    for k, v in headers.items():
        if v is None:
            continue
        v = str(v).replace('"', '\\"')
        lines.append(f"{k}: {v}\r\n")

    return "".join(lines)

def clean_title_for_lyrics(title: str) -> str:
    if not title:
        return ""

    title = title.lower()
    patterns = [
        r"\(.*?\)", r"\[.*?\]", r"official video", r"official audio",
        r"lyrics?", r"audio", r"video", r"hd", r"4k",
        r"remastered?", r"feat\.?.*", r"ft\.?.*", r"- topic", r"•.*",
    ]
    for p in patterns:
        title = re.sub(p, "", title)

    title = re.sub(r"[^\w\s\-]", "", title)
    title = re.sub(r"\s{2,}", " ", title)
    return title.strip()


def normalize_youtube_url(value: str | None) -> str | None:
    if not value:
        return None
    return value if value.startswith("http") else f"https://www.youtube.com/watch?v={value}"


def build_ytdlp_opts(is_search: bool, client: str | None = None) -> dict:
    opts = ytdlp_common_opts.copy()
    opts["extractor_args"] = copy.deepcopy(ytdlp_common_opts.get("extractor_args", {}))

    # Seleccionar client (web por defecto) y, si hay PO token, combinarlo
    base_client = client or "web"
    yt_args = opts["extractor_args"].setdefault("youtube", {})
    yt_args["player_client"] = [base_client]
    yt_args["po_token"] = [f"{base_client}+{PO_TOKEN}"] if PO_TOKEN else []

    if is_search:
        opts.update({
            "default_search": "ytsearch1",
            "extract_flat": "in_playlist",
        })
    return opts


async def ytdlp_extract(loop, query: str, is_search: bool = False, client: str | None = None) -> dict:
    os.environ["YT_DLP_JS_RUNTIME"] = "node"
    opts = build_ytdlp_opts(is_search, client)

    def _extract():
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(query, download=False)

    return await loop.run_in_executor(None, _extract)


async def ytdlp_search(loop, query: str, limit: int = 8, client: str | None = None) -> dict:
    """Búsqueda con número variable de resultados (para radio)."""
    os.environ["YT_DLP_JS_RUNTIME"] = "node"
    opts = build_ytdlp_opts(is_search=True, client=client)
    opts["default_search"] = f"ytsearch{limit}"

    def _extract():
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(query, download=False)

    return await loop.run_in_executor(None, _extract)


async def extract_audio_with_fallback(loop, query: str) -> tuple[dict, str, str]:
    """Intenta extraer el stream probando varios player_client (web → android → ios)."""
    last_error = None
    for client in YT_CLIENTS:
        try:
            info = await ytdlp_extract(loop, query, is_search=False, client=client)
            if isinstance(info, dict) and info.get("entries"):
                info = info["entries"][0]

            audio_url = pick_best_audio_url(info)
            if audio_url:
                return info, audio_url, client

        except Exception as e:
            last_error = e
            continue

    if last_error:
        raise last_error
    raise RuntimeError("No se obtuvo un audio URL con ningún client")


def pick_best_audio_url(info: dict) -> str | None:
    formats = info.get("formats") or []
    audio_only = [
        f for f in formats
        if f.get("acodec") not in (None, "none")
        and f.get("vcodec") == "none"
        and f.get("url")
    ]
    if audio_only:
        audio_only.sort(key=lambda x: (x.get("abr") or 0), reverse=True)
        return audio_only[0]["url"]

    # Fallback: formatos con audio+video (asegura audio presente)
    av_with_audio = [
        f for f in formats
        if f.get("acodec") not in (None, "none")
        and f.get("url")
    ]
    if av_with_audio:
        av_with_audio.sort(key=lambda x: (x.get("tbr") or 0), reverse=True)
        return av_with_audio[0]["url"]

    return None


def is_youtube_login_block(err: Exception) -> bool:
    s = str(err).lower()
    return ("sign in to confirm you’re not a bot" in s) or ("sign in to confirm you're not a bot" in s)


# ──────────────────── AUTOPLAY ────────────────────
async def autoplay_next(ctx) -> bool:
    gid = ctx.guild.id
    if not autoplay_enabled.get(gid):
        return False

    query = clean_title_for_lyrics(last_played_query.get(gid, ""))
    last_id = last_video_id.get(gid)
    if not query:
        return False

    try:
        info = await ytdlp_extract(asyncio.get_event_loop(), query, is_search=True)
        entries = info.get("entries") if isinstance(info, dict) else None
        if not entries:
            return False

        candidates = [e for e in entries[:5] if e.get("id") != last_id]
        if not candidates:
            return False

        pick = random.choice(candidates)
        url = normalize_youtube_url(pick.get("webpage_url") or pick.get("url"))
        if not url:
            return False

        queues.setdefault(gid, []).append(url)
        return True

    except Exception as e:
        print(f"Autoplay error: {e}")
        return False


# ──────────────────── RADIO ────────────────────
def get_radio_history(gid: int) -> deque:
    return radio_recent_history.setdefault(gid, deque(maxlen=RADIO_HISTORY_SIZE))


async def radio_refill_pool(ctx) -> bool:
    gid = ctx.guild.id
    if not radio_enabled.get(gid):
        return False

    seed = radio_seed.get(gid) or random.choice(RADIO_DEFAULT_SEEDS)
    try:
        info = await ytdlp_search(asyncio.get_event_loop(), seed, limit=RADIO_SEARCH_SIZE)
    except Exception as e:
        print(f"Radio search error: {e}")
        return False

    entries = info.get("entries") if isinstance(info, dict) else None
    if not entries:
        return False

    pool = radio_pool.setdefault(gid, [])
    history = get_radio_history(gid)
    added = 0

    for entry in entries:
        vid = entry.get("id")
        url = normalize_youtube_url(entry.get("webpage_url") or entry.get("url"))
        title = entry.get("title") or "Canción"
        if not url or not vid or vid in history:
            continue
        pool.append({"url": url, "title": title, "id": vid})
        added += 1

    if pool:
        random.shuffle(pool)

    return added > 0


async def radio_enqueue_next(ctx, announce: bool = True) -> bool:
    gid = ctx.guild.id
    if not radio_enabled.get(gid):
        return False

    pool = radio_pool.setdefault(gid, [])
    history = get_radio_history(gid)

    if not pool:
        ok = await radio_refill_pool(ctx)
        if not ok:
            if announce:
                await ctx.send("❌ No se pudieron conseguir temas para el modo radio.")
            return False

    if not pool:
        return False

    track = pool.pop()
    queues.setdefault(gid, []).append(track["url"])
    if track.get("id"):
        history.append(track["id"])

    if announce:
        await ctx.send(f"📻 Añadido desde radio: **{track.get('title', 'Canción')}**")

    return True


# ──────────────────── PLAY NEXT ────────────────────
async def play_next(ctx):
    gid = ctx.guild.id
    queue = queues.get(gid) or []

    playnext_fail_count.setdefault(gid, 0)

    if not queue:
        if await autoplay_next(ctx):
            return await play_next(ctx)
        if await radio_enqueue_next(ctx, announce=False):
            return await play_next(ctx)
        if ctx.voice_client:
            await ctx.voice_client.disconnect()
        return

    url = normalize_youtube_url(queue.pop(0))
    queues[gid] = queue

    try:
        if not ctx.voice_client or not ctx.voice_client.is_connected():
            return

        info, audio_url, client_used = await extract_audio_with_fallback(asyncio.get_event_loop(), url)

        current_song[gid] = info.get("title", "Desconocido")
        last_played_query[gid] = current_song[gid]
        last_video_id[gid] = info.get("id")

        hdr = ffmpeg_headers_from_info(info)
        before = (
            "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 "
            f'-headers "{hdr}" '
            '-referer "https://www.youtube.com/" '
            '-user_agent "Mozilla/5.0"'
        )
        source = discord.FFmpegPCMAudio(audio_url, before_options=before, options="-vn")

        ctx.voice_client.play(
            source,
            after=lambda e: bot.loop.create_task(play_next(ctx))
        )

        await ctx.send(f"🎶 Reproduciendo: **{current_song[gid]}**")
        playnext_fail_count[gid] = 0

    except Exception as e:
        playnext_fail_count[gid] = playnext_fail_count.get(gid, 0) + 1
        print(f"Play error: {e}")
        # Ayuda a diagnosticar por qué se corta la llamada
        if playnext_fail_count[gid] == 1:
            await ctx.send(f"❌ Error al reproducir: {e}")

        if is_youtube_login_block(e):
            await ctx.send("❌ YouTube bloqueó la reproducción (bot-check). Reexporta cookies (rotaron).")
            queues[gid] = []
            if ctx.voice_client:
                await ctx.voice_client.disconnect()
            return

        if playnext_fail_count[gid] >= MAX_PLAYNEXT_FAILS:
            await ctx.send("❌ Falló la reproducción varias veces. Deteniendo y limpiando cola.")
            queues[gid] = []
            if ctx.voice_client:
                await ctx.voice_client.disconnect()
            return

        await play_next(ctx)


# ──────────────────── COMANDOS ────────────────────
@bot.command()
async def play(ctx, *, search: str = None):
    if not search:
        return await ctx.send("❌ Escribe el nombre de una canción.")

    if not ctx.author.voice:
        return await ctx.send("❌ Debes estar en un canal de voz.")

    if not ctx.voice_client:
        try:
            await ctx.author.voice.channel.connect(timeout=20)
        except asyncio.TimeoutError:
            return await ctx.send("❌ No pude conectarme al canal de voz (timeout). Intenta otra vez.")
        except (discord.Forbidden, discord.HTTPException, discord.ClientException) as e:
            print(f"Voice connect error: {e}")
            return await ctx.send("❌ No pude conectarme al canal de voz (permisos/capacidad).")

    await ctx.send(f"🔍 Buscando: **{search}**...")

    try:
        info = await ytdlp_extract(asyncio.get_event_loop(), search, is_search=True)
        entries = info.get("entries") if isinstance(info, dict) else None
        if not entries:
            return await ctx.send("❌ No se encontraron resultados.")

        video = entries[0]
        url = normalize_youtube_url(video.get("webpage_url") or video.get("url"))
        title = video.get("title", "Canción")

        queues.setdefault(ctx.guild.id, []).append(url)

        if ctx.voice_client and ctx.voice_client.is_playing():
            await ctx.send(f"✅ En cola: **{title}**")
        else:
            await play_next(ctx)

    except Exception as e:
        print(f"Error en comando play: {e}")
        if is_youtube_login_block(e):
            return await ctx.send(
                "❌ YouTube bloqueó la búsqueda/reproducción desde Railway (bot-check). "
                "Prueba con `YTDLP_PROXY` o ejecuta el bot en una IP residencial."
            )
        await ctx.send("❌ Hubo un error procesando la búsqueda.")


@bot.command()
async def skip(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.stop()


@bot.command()
async def stop(ctx):
    queues[ctx.guild.id] = []
    if ctx.voice_client:
        ctx.voice_client.stop()
        await ctx.voice_client.disconnect()


@bot.command()
async def lyrics(ctx, *, song: str = None):
    if not song:
        song = current_song.get(ctx.guild.id)

    if not song:
        return await ctx.send("❌ Escribe el nombre de la canción o reproduce una primero.")

    title = clean_title_for_lyrics(song)

    try:
        loop = asyncio.get_event_loop()
        song_data = await loop.run_in_executor(None, lambda: genius.search_song(title))
        if not song_data or not song_data.lyrics:
            return await ctx.send("❌ Letra no encontrada.")

        text = song_data.lyrics
        if len(text) > 2000:
            text = text[:1990] + "..."

        await ctx.send(f"🎶 **{song_data.title} – {song_data.artist}**\n\n{text}")

    except Exception as e:
        print(f"Lyrics error: {e}")
        await ctx.send("❌ Error al obtener la letra.")


@bot.command()
async def autoplay(ctx, mode: str = None):
    gid = ctx.guild.id
    if mode == "on":
        autoplay_enabled[gid] = True
        await ctx.send("🔁 Autoplay activado.")
    elif mode == "off":
        autoplay_enabled[gid] = False
        await ctx.send("⏹️ Autoplay desactivado.")
    else:
        state = autoplay_enabled.get(gid, False)
        await ctx.send(f"Autoplay: {'ON' if state else 'OFF'}")


@bot.command()
async def radio(ctx, mode: str = None, *, seed: str = None):
    gid = ctx.guild.id
    mode = (mode or "").lower()

    if mode == "on":
        if not ctx.author.voice:
            return await ctx.send("❌ Debes estar en un canal de voz.")

        if not ctx.voice_client:
            try:
                await ctx.author.voice.channel.connect(timeout=20)
            except asyncio.TimeoutError:
                return await ctx.send("❌ No pude conectarme al canal de voz (timeout). Intenta otra vez.")
            except (discord.Forbidden, discord.HTTPException, discord.ClientException) as e:
                print(f"Voice connect error: {e}")
                return await ctx.send("❌ No pude conectarme al canal de voz (permisos/capacidad).")

        radio_enabled[gid] = True
        radio_seed[gid] = seed.strip() if seed else None
        radio_pool[gid] = []
        get_radio_history(gid).clear()

        msg = "📻 Radio activada"
        if seed:
            msg += f" con semilla: **{seed}**"
        await ctx.send(msg)

        if (not queues.get(gid)) and (not ctx.voice_client.is_playing()):
            if await radio_enqueue_next(ctx, announce=False):
                await play_next(ctx)
        return

    if mode == "off":
        radio_enabled[gid] = False
        radio_seed[gid] = None
        radio_pool[gid] = []
        get_radio_history(gid).clear()
        return await ctx.send("⏹️ Radio desactivada.")

    if mode == "status":
        enabled = radio_enabled.get(gid, False)
        seed_text = radio_seed.get(gid) or "aleatoria"
        pool_size = len(radio_pool.get(gid, []))
        return await ctx.send(
            f"📻 Radio: {'ON' if enabled else 'OFF'}\n"
            f"Semilla: {seed_text}\n"
            f"Pool pendiente: {pool_size} temas"
        )

    if mode == "skip":
        if ctx.voice_client and ctx.voice_client.is_playing():
            ctx.voice_client.stop()
            return await ctx.send("⏭️ Saltando y buscando otro tema de radio...")
        if await radio_enqueue_next(ctx):
            await play_next(ctx)
        return

    await ctx.send("Uso: `!radio on [semilla]`, `!radio off`, `!radio status`, `!radio skip`")


@bot.command()
async def comandos(ctx):
    help_text = (
        "🎵 **Comandos del Bot de Música:**\n"
        "`!play <canción o URL>` - Reproduce una canción o la añade a la cola.\n"
        "`!skip` - Salta la canción actual.\n"
        "`!stop` - Detiene la reproducción y desconecta el bot.\n"
        "`!lyrics <canción>` - Busca y muestra la letra de una canción.\n"
        "`!autoplay <on/off>` - Activa o desactiva el autoplay.\n"
        "`!radio <on/off/status/skip> [semilla]` - Modo radio con canciones aleatorias.\n"
        "`!clear <n>` - Elimina los últimos n mensajes del chat (requiere permisos).\n"
        "`!repo` - Muestra el enlace al repositorio del bot.\n"
        "`!comandos` - Muestra esta ayuda."
    )
    await ctx.send(help_text)


@bot.command()
async def repo(ctx):
    await ctx.send("🔗 Repositorio del bot: https://github.com/bak1-H/BOT_DISCORD_MUSICA")


@bot.command()
@commands.has_permissions(manage_messages=True)
async def clear(ctx, num: int):
    if num < 1:
        return await ctx.send("❌ Usa un número mayor a 0.")
    deleted = await ctx.channel.purge(limit=num + 1)
    await ctx.send(f"🧹 Eliminados {len(deleted) - 1} mensajes.", delete_after=5)


@bot.event
async def on_ready():
    print(f"✅ {bot.user} listo.")


bot.run(os.getenv("DISCORD_TOKEN"))
