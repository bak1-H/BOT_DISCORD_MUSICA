import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

# Health check arranca primero para que Fly.io lo detecte durante el deploy
class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, *_):
        pass

def _start_health_server():
    HTTPServer(("0.0.0.0", int(os.getenv("PORT", 8080))), _HealthHandler).serve_forever()

threading.Thread(target=_start_health_server, daemon=True).start()

import asyncio
import random
import re
import copy
import traceback
import discord
from discord.ext import commands
from dotenv import load_dotenv
import base64
import yt_dlp
import lyricsgenius

load_dotenv()

os.environ["YT_DLP_JS_RUNTIME"] = "node"

# Agrega ffmpeg local al PATH si no está disponible globalmente
import sys
import shutil
import subprocess
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
bot = commands.Bot(command_prefix="!", intents=intents)

# ──────────────────── CONFIG ────────────────────
YTDLP_PROXY = os.getenv("YTDLP_PROXY", "").strip() or None
MAX_PLAYNEXT_FAILS = 3
PO_TOKEN = os.getenv("YOUTUBE_PO_TOKEN", "").strip()
VISITOR_DATA = os.getenv("YOUTUBE_VISITOR_DATA", "").strip()
YT_CLIENTS = ["web", "android", "ios"]

# ──────────────────── YT-DLP BASE CONFIG ────────────────────
_YTDLP_BASE = {
    "format": "bestaudio[acodec!=none]/bestaudio/best",
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


async def extract_audio_with_fallback(query: str) -> tuple[dict, str, str]:
    """Try each player_client until one returns a valid audio URL."""
    last_error = None
    for client in YT_CLIENTS:
        try:
            info = await ytdlp_extract(query, is_search=False, client=client)
            if isinstance(info, dict) and info.get("entries"):
                info = info["entries"][0]
            audio_url = pick_best_audio_url(info)
            if audio_url:
                return info, audio_url, client
        except Exception as e:
            last_error = e
    if last_error:
        raise last_error
    raise RuntimeError("No se obtuvo un audio URL con ningún client")


def pick_best_audio_url(info: dict) -> str | None:
    formats = info.get("formats") or []
    audio_only = [
        f for f in formats
        if f.get("acodec") not in (None, "none") and f.get("vcodec") == "none" and f.get("url")
    ]
    if audio_only:
        return max(audio_only, key=lambda x: x.get("abr") or 0)["url"]

    av_with_audio = [
        f for f in formats
        if f.get("acodec") not in (None, "none") and f.get("url")
    ]
    if av_with_audio:
        return max(av_with_audio, key=lambda x: x.get("tbr") or 0)["url"]
    return None


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
    queue = queues.get(gid) or []
    playnext_fail_count.setdefault(gid, 0)

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
        info, _, _ = await extract_audio_with_fallback(url)

        song = {
            "title": info.get("title", queued_title),
            "url": url,
            "thumbnail": info.get("thumbnail"),
            "duration": info.get("duration"),
            "uploader": info.get("uploader") or info.get("channel"),
        }
        current_song[gid] = song
        last_video_id[gid] = info.get("id")

        # Pipe yt-dlp → FFmpeg: más confiable que pasarle la URL directo.
        # Ejecuta yt-dlp como módulo del mismo Python: funciona en Windows y Linux,
        # sin depender de un binario en el PATH ni de yt-dlp.exe.
        # WebM/Opus no requiere seek al escribir → compatible con pipes
        ydl_cmd = [
            sys.executable, "-m", "yt_dlp", "-o", "-",
            "-f", "bestaudio[ext=webm]/bestaudio[ext=opus]/bestaudio[ext=ogg]/bestaudio",
            "--no-playlist", "-q",
        ]
        if COOKIES_FILE:
            ydl_cmd += ["--cookies", COOKIES_FILE]
        ydl_cmd.append(url)

        ydl_proc = subprocess.Popen(
            ydl_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        async with get_voice_lock(gid):
            if not ctx.voice_client or not ctx.voice_client.is_connected():
                ydl_proc.kill()
                return

            source = discord.FFmpegPCMAudio(ydl_proc.stdout, pipe=True, options="-vn")
            ctx.voice_client.play(
                source,
                after=lambda e: bot.loop.create_task(play_next(ctx)),
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
    queues[gid] = []
    current_song.pop(gid, None)
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
    embed.add_field(name="!radio <estilo>", value="Reproduce canciones del estilo en bucle. `!radio off` para detener.", inline=False)
    embed.add_field(name="!clear <n>", value="Elimina los últimos n mensajes (requiere permisos).", inline=False)
    embed.add_field(name="!reiniciar", value="Reinicia el bot si se quedó bugueado.", inline=False)
    embed.add_field(name="!repo", value="Enlace al repositorio del bot.", inline=False)
    await ctx.send(embed=embed)


@bot.command()
async def repo(ctx):
    await ctx.send("🔗 Repositorio: https://github.com/bak1-H/BOT_DISCORD_MUSICA")


@bot.command()
@commands.has_permissions(manage_messages=True)
async def clear(ctx, num: int):
    if num < 1:
        return await ctx.send("❌ Usa un número mayor a 0.")
    deleted = await ctx.channel.purge(limit=num + 1)
    await ctx.send(f"🧹 Eliminados {len(deleted) - 1} mensajes.", delete_after=5)


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


@bot.event
async def on_ready():
    print(f"[OK] {bot.user} listo.")


bot.run(os.getenv("DISCORD_TOKEN"))
