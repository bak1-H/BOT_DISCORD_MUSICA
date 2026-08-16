import os
import asyncio
import random
import re
import copy
import traceback
import json
from datetime import datetime, timezone, timedelta
import discord
from discord.ext import commands
from dotenv import load_dotenv
import base64
import yt_dlp
import lyricsgenius
import aiohttp

load_dotenv()

os.environ["YT_DLP_JS_RUNTIME"] = "node"

# Agrega ffmpeg local al PATH si no está disponible globalmente
import shutil
if not shutil.which("ffmpeg"):
    _local_ffmpeg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg.exe")
    if os.path.exists(_local_ffmpeg):
        os.environ["PATH"] = os.path.dirname(_local_ffmpeg) + os.pathsep + os.environ["PATH"]
        print(f"[ffmpeg] usando binario local: {_local_ffmpeg}")

# ──────────────────── COOKIES ────────────────────
COOKIES_FILE = None
_here = os.path.dirname(os.path.abspath(__file__))

cookies_b64 = os.getenv("YOUTUBE_COOKIES_B64", "").strip()
if cookies_b64:
    try:
        _path = os.path.join(_here, "cookies.txt")
        with open(_path, "wb") as f:
            f.write(base64.b64decode(cookies_b64))
        COOKIES_FILE = _path
        print("[cookies] Cargadas desde variable de entorno")
    except Exception as e:
        print(f"[cookies] Error con env var: {e}")

if not COOKIES_FILE:
    _bundled = os.path.join(_here, "cookies.txt")
    if os.path.exists(_bundled):
        COOKIES_FILE = _bundled
        print(f"[cookies] Usando archivo bundled")

# ──────────────────── GENIUS ────────────────────
genius = lyricsgenius.Genius(
    os.getenv("GENIUS_TOKEN"),
    skip_non_songs=True,
    remove_section_headers=True,
)

# ──────────────────── DISCORD ────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, case_insensitive=True)

# ──────────────────── CONFIG ────────────────────
YTDLP_PROXY = os.getenv("YTDLP_PROXY", "").strip() or None
RIOT_API_KEY = os.getenv("RIOT_API_KEY", "").strip()
RIOT_PLATFORM = os.getenv("RIOT_REGION", "la2")   # plataforma: la1/la2/na1/euw1...
RIOT_ROUTING = "americas"                          # LAS/LAN/NA usan americas
MAX_PLAYNEXT_FAILS = 3
PO_TOKEN = os.getenv("YOUTUBE_PO_TOKEN", "").strip()
VISITOR_DATA = os.getenv("YOUTUBE_VISITOR_DATA", "").strip()
# android_vr no requiere po_token y suele exponer pistas de audio puro (webm/opus)
# en videos donde web/android/ios sólo entregan el combinado legado mp4 360p
# (formato 18, sin variante audio-only) -> el selector "-f bestaudio" falla ahí.
YT_CLIENTS = ["web", "android_vr", "android", "ios"]

# ──────────────────── YT-DLP BASE CONFIG ────────────────────
_YTDLP_BASE = {
    "format": "bestaudio*/best*",
    "noplaylist": True,
    "nocheckcertificate": True,
    "quiet": True,
    "no_warnings": True,
    "proxy": YTDLP_PROXY,
    "js_runtimes": {"node": {}},
    "cookiefile": COOKIES_FILE,
}

# ──────────────────── STATE ────────────────────
# queues[gid]: list of (url, title)
queues: dict[int, list[tuple[str, str]]] = {}
# current_song[gid]: {"title", "url", "thumbnail", "duration", "uploader"}
current_song: dict[int, dict] = {}
radio_query: dict[int, str | None] = {}       # contexto activo por servidor
radio_played: dict[int, set[str]] = {}         # IDs reproducidos para no repetir
last_video_id: dict[int, str] = {}
playnext_fail_count: dict[int, int] = {}
voice_state_locks: dict[int, asyncio.Lock] = {}
current_audio_file: dict[int, str] = {}  # gid -> archivo temporal de la cancion actual
loop_mode: dict[int, str] = {}  # "off" | "song" | "queue"
alone_tasks: dict[int, asyncio.Task] = {}       # timer de desconexion por inactividad
last_text_channel: dict[int, discord.TextChannel] = {}  # ultimo canal de texto usado


def get_voice_lock(gid: int) -> asyncio.Lock:
    lock = voice_state_locks.get(gid)
    if lock is None:
        lock = asyncio.Lock()
        voice_state_locks[gid] = lock
    return lock


# Serializa el arranque de canciones por servidor: evita que dos invocaciones
# de play_next (ej. encolar rapido mientras una cancion aun esta cargando)
# reproduzcan a la vez y choquen con "Already playing audio".
playback_locks: dict[int, asyncio.Lock] = {}


DOWNLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


def cleanup_audio_file(gid: int) -> None:
    path = current_audio_file.pop(gid, None)
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def get_playback_lock(gid: int) -> asyncio.Lock:
    lock = playback_locks.get(gid)
    if lock is None:
        lock = asyncio.Lock()
        playback_locks[gid] = lock
    return lock

# ──────────────────── HELPERS ────────────────────

def format_duration(seconds) -> str:
    if not seconds:
        return "?"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"



def clean_title_for_lyrics(title: str) -> str:
    if not title:
        return ""
    title = title.lower()
    for p in [
        r"\(.*?\)", r"\[.*?\]", r"official video", r"official audio",
        r"lyrics?", r"audio", r"video", r"hd", r"4k",
        r"remastered?", r"feat\.?.*", r"ft\.?.*", r"- topic", r"•.*",
    ]:
        title = re.sub(p, "", title)
    title = re.sub(r"[^\w\s\-]", "", title)
    return re.sub(r"\s{2,}", " ", title).strip()


