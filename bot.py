import os
import asyncio
import random
import re
import copy
import discord
from discord.ext import commands
from dotenv import load_dotenv
import base64
import yt_dlp
import lyricsgenius

load_dotenv()

os.environ["YT_DLP_JS_RUNTIME"] = "node"

# ──────────────────── COOKIES ────────────────────
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
autoplay_enabled: dict[int, bool] = {}
last_played_query: dict[int, str] = {}
last_video_id: dict[int, str] = {}
playnext_fail_count: dict[int, int] = {}

# ──────────────────── HELPERS ────────────────────

def format_duration(seconds) -> str:
    if not seconds:
        return "?"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def ffmpeg_headers_from_info(info: dict) -> str:
    headers = dict(info.get("http_headers") or {})
    headers.setdefault("User-Agent", "Mozilla/5.0")
    headers.setdefault("Accept-Language", "en-US,en;q=0.9")
    headers.setdefault("Referer", "https://www.youtube.com/")
    headers.setdefault("Origin", "https://www.youtube.com")

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
    opts["extractor_args"] = {
        "youtube": {
            "player_client": [client],
            "po_token": [f"{client}+{PO_TOKEN}"] if PO_TOKEN else [],
            "visitor_data": [VISITOR_DATA] if VISITOR_DATA else [],
        }
    }
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
        info = await ytdlp_extract(query, is_search=True, search_count=5)
        entries = info.get("entries") if isinstance(info, dict) else None
        if not entries:
            return False
        candidates = [e for e in entries if e.get("id") != last_id]
        if not candidates:
            return False
        pick = random.choice(candidates)
        url = normalize_youtube_url(pick.get("webpage_url") or pick.get("url"))
        title = pick.get("title", "Desconocido")
        if not url:
            return False
        queues.setdefault(gid, []).append((url, title))
        return True
    except Exception as e:
        print(f"Autoplay error: {e}")
        return False


# ──────────────────── PLAY NEXT ────────────────────

async def play_next(ctx):
    gid = ctx.guild.id
    queue = queues.get(gid) or []
    playnext_fail_count.setdefault(gid, 0)

    if not queue:
        if await autoplay_next(ctx):
            return await play_next(ctx)
        current_song.pop(gid, None)
        if ctx.voice_client:
            await ctx.voice_client.disconnect()
        return

    url_raw, queued_title = queue.pop(0)
    queues[gid] = queue
    url = normalize_youtube_url(url_raw)

    try:
        if not ctx.voice_client or not ctx.voice_client.is_connected():
            return

        info, audio_url, _ = await extract_audio_with_fallback(url)

        song = {
            "title": info.get("title", queued_title),
            "url": url,
            "thumbnail": info.get("thumbnail"),
            "duration": info.get("duration"),
            "uploader": info.get("uploader") or info.get("channel"),
        }
        current_song[gid] = song
        last_played_query[gid] = song["title"]
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
            after=lambda e: bot.loop.create_task(play_next(ctx)),
        )

        await ctx.send(embed=make_song_embed(song))
        playnext_fail_count[gid] = 0

    except Exception as e:
        playnext_fail_count[gid] = playnext_fail_count.get(gid, 0) + 1
        print(f"Play error: {e}")

        if playnext_fail_count[gid] == 1:
            await ctx.send(f"❌ Error al reproducir: {e}")

        if is_youtube_login_block(e):
            await ctx.send("❌ YouTube bloqueó la reproducción (bot-check). Reexporta las cookies.")
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
            await ctx.author.voice.channel.connect(timeout=60)
        except asyncio.TimeoutError:
            return await ctx.send("❌ No pude conectarme al canal de voz (timeout).")
        except (discord.Forbidden, discord.HTTPException, discord.ClientException) as e:
            print(f"Voice connect error: {e}")
            return await ctx.send("❌ No pude conectarme al canal de voz (permisos/capacidad).")

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

        if ctx.voice_client and ctx.voice_client.is_playing():
            song_preview = {
                "title": title,
                "url": url,
                "thumbnail": video.get("thumbnail"),
                "duration": video.get("duration"),
                "uploader": video.get("uploader") or video.get("channel"),
            }
            await ctx.send(embed=make_song_embed(song_preview, in_queue=True))
        else:
            await play_next(ctx)

    except Exception as e:
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
    queues[ctx.guild.id] = []
    current_song.pop(ctx.guild.id, None)
    if ctx.voice_client:
        ctx.voice_client.stop()
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
        await ctx.send(f"Autoplay: {'🟢 ON' if state else '🔴 OFF'}")


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
    embed.add_field(name="!autoplay <on/off>", value="Activa o desactiva el autoplay.", inline=False)
    embed.add_field(name="!clear <n>", value="Elimina los últimos n mensajes (requiere permisos).", inline=False)
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


@bot.event
async def on_ready():
    print(f"[OK] {bot.user} listo.")


bot.run(os.getenv("DISCORD_TOKEN"))
