import base64
import os
import asyncio
import random
import discord
import yt_dlp
import lyricsgenius
import re

from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

# ──────────────────── GENIUS ────────────────────
genius = lyricsgenius.Genius(
    os.getenv("GENIUS_TOKEN"),
    skip_non_songs=True,
    remove_section_headers=True,
    verbose=False
)

# ──────────────────── TOKENS ────────────────────
PO_TOKEN = os.getenv("YOUTUBE_PO_TOKEN", "").strip()
VISITOR_DATA = os.getenv("YOUTUBE_VISITOR_DATA", "").strip()


# ──────────────────── DISCORD ────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ──────────────────── YT-DLP ────────────────────
ytdlp_common_opts = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "nocheckcertificate": True,
    "js_runtimes": {"node": {}},
    "remote_components": {"ejs:github"},
    "cookiefile": "cookies.txt" if os.path.exists("cookies.txt") else None,
    "extractor_args": {
        "youtube": {
            "player_client": ["android"],
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

# ──────────────────── HELPERS ────────────────────
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


def build_ytdlp_opts(is_search: bool) -> dict:
    opts = ytdlp_common_opts.copy()
    if is_search:
        opts.update({
            "default_search": "ytsearch1",
            "extract_flat": "in_playlist",
        })
    return opts


async def ytdlp_extract(loop, query: str, is_search: bool = False) -> dict:
    os.environ["YT_DLP_JS_RUNTIME"] = "node"
    opts = build_ytdlp_opts(is_search)

    def _extract():
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(query, download=False)

    return await loop.run_in_executor(None, _extract)


def pick_best_audio_url(info: dict) -> str | None:
    formats = info.get("formats") or []

    # 1️⃣ Preferir audio-only
    audio_only = [
        f for f in formats
        if f.get("acodec") not in (None, "none")
        and f.get("vcodec") == "none"
        and f.get("url")
    ]

    if audio_only:
        audio_only.sort(key=lambda x: (x.get("abr") or 0), reverse=True)
        return audio_only[0]["url"]

    # 2️⃣ Fallback seguro: audio + video (itag 18, 22, etc.)
    fallback = [
        f for f in formats
        if f.get("acodec") not in (None, "none")
        and f.get("url")
    ]

    if fallback:
        fallback.sort(key=lambda x: (x.get("abr") or 0), reverse=True)
        return fallback[0]["url"]

    return None

# ──────────────────── AUTOPLAY ────────────────────
async def autoplay_next(ctx):
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

# ──────────────────── PLAY NEXT ────────────────────
async def play_next(ctx):
    gid = ctx.guild.id
    queue = queues.get(gid)

    if not queue:
        if await autoplay_next(ctx):
            return await play_next(ctx)
        if ctx.voice_client:
            await ctx.voice_client.disconnect()
        return

    url = normalize_youtube_url(queue.pop(0))

    try:
        info = await ytdlp_extract(asyncio.get_event_loop(), url)
        if info.get("entries"):
            info = info["entries"][0]

        audio_url = pick_best_audio_url(info)
        if not audio_url:
            raise RuntimeError("No audio")

        current_song[gid] = info.get("title", "Desconocido")
        last_played_query[gid] = current_song[gid]
        last_video_id[gid] = info.get("id")


        source = discord.FFmpegPCMAudio(
            audio_url,
            before_options=(
                "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 "
                "-headers \"User-Agent: Mozilla/5.0\" "
                "-headers \"Accept-Language: en-US,en;q=0.9\" "
                "-vn"
            )
        )
        if not ctx.voice_client or not ctx.voice_client.is_connected():
            return

        ctx.voice_client.play(source, after=lambda e: bot.loop.create_task(play_next(ctx)))
        await ctx.send(f"🎶 Reproduciendo: **{current_song[gid]}**")

    except Exception as e:
        print(f"Play error: {e}")
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
            return await ctx.send(
                "❌ No pude conectarme al canal de voz (timeout). Intenta otra vez."
            )

    await ctx.send(f"🔍 Buscando: **{search}**...")

    loop = asyncio.get_event_loop()
    try:
        info = await ytdlp_extract(loop, search, is_search=True)

        entries = info.get("entries") if isinstance(info, dict) else None
        if not entries:
            return await ctx.send("❌ No se encontraron resultados.")

        video = entries[0]
        url = normalize_youtube_url(video.get("webpage_url") or video.get("url"))
        title = video.get("title", "Canción")

        queues.setdefault(ctx.guild.id, []).append(url)

        if ctx.voice_client.is_playing():
            await ctx.send(f"✅ En cola: **{title}**")
        else:
            await play_next(ctx)

    except Exception as e:
        print(f"Error en comando play: {e}")
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
async def lyrics(ctx, *, song: str):
    title = clean_title_for_lyrics(song)
    song_data = genius.search_song(title)
    if not song_data:
        return await ctx.send("❌ Letra no encontrada.")

    text = song_data.lyrics[:1990]
    await ctx.send(f"🎶 **{song_data.title} – {song_data.artist}**\n\n{text}")


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
async def comandos(ctx):
    help_text = (
        "🎵 **Comandos del Bot de Música:**\n"
        "`!play <canción o URL>` - Reproduce una canción o la añade a la cola.\n"
        "`!skip` - Salta la canción actual.\n"
        "`!stop` - Detiene la reproducción y desconecta el bot.\n"
        "`!lyrics <canción>` - Busca y muestra la letra de una canción.\n"
        "`!autoplay <on/off>` - Activa o desactiva el autoplay.\n"
        "`!comandos` - Muestra esta ayuda.\n"
        "`!repo` - Muestra el enlace al repositorio del bot."
        "`!clear <n>` - Elimina los últimos n mensajes del chat."
    )
    await ctx.send(help_text)

@bot.command()
async def repo(ctx):
    await ctx.send("🔗 Repositorio del bot: https://github.com/bak1-H/BOT_DISCORD_MUSICA")

#para eliminar mensajes del chat !clear <num>
@bot.command()
async def clear(ctx, num: int):
    deleted = await ctx.channel.purge(limit=num + 1)
    await ctx.send(f"🧹 Eliminados {len(deleted)-1} mensajes.", delete_after=5) 


@bot.event
async def on_ready():
    print(f"✅ {bot.user} listo.")


bot.run(os.getenv("DISCORD_TOKEN"))