def normalize_youtube_url(value: str | None) -> str | None:
    if not value:
        return None
    return value if value.startswith("http") else f"https://www.youtube.com/watch?v={value}"


def build_ytdlp_opts(is_search: bool, client: str = "web", search_count: int = 1) -> dict:
    opts = copy.deepcopy(_YTDLP_BASE)
    yt_args: dict = {"player_client": [client]}
    if PO_TOKEN:
        yt_args["po_token"] = [f"{client}+{PO_TOKEN}"]
    if VISITOR_DATA:
        yt_args["visitor_data"] = [VISITOR_DATA]
    opts["extractor_args"] = {"youtube": yt_args}
    if is_search:
        opts["default_search"] = f"ytsearch{search_count}"
        opts["extract_flat"] = "in_playlist"
    return opts


def is_youtube_login_block(err: Exception) -> bool:
    s = str(err).lower()
    return any(phrase in s for phrase in (
        "sign in to confirm you're not a bot",
        "sign in to confirm",
        "bot check",
        "login required",
    ))


def make_song_embed(song: dict, in_queue: bool = False) -> discord.Embed:
    if in_queue:
        embed = discord.Embed(
            title="✅ Añadido a la cola",
            description=f"**{song['title']}**",
            color=discord.Color.blue(),
        )
    else:
        embed = discord.Embed(
            title="🎵 Reproduciendo ahora",
            description=f"**{song['title']}**",
            color=discord.Color.green(),
        )
    if song.get("uploader"):
        embed.add_field(name="Canal", value=song["uploader"], inline=True)
    if song.get("duration"):
        embed.add_field(name="Duración", value=format_duration(song["duration"]), inline=True)
    if song.get("thumbnail"):
        embed.set_thumbnail(url=song["thumbnail"])
    return embed


# ──────────────────── AUDIO EXTRACTION ────────────────────

async def ytdlp_extract(query: str, is_search: bool = False, client: str = "web", search_count: int = 1) -> dict:
    loop = asyncio.get_running_loop()
    opts = build_ytdlp_opts(is_search, client, search_count)

    def _extract():
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(query, download=False)

    return await loop.run_in_executor(None, _extract)


def build_download_opts(gid: int, client: str) -> dict:
    opts = build_ytdlp_opts(is_search=False, client=client)
    opts["format"] = "bestaudio[ext=webm]/bestaudio[ext=opus]/bestaudio[ext=ogg]/bestaudio/best"
    opts["outtmpl"] = os.path.join(DOWNLOAD_DIR, f"{gid}_%(id)s.%(ext)s")
    return opts


async def download_audio_with_fallback(gid: int, url: str) -> tuple[dict, str, str]:
    """Try each player_client until one can extract AND download the audio.

    Extraction and download happen in the same yt-dlp call (single request per
    client) instead of extracting metadata first and re-downloading separately
    afterwards: doing two round-trips to YouTube for the same video is what was
    triggering an intermittent 403 on the second (download) request.
    """
    loop = asyncio.get_running_loop()
    last_error = None
    for client in YT_CLIENTS:
        opts = build_download_opts(gid, client)

        def _download():
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if isinstance(info, dict) and info.get("entries"):
                    info = info["entries"][0]
                return info, ydl.prepare_filename(info)

        try:
            info, path = await loop.run_in_executor(None, _download)
            if os.path.exists(path):
                return info, path, client
        except Exception as e:
            last_error = e
    if last_error:
        raise last_error
    raise RuntimeError("No se pudo descargar el audio con ningún client")


# ──────────────────── RADIO ────────────────────

async def radio_next(ctx) -> bool:
    gid = ctx.guild.id
    query = radio_query.get(gid)
    if not query:
        return False

    played = radio_played.setdefault(gid, set())
    last_id = last_video_id.get(gid)

    try:
        info = await ytdlp_extract(query, is_search=True, search_count=5)
        entries = info.get("entries") if isinstance(info, dict) else None
        if not entries:
            return False

        candidates = [
            e for e in entries
            if e.get("id") and e.get("id") != last_id and e.get("id") not in played
        ]
        if not candidates:
            # Si ya se jugaron todos, resetear historial y volver a intentar
            radio_played[gid] = set()
            candidates = [e for e in entries if e.get("id") != last_id]
        if not candidates:
            return False

        pick = random.choice(candidates)
        url = normalize_youtube_url(pick.get("webpage_url") or pick.get("url"))
        title = pick.get("title", "Desconocido")
        if not url:
            return False

        if pick.get("id"):
            radio_played[gid].add(pick["id"])

        queues.setdefault(gid, []).append((url, title))
        return True
    except Exception as e:
        print(f"Radio error: {e}")
        return False


# ──────────────────── PLAY NEXT ────────────────────

async def play_next(ctx):
    """Serializa el arranque de la siguiente cancion. Lo llama el callback `after`."""
    async with get_playback_lock(ctx.guild.id):
        await _play_next_locked(ctx)


async def ensure_playing(ctx):
    """Arranca la reproduccion solo si no hay nada sonando ni cargando (serializado)."""
    gid = ctx.guild.id
    async with get_playback_lock(gid):
        vc = ctx.voice_client
        if not vc or not vc.is_connected():
            return
        if vc.is_playing() or vc.is_paused():
            return
        await _play_next_locked(ctx)


async def _play_next_locked(ctx):
    gid = ctx.guild.id
    playnext_fail_count.setdefault(gid, 0)

    mode = loop_mode.get(gid, "off")
    prev = current_song.get(gid)
    if prev:
        if mode == "song":
            queues.setdefault(gid, []).insert(0, (prev["url"], prev["title"]))
        elif mode == "queue":
            queues.setdefault(gid, []).append((prev["url"], prev["title"]))

    queue = queues.get(gid) or []
    cleanup_audio_file(gid)  # ya no se necesita el archivo de la cancion que termino

    if not queue:
        if await radio_next(ctx):
            return await _play_next_locked(ctx)
        current_song.pop(gid, None)
        if ctx.voice_client:
            await ctx.voice_client.disconnect()
        return

    url_raw, queued_title = queue.pop(0)
    queues[gid] = queue
    url = normalize_youtube_url(url_raw)

    try:
        info, audio_path, _ = await download_audio_with_fallback(gid, url)

        song = {
            "title": info.get("title", queued_title),
            "url": url,
            "thumbnail": info.get("thumbnail"),
            "duration": info.get("duration"),
            "uploader": info.get("uploader") or info.get("channel"),
        }
        current_song[gid] = song
        last_video_id[gid] = info.get("id")
        current_audio_file[gid] = audio_path

        async with get_voice_lock(gid):
            if not ctx.voice_client or not ctx.voice_client.is_connected():
                cleanup_audio_file(gid)
                return

            source = discord.FFmpegPCMAudio(audio_path, options="-vn")
            ctx.voice_client.play(
                source,
                after=lambda e: asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop),
            )

        await ctx.send(embed=make_song_embed(song))
        playnext_fail_count[gid] = 0

    except Exception as e:
        # Otra invocacion ya esta reproduciendo: abortar sin contar como fallo ni reintentar.
        if isinstance(e, discord.ClientException) and "already playing" in str(e).lower():
            return

        playnext_fail_count[gid] = playnext_fail_count.get(gid, 0) + 1
        traceback.print_exc()
        print(f"Play error: {e}")

        if playnext_fail_count[gid] == 1:
            await ctx.send(f"❌ Error al reproducir: {e}")

        if is_youtube_login_block(e):
            await ctx.send(f"⚠️ `{queued_title}` bloqueado por YouTube desde este servidor. Saltando.")
            playnext_fail_count[gid] = 0
            await _play_next_locked(ctx)
            return

        if playnext_fail_count[gid] >= MAX_PLAYNEXT_FAILS:
            await ctx.send("❌ Falló la reproducción varias veces. Deteniendo y limpiando cola.")
            queues[gid] = []
            async with get_voice_lock(gid):
                if ctx.voice_client and ctx.voice_client.is_connected():
                    await ctx.voice_client.disconnect()
            return

        await _play_next_locked(ctx)


# ──────────────────── COMANDOS ────────────────────

@bot.command()
async def play(ctx, *, search: str = None):
    if not search:
        return await ctx.send("❌ Escribe el nombre de una canción.")
    if not ctx.author.voice:
        return await ctx.send("❌ Debes estar en un canal de voz.")

    async with get_voice_lock(ctx.guild.id):
        if not ctx.voice_client or not ctx.voice_client.is_connected():
            try:
                await ctx.author.voice.channel.connect(timeout=60)
            except asyncio.TimeoutError:
                try:
                    await ctx.send("❌ No pude conectarme al canal de voz (timeout). Verifica que el bot tenga permisos y que no haya un firewall bloqueando UDP.")
                except Exception:
                    pass
                return
            except (discord.Forbidden, discord.HTTPException, discord.ClientException) as e:
                print(f"Voice connect error: {e}")
                try:
                    await ctx.send("❌ No pude conectarme al canal de voz (permisos/capacidad).")
                except Exception:
                    pass
                return

    last_text_channel[ctx.guild.id] = ctx.channel
    await ctx.send(f"🔍 Buscando: **{search}**...")

    try:
        info = await ytdlp_extract(search, is_search=True)
        entries = info.get("entries") if isinstance(info, dict) else None
        if not entries:
            return await ctx.send("❌ No se encontraron resultados.")

        video = entries[0]
        url = normalize_youtube_url(video.get("webpage_url") or video.get("url"))
        title = video.get("title", "Canción")

        queues.setdefault(ctx.guild.id, []).append((url, title))

        vc = ctx.voice_client
        # "ocupado" = sonando, pausado, o con una cancion cargando (lock tomado)
        busy = bool(vc and (vc.is_playing() or vc.is_paused())) or get_playback_lock(ctx.guild.id).locked()
        if busy:
            song_preview = {
                "title": title,
                "url": url,
                "thumbnail": video.get("thumbnail"),
                "duration": video.get("duration"),
                "uploader": video.get("uploader") or video.get("channel"),
            }
            await ctx.send(embed=make_song_embed(song_preview, in_queue=True))
        await ensure_playing(ctx)

    except Exception as e:
        traceback.print_exc()
        print(f"Error en comando play: {e}")
        if is_youtube_login_block(e):
            return await ctx.send(
                "❌ YouTube bloqueó la búsqueda (bot-check). "
                "Prueba con `YTDLP_PROXY` o ejecuta el bot en una IP residencial."
            )
        await ctx.send("❌ Hubo un error procesando la búsqueda.")


@bot.command()
async def skip(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.stop()
        await ctx.send("⏭️ Canción saltada.")
    else:
        await ctx.send("❌ No hay nada reproduciéndose.")


@bot.command()
async def stop(ctx):
    gid = ctx.guild.id
    cleanup_audio_file(gid)
    queues[gid] = []
    current_song.pop(gid, None)
    loop_mode.pop(gid, None)
    radio_query.pop(gid, None)
    radio_played.pop(gid, None)
    async with get_voice_lock(gid):
        if ctx.voice_client:
            ctx.voice_client.stop()
            if ctx.voice_client.is_connected():
                await ctx.voice_client.disconnect()
    await ctx.send("⏹️ Reproducción detenida.")


@bot.command()
async def pause(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.pause()
        await ctx.send("⏸️ Pausado.")
    else:
        await ctx.send("❌ No hay nada reproduciéndose.")


@bot.command()
async def resume(ctx):
    if ctx.voice_client and ctx.voice_client.is_paused():
        ctx.voice_client.resume()
        await ctx.send("▶️ Reanudado.")
    else:
        await ctx.send("❌ No hay nada pausado.")


@bot.command(aliases=["q"])
async def queue(ctx):
    gid = ctx.guild.id
    q = queues.get(gid) or []
    song = current_song.get(gid)

    embed = discord.Embed(title="🎵 Cola de reproducción", color=discord.Color.blurple())

    if song:
        duration_str = f" `{format_duration(song['duration'])}`" if song.get("duration") else ""
        embed.add_field(
            name="▶️ Reproduciendo ahora",
            value=f"**{song['title']}**{duration_str}",
            inline=False,
        )
    if q:
        lines = [f"`{i + 1}.` {title}" for i, (_, title) in enumerate(q[:10])]
        if len(q) > 10:
            lines.append(f"*...y {len(q) - 10} más*")
        embed.add_field(name="📋 En cola", value="\n".join(lines), inline=False)
    elif not song:
        embed.description = "La cola está vacía."

    rq = radio_query.get(gid)
    if rq:
        embed.set_footer(text=f"📻 Radio activa: {rq}")

    await ctx.send(embed=embed)


@bot.command(aliases=["nowplaying"])
async def np(ctx):
    song = current_song.get(ctx.guild.id)
    if not song:
        return await ctx.send("❌ No hay nada reproduciéndose ahora.")
    await ctx.send(embed=make_song_embed(song))


@bot.command()
async def lyrics(ctx, *, song: str = None):
    if not song:
        song_data = current_song.get(ctx.guild.id)
        song = song_data["title"] if song_data else None
    if not song:
        return await ctx.send("❌ Escribe el nombre de la canción o reproduce una primero.")

    title = clean_title_for_lyrics(song)
    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, lambda: genius.search_song(title))
        if not result or not result.lyrics:
            return await ctx.send("❌ Letra no encontrada.")
        text = result.lyrics
        if len(text) > 2000:
            text = text[:1990] + "..."
        await ctx.send(f"🎶 **{result.title} – {result.artist}**\n\n{text}")
    except Exception as e:
        print(f"Lyrics error: {e}")
        await ctx.send("❌ Error al obtener la letra.")


@bot.command()
async def radio(ctx, *, query: str = None):
    gid = ctx.guild.id

    if not query or query.lower() == "off":
        radio_query.pop(gid, None)
        radio_played.pop(gid, None)
        await ctx.send("📻 Radio desactivada.")
        return

    if not ctx.author.voice:
        return await ctx.send("❌ Debes estar en un canal de voz.")

    async with get_voice_lock(gid):
        if not ctx.voice_client or not ctx.voice_client.is_connected():
            try:
                await ctx.author.voice.channel.connect(timeout=60)
            except asyncio.TimeoutError:
                try:
                    await ctx.send("❌ No pude conectarme al canal de voz (timeout).")
                except Exception:
                    pass
                return
            except (discord.Forbidden, discord.HTTPException, discord.ClientException) as e:
                print(f"Voice connect error: {e}")
                try:
                    await ctx.send("❌ No pude conectarme al canal de voz.")
                except Exception:
                    pass
                return

    last_text_channel[gid] = ctx.channel
    radio_query[gid] = query
    radio_played[gid] = set()

    embed = discord.Embed(
        title="📻 Radio activada",
        description=f"Reproduciendo canciones de **{query}** en bucle.",
        color=discord.Color.og_blurple(),
    )
    await ctx.send(embed=embed)

    if await radio_next(ctx):
        await ensure_playing(ctx)
    else:
        radio_query.pop(gid, None)
        await ctx.send("❌ No se encontraron canciones para ese estilo.")


@bot.command()
async def comandos(ctx):
    embed = discord.Embed(title="🎵 Comandos del Bot de Música", color=discord.Color.blurple())
    embed.add_field(name="!play <canción o URL>", value="Reproduce o añade a la cola.", inline=False)
    embed.add_field(name="!skip", value="Salta la canción actual.", inline=False)
    embed.add_field(name="!stop", value="Detiene y desconecta el bot.", inline=False)
    embed.add_field(name="!pause / !resume", value="Pausa o reanuda la reproducción.", inline=False)
    embed.add_field(name="!queue / !q", value="Muestra la cola de reproducción.", inline=False)
    embed.add_field(name="!np / !nowplaying", value="Muestra la canción actual.", inline=False)
    embed.add_field(name="!lyrics [canción]", value="Muestra la letra de la canción.", inline=False)
    embed.add_field(name="!loop", value="Cicla entre: sin loop → repetir canción → repetir cola.", inline=False)
    embed.add_field(name="!radio <estilo>", value="Reproduce canciones del estilo en bucle. `!radio off` para detener.", inline=False)
    embed.add_field(name="!clear <n>", value="Elimina los últimos n mensajes (requiere permisos).", inline=False)
    embed.add_field(name="!invocador <Nombre#TAG>", value="Muestra el rango y stats de un invocador de LoL.", inline=False)
    embed.add_field(name="!vs <Nombre#TAG> <Nombre#TAG>", value="Compara dos invocadores de LoL lado a lado.", inline=False)
    embed.add_field(name="!playlist create <nombre>", value="Crea una playlist vacía.", inline=False)
    embed.add_field(name="!playlist add <nombre>", value="Agrega la canción actual a la playlist.", inline=False)
    embed.add_field(name="!playlist load <nombre>", value="Carga la playlist en la cola.", inline=False)
    embed.add_field(name="!playlist list", value="Muestra todas las playlists del servidor.", inline=False)
    embed.add_field(name="!playlist show <nombre>", value="Muestra las canciones de una playlist.", inline=False)
    embed.add_field(name="!playlist remove <nombre> <pos>", value="Saca una canción de la playlist.", inline=False)
    embed.add_field(name="!playlist delete <nombre>", value="Elimina la playlist completa.", inline=False)
    embed.add_field(name="!reiniciar", value="Reinicia el bot si se quedó bugueado.", inline=False)
    embed.add_field(name="!repo", value="Enlace al repositorio del bot.", inline=False)
    await ctx.send(embed=embed)


@bot.command()
async def repo(ctx):
    await ctx.send("🔗 Repositorio: https://github.com/bak1-H/BOT_DISCORD_MUSICA")


@bot.command()
async def loop(ctx):
    gid = ctx.guild.id
    modes = ["off", "song", "queue"]
    current = loop_mode.get(gid, "off")
    next_mode = modes[(modes.index(current) + 1) % len(modes)]
    loop_mode[gid] = next_mode
    labels = {
        "off":   "➡️ Loop **desactivado**.",
        "song":  "🔂 Repitiendo **canción actual**.",
        "queue": "🔁 Repitiendo **cola completa**.",
    }
    await ctx.send(labels[next_mode])


@bot.command()
@commands.has_permissions(manage_messages=True)
async def clear(ctx, num: int):
    if num < 1:
        return await ctx.send("❌ Usa un número mayor a 0.")
    # Discord only allows bulk-delete for messages < 14 days old.
    # Deleting older messages one-by-one hammers the rate limit and can drop the voice session.
    cutoff = datetime.now(timezone.utc) - timedelta(days=13, hours=23)
    deleted = await ctx.channel.purge(
        limit=num + 1,
        check=lambda m: m.created_at > cutoff,
        bulk=True,
    )
    count = len(deleted) - 1  # -1 for the !clear command itself
    await ctx.send(f"🧹 Eliminados {count} mensajes.", delete_after=5)


_RANK_EMOJI = {
    "IRON": "🔩", "BRONZE": "🥉", "SILVER": "🥈", "GOLD": "🥇",
    "PLATINUM": "🌿", "EMERALD": "💚", "DIAMOND": "💎",
    "MASTER": "👑", "GRANDMASTER": "🏆", "CHALLENGER": "🔥",
}

_RANK_COLOR = {
    "IRON": 0x4a4a4a, "BRONZE": 0xcd7f32, "SILVER": 0xa8a9ad,
    "GOLD": 0xffd700, "PLATINUM": 0x4da6a8, "EMERALD": 0x149c50,
    "DIAMOND": 0x5b85f5, "MASTER": 0x9c4dcc,
    "GRANDMASTER": 0xd45a2a, "CHALLENGER": 0xf4c874,
}

_TIER_ORDER = ["IRON","BRONZE","SILVER","GOLD","PLATINUM","EMERALD","DIAMOND","MASTER","GRANDMASTER","CHALLENGER"]


def _fmt_rank(entry: dict | None) -> str:
    if not entry:
        return "*Sin clasificar*"
    tier = entry["tier"]
    division = entry.get("rank", "")
    lp = entry["leaguePoints"]
    wins, losses = entry["wins"], entry["losses"]
    wr = round(wins / (wins + losses) * 100) if (wins + losses) else 0
    emoji = _RANK_EMOJI.get(tier, "")
    return (
        f"{emoji} **{tier.capitalize()} {division}**\n"
        f"`{lp} LP` · {wins}V / {losses}D\n"
        f"**{wr}%** winrate"
    )


def _best_tier(entries: list) -> str | None:
    tiers = [e["tier"] for e in entries if "tier" in e]
    return max(tiers, key=lambda t: _TIER_ORDER.index(t) if t in _TIER_ORDER else -1, default=None)


async def _fetch_lol_player(session: aiohttp.ClientSession, riot_id: str, headers: dict) -> dict | str:
    """Fetches account, summoner and ranked data. Returns a dict or an error string."""
    game_name, tag_line = riot_id.rsplit("#", 1)
    url = f"https://{RIOT_ROUTING}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}"
    async with session.get(url, headers=headers) as r:
        if r.status == 404:
            return f"❌ `{riot_id}` no encontrado."
        if r.status in (401, 403):
            return "❌ API key inválida o expirada."
        if r.status != 200:
            return f"❌ Error Riot API ({r.status})."
        account = await r.json()

    puuid = account["puuid"]

    async with session.get(
        f"https://{RIOT_PLATFORM}.api.riotgames.com/lol/summoner/v4/summoners/by-puuid/{puuid}",
        headers=headers,
    ) as r:
        summoner = await r.json() if r.status == 200 else {}

    async with session.get(
        f"https://{RIOT_PLATFORM}.api.riotgames.com/lol/league/v4/entries/by-puuid/{puuid}",
        headers=headers,
    ) as r:
        entries = await r.json() if r.status == 200 else []

    return {
        "account": account,
        "summoner": summoner,
        "solo": next((e for e in entries if e["queueType"] == "RANKED_SOLO_5x5"), None),
        "flex": next((e for e in entries if e["queueType"] == "RANKED_FLEX_SR"), None),
        "entries": entries,
    }


@bot.command()
async def invocador(ctx, *, nombre: str = None):
    if not nombre:
        return await ctx.send("❌ Uso: `!invocador NombreJugador#TAG`")
    if "#" not in nombre:
        return await ctx.send("❌ Incluí el tag. Ejemplo: `!invocador Faker#KR1`")
    if not RIOT_API_KEY:
        return await ctx.send("❌ RIOT_API_KEY no configurada en el servidor.")

    game_name, tag_line = nombre.rsplit("#", 1)
    headers = {"X-Riot-Token": RIOT_API_KEY}

    await ctx.send(f"🔍 Buscando **{nombre}**...")

    try:
        async with aiohttp.ClientSession() as session:
            # 1. PUUID desde Riot ID
            url = (
                f"https://{RIOT_ROUTING}.api.riotgames.com"
                f"/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}"
            )
            async with session.get(url, headers=headers) as r:
                if r.status == 404:
                    return await ctx.send(f"❌ `{nombre}` no encontrado.")
                if r.status in (401, 403):
                    return await ctx.send("❌ API key inválida o expirada.")
                if r.status != 200:
                    return await ctx.send(f"❌ Error Riot API ({r.status}).")
                account = await r.json()

            # 2. Summoner por PUUID — solo para nivel e ícono (id ya no se devuelve)
            url = (
                f"https://{RIOT_PLATFORM}.api.riotgames.com"
                f"/lol/summoner/v4/summoners/by-puuid/{account['puuid']}"
            )
            async with session.get(url, headers=headers) as r:
                summoner = await r.json() if r.status == 200 else {}

            # 3. Ranked por PUUID (endpoint nuevo, no requiere summonerId)
            url = (
                f"https://{RIOT_PLATFORM}.api.riotgames.com"
                f"/lol/league/v4/entries/by-puuid/{account['puuid']}"
            )
            async with session.get(url, headers=headers) as r:
                if r.status != 200:
                    return await ctx.send(f"❌ Error obteniendo ranked ({r.status}).")
                entries = await r.json()

    except aiohttp.ClientError as e:
        print(f"Riot API error: {e}")
        return await ctx.send("❌ Error de red al consultar la API de Riot.")

    solo = next((e for e in entries if e["queueType"] == "RANKED_SOLO_5x5"), None)
    flex = next((e for e in entries if e["queueType"] == "RANKED_FLEX_SR"), None)
    icon_id = summoner.get("profileIconId", 0)
    level = summoner.get("summonerLevel", "?")

    best = _best_tier(entries)
    color = _RANK_COLOR.get(best, 0x5865f2) if best else 0x5865f2

    embed = discord.Embed(
        title=f"{account['gameName']}#{account['tagLine']}",
        description=f"Nivel **{level}** · {RIOT_PLATFORM.upper()}",
        color=color,
    )
    embed.add_field(name="🎯 Solo/Duo", value=_fmt_rank(solo), inline=True)
    embed.add_field(name="👥 Flex 5v5", value=_fmt_rank(flex), inline=True)
    embed.set_thumbnail(
        url=f"https://ddragon.leagueoflegends.com/cdn/15.1.1/img/profileicon/{icon_id}.png"
    )
    embed.set_footer(
        text="League of Legends · Riot Games API",
        icon_url="https://cdn.communitydragon.org/latest/asset/ASSETS/Riot_Games/Logos/LoL_Icon_RGB.png",
    )
    await ctx.send(embed=embed)


def _rank_score(entry: dict | None) -> float:
    if not entry:
        return -1.0
    tier_idx = _TIER_ORDER.index(entry["tier"]) if entry["tier"] in _TIER_ORDER else 0
    div_bonus = {"I": 3, "II": 2, "III": 1, "IV": 0}.get(entry.get("rank", "IV"), 0)
    return tier_idx * 4 + div_bonus + entry["leaguePoints"] / 100


def _wr(entry: dict | None) -> float:
    if not entry:
        return -1.0
    w, l = entry["wins"], entry["losses"]
    return round(w / (w + l) * 100, 1) if (w + l) else 0.0


def _cmp(a, b) -> tuple[str, str]:
    """Returns (indicator_a, indicator_b). 🟢 = wins, 🔴 = loses, ⚪ = tie."""
    if a > b:   return "🟢", "🔴"
    if b > a:   return "🔴", "🟢"
    return "⚪", "⚪"


@bot.command()
async def vs(ctx, *, nombres: str = None):
    if not nombres:
        return await ctx.send("❌ Uso: `!vs Jugador1#TAG1 Jugador2#TAG2`")
    if not RIOT_API_KEY:
        return await ctx.send("❌ RIOT_API_KEY no configurada.")

    ids = re.findall(r'\S+#\S+', nombres)
    if len(ids) < 2:
        return await ctx.send("❌ Necesito dos Riot IDs. Ejemplo: `!vs maxipepsi#CHL FatReign#KFC`")

    await ctx.send(f"⚔️ Comparando **{ids[0]}** vs **{ids[1]}**...")

    headers = {"X-Riot-Token": RIOT_API_KEY}
    try:
        async with aiohttp.ClientSession() as session:
            p1, p2 = await asyncio.gather(
                _fetch_lol_player(session, ids[0], headers),
                _fetch_lol_player(session, ids[1], headers),
            )
    except aiohttp.ClientError as e:
        print(f"Riot VS error: {e}")
        return await ctx.send("❌ Error de red al consultar la API de Riot.")

    if isinstance(p1, str):
        return await ctx.send(p1)
    if isinstance(p2, str):
        return await ctx.send(p2)

    name1 = f"{p1['account']['gameName']}#{p1['account']['tagLine']}"
    name2 = f"{p2['account']['gameName']}#{p2['account']['tagLine']}"

    # ── comparación por categoría ──
    solo_r1, solo_r2 = _cmp(_rank_score(p1["solo"]), _rank_score(p2["solo"]))
    solo_w1, solo_w2 = _cmp(_wr(p1["solo"]), _wr(p2["solo"]))
    flex_r1, flex_r2 = _cmp(_rank_score(p1["flex"]), _rank_score(p2["flex"]))
    flex_w1, flex_w2 = _cmp(_wr(p1["flex"]), _wr(p2["flex"]))

    pts1 = sum(2 if i == "🟢" else (1 if i == "⚪" else 0) for i in [solo_r1, solo_w1, flex_r1, flex_w1])
    pts2 = sum(2 if i == "🟢" else (1 if i == "⚪" else 0) for i in [solo_r2, solo_w2, flex_r2, flex_w2])

    def player_field(p, sr, sw, fr, fw) -> str:
        lvl   = p["summoner"].get("summonerLevel", "?")
        s     = p["solo"]
        f     = p["flex"]
        s_wr  = f"{_wr(s):.0f}%" if s else "—"
        f_wr  = f"{_wr(f):.0f}%" if f else "—"
        s_lbl = f"{_RANK_EMOJI.get(s['tier'],'')} {s['tier'].capitalize()} {s.get('rank','')} · {s['leaguePoints']} LP" if s else "Sin clasificar"
        f_lbl = f"{_RANK_EMOJI.get(f['tier'],'')} {f['tier'].capitalize()} {f.get('rank','')} · {f['leaguePoints']} LP" if f else "Sin clasificar"
        return (
            f"Nivel **{lvl}**\n\n"
            f"🎯 **Solo/Duo**\n{sr} {s_lbl}\n{sw} WR: **{s_wr}**\n\n"
            f"👥 **Flex**\n{fr} {f_lbl}\n{fw} WR: **{f_wr}**"
        )

    if pts1 > pts2:
        verdict = f"🏆 **{name1}** es superior ({pts1//2}-{pts2//2})"
        winner_tier = _best_tier(p1["entries"])
    elif pts2 > pts1:
        verdict = f"🏆 **{name2}** es superior ({pts2//2}-{pts1//2})"
        winner_tier = _best_tier(p2["entries"])
    else:
        verdict = "🤝 **Empate** — estadísticas muy parejas"
        winner_tier = _best_tier(p1["entries"] + p2["entries"])

    color = _RANK_COLOR.get(winner_tier, 0x5865f2) if winner_tier else 0x5865f2

    embed = discord.Embed(title=f"⚔️ {name1}  vs  {name2}", color=color)
    embed.add_field(name=name1, value=player_field(p1, solo_r1, solo_w1, flex_r1, flex_w1), inline=True)
    embed.add_field(name=name2, value=player_field(p2, solo_r2, solo_w2, flex_r2, flex_w2), inline=True)
    embed.add_field(name="Veredicto", value=verdict, inline=False)
    embed.set_footer(
        text="League of Legends · Riot Games API",
        icon_url="https://cdn.communitydragon.org/latest/asset/ASSETS/Riot_Games/Logos/LoL_Icon_RGB.png",
    )
    await ctx.send(embed=embed)


# ──────────────────── PLAYLISTS ────────────────────

PLAYLISTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "playlists")
os.makedirs(PLAYLISTS_DIR, exist_ok=True)


def _pl_path(gid: int) -> str:
    return os.path.join(PLAYLISTS_DIR, f"{gid}.json")


def _load_playlists(gid: int) -> dict:
    path = _pl_path(gid)
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_playlists(gid: int, data: dict) -> None:
    with open(_pl_path(gid), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


@bot.group(name="playlist", aliases=["pl"], invoke_without_command=True)
async def playlist(ctx):
    await ctx.send(
        "📋 **Comandos de playlist:**\n"
        "`!playlist create <nombre>` — crea una playlist vacía\n"
        "`!playlist add <nombre>` — agrega la canción actual\n"
        "`!playlist load <nombre>` — carga la playlist en la cola\n"
        "`!playlist list` — muestra todas las playlists\n"
        "`!playlist show <nombre>` — muestra las canciones\n"
        "`!playlist remove <nombre> <posición>` — saca una canción\n"
        "`!playlist delete <nombre>` — elimina la playlist completa"
    )


@playlist.command(name="create")
async def pl_create(ctx, *, nombre: str = None):
    if not nombre:
        return await ctx.send("❌ Uso: `!playlist create <nombre>`")
    nombre = nombre.lower().strip()
    data = _load_playlists(ctx.guild.id)
    if nombre in data:
        return await ctx.send(f"❌ Ya existe la playlist **{nombre}**.")
    data[nombre] = []
    _save_playlists(ctx.guild.id, data)
    await ctx.send(f"✅ Playlist **{nombre}** creada. Agrega canciones con `!playlist add {nombre}`.")


@playlist.command(name="add")
async def pl_add(ctx, *, nombre: str = None):
    if not nombre:
        return await ctx.send("❌ Uso: `!playlist add <nombre>`")
    nombre = nombre.lower().strip()
    song = current_song.get(ctx.guild.id)
    if not song:
        return await ctx.send("❌ No hay ninguna canción sonando ahora.")
    data = _load_playlists(ctx.guild.id)
    if nombre not in data:
        return await ctx.send(f"❌ No existe la playlist **{nombre}**. Créala con `!playlist create {nombre}`.")
    entry = {"title": song["title"], "url": song["url"]}
    if entry in data[nombre]:
        return await ctx.send(f"⚠️ **{song['title']}** ya está en **{nombre}**.")
    data[nombre].append(entry)
    _save_playlists(ctx.guild.id, data)
    await ctx.send(f"✅ **{song['title']}** agregada a **{nombre}** ({len(data[nombre])} canciones).")


@playlist.command(name="load")
async def pl_load(ctx, *, nombre: str = None):
    if not nombre:
        return await ctx.send("❌ Uso: `!playlist load <nombre>`")
    nombre = nombre.lower().strip()
    data = _load_playlists(ctx.guild.id)
    if nombre not in data or not data[nombre]:
        return await ctx.send(f"❌ La playlist **{nombre}** no existe o está vacía.")
    if not ctx.author.voice:
        return await ctx.send("❌ Debes estar en un canal de voz.")
    async with get_voice_lock(ctx.guild.id):
        if not ctx.voice_client or not ctx.voice_client.is_connected():
            try:
                await ctx.author.voice.channel.connect(timeout=60)
            except Exception as e:
                return await ctx.send(f"❌ No pude conectarme al canal de voz: {e}")
    last_text_channel[ctx.guild.id] = ctx.channel
    songs = data[nombre]
    for entry in songs:
        queues.setdefault(ctx.guild.id, []).append((entry["url"], entry["title"]))
    await ctx.send(f"📋 **{nombre}** cargada — {len(songs)} canciones añadidas a la cola.")
    await ensure_playing(ctx)


@playlist.command(name="list")
async def pl_list(ctx):
    data = _load_playlists(ctx.guild.id)
    if not data:
        return await ctx.send("❌ No hay playlists guardadas en este servidor.")
    embed = discord.Embed(title="📋 Playlists del servidor", color=discord.Color.blurple())
    lines = [f"**{nombre}** — {len(canciones)} canciones" for nombre, canciones in data.items()]
    embed.description = "\n".join(lines)
    await ctx.send(embed=embed)


@playlist.command(name="show")
async def pl_show(ctx, *, nombre: str = None):
    if not nombre:
        return await ctx.send("❌ Uso: `!playlist show <nombre>`")
    nombre = nombre.lower().strip()
    data = _load_playlists(ctx.guild.id)
    if nombre not in data:
        return await ctx.send(f"❌ No existe la playlist **{nombre}**.")
    songs = data[nombre]
    if not songs:
        return await ctx.send(f"📋 **{nombre}** está vacía.")
    embed = discord.Embed(title=f"📋 {nombre} ({len(songs)} canciones)", color=discord.Color.blurple())
    lines = [f"`{i+1}.` {s['title']}" for i, s in enumerate(songs[:20])]
    if len(songs) > 20:
        lines.append(f"*...y {len(songs) - 20} más*")
    embed.description = "\n".join(lines)
    await ctx.send(embed=embed)


@playlist.command(name="remove")
async def pl_remove(ctx, nombre: str = None, posicion: int = None):
    if not nombre or posicion is None:
        return await ctx.send("❌ Uso: `!playlist remove <nombre> <posición>`")
    nombre = nombre.lower().strip()
    data = _load_playlists(ctx.guild.id)
    if nombre not in data:
        return await ctx.send(f"❌ No existe la playlist **{nombre}**.")
    songs = data[nombre]
    if posicion < 1 or posicion > len(songs):
        return await ctx.send(f"❌ Posición inválida. La playlist tiene {len(songs)} canciones.")
    removed = songs.pop(posicion - 1)
    _save_playlists(ctx.guild.id, data)
    await ctx.send(f"🗑️ **{removed['title']}** eliminada de **{nombre}**.")


@playlist.command(name="delete")
async def pl_delete(ctx, *, nombre: str = None):
    if not nombre:
        return await ctx.send("❌ Uso: `!playlist delete <nombre>`")
    nombre = nombre.lower().strip()
    data = _load_playlists(ctx.guild.id)
    if nombre not in data:
        return await ctx.send(f"❌ No existe la playlist **{nombre}**.")
    del data[nombre]
    _save_playlists(ctx.guild.id, data)
    await ctx.send(f"🗑️ Playlist **{nombre}** eliminada.")


@bot.command(name="reiniciar", aliases=["restart"])
async def reiniciar(ctx):
    """Reinicia el proceso del bot. systemd (Restart=always) lo vuelve a levantar."""
    await ctx.send("🔄 Reiniciando el bot... vuelvo en unos segundos.")
    for vc in list(bot.voice_clients):
        try:
            await vc.disconnect(force=True)
        except Exception:
            pass
    await bot.close()
    # Salida limpia: en el mini PC systemd lo reinicia automaticamente.
    os._exit(0)


ALONE_TIMEOUT = 180  # segundos hasta desconectarse si el canal queda vacio


async def _alone_timeout(vc: discord.VoiceClient, gid: int) -> None:
    await asyncio.sleep(ALONE_TIMEOUT)
    if not vc.is_connected():
        return
    if any(not m.bot for m in vc.channel.members):
        return
    cleanup_audio_file(gid)
    queues.pop(gid, None)
    current_song.pop(gid, None)
    loop_mode.pop(gid, None)
    radio_query.pop(gid, None)
    radio_played.pop(gid, None)
    alone_tasks.pop(gid, None)
    async with get_voice_lock(gid):
        if vc.is_connected():
            await vc.disconnect()
    ch = last_text_channel.get(gid)
    if ch:
        try:
            await ch.send("👋 Me fui porque quedé solo en el canal.")
        except Exception:
            pass


@bot.event
async def on_voice_state_update(member, before, after):
    for vc in bot.voice_clients:
        if vc.guild != member.guild or not vc.is_connected():
            continue
        gid = vc.guild.id
        humans = [m for m in vc.channel.members if not m.bot]
        if not humans:
            if gid not in alone_tasks or alone_tasks[gid].done():
                alone_tasks[gid] = asyncio.create_task(_alone_timeout(vc, gid))
        else:
            task = alone_tasks.pop(gid, None)
            if task and not task.done():
                task.cancel()


@bot.event
async def on_ready():
    print(f"[OK] {bot.user} listo.")


bot.run(os.getenv("DISCORD_TOKEN"))
