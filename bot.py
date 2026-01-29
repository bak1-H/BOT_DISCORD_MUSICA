import base64
import os
import asyncio
import functools

import discord
from discord.ext import commands
import yt_dlp
from dotenv import load_dotenv

load_dotenv()

PO_TOKEN = os.getenv("YOUTUBE_PO_TOKEN", "").strip()
VISITOR_DATA = os.getenv("YOUTUBE_VISITOR_DATA", "").strip()

cookies_b64 = os.getenv("YOUTUBE_COOKIES_B64")
if cookies_b64:
    with open("cookies.txt", "wb") as f:
        f.write(base64.b64decode(cookies_b64))

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

ytdlp_common_opts = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "nocheckcertificate": True,
    "js_runtimes": {"node": {}},
    "remote_components": {"ejs:github"},
    "cookiefile": "cookies.txt" if os.path.exists("cookies.txt") else None,
    "extractor_args": {
        "youtube": {
            "player_client": ["web"],
            "po_token": [f"web+{PO_TOKEN}"] if PO_TOKEN else [],
            "visitor_data": [VISITOR_DATA] if VISITOR_DATA else [],
        }
    },
}

queues = {}
current_song = {}


def normalize_youtube_url(value: str | None) -> str | None:
    if not value:
        return None
    if value.startswith("http"):
        return value
    return f"https://www.youtube.com/watch?v={value}"


def build_ytdlp_opts(is_search: bool) -> dict:
    opts = ytdlp_common_opts.copy()
    opts.update(
        {
            "allow_unplayable_formats": True,
            "check_formats": False,
            "javascript_executable": "/usr/bin/node",
        }
    )
    if is_search:
        opts.update(
            {
                "default_search": "ytsearch1",
                "extract_flat": "in_playlist",
            }
        )
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
    audio_formats = [
        f for f in formats if f.get("acodec") not in (None, "none") and f.get("url")
    ]
    if audio_formats:
        audio_formats.sort(key=lambda x: (x.get("abr") or 0), reverse=True)
        return audio_formats[0]["url"]
    return info.get("url")


async def play_next(ctx):
    guild_id = ctx.guild.id
    queue = queues.get(guild_id)

    if not queue:
        if ctx.voice_client:
            await ctx.voice_client.disconnect()
        return

    url = normalize_youtube_url(queue.pop(0))
    loop = asyncio.get_event_loop()

    try:
        info = await ytdlp_extract(loop, url)

        if isinstance(info, dict) and info.get("entries"):
            info = info["entries"][0]

        audio_url = pick_best_audio_url(info)
        if not audio_url:
            raise RuntimeError("No se encontró una URL de audio reproducible.")

        ffmpeg_opts = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -vn"
        source = discord.FFmpegPCMAudio(audio_url, before_options=ffmpeg_opts)

        current_song[guild_id] = info.get("title", "Desconocido")
        ctx.voice_client.play(source, after=lambda e: bot.loop.create_task(play_next(ctx)))
        await ctx.send(f"🎶 Reproduciendo: **{current_song[guild_id]}**")

    except Exception as e:
        print(f"Error en play_next: {e}")
        await ctx.send("❌ Error al intentar reproducccir esta canción. Pasando a la siguiente...")
        await play_next(ctx)


@bot.command()
async def play(ctx, *, search: str = None):
    if not search:
        return await ctx.send("❌ Escribe el nombre de una canción.")

    if not ctx.voice_client:
        if ctx.author.voice:
            await ctx.author.voice.channel.connect()
        else:
            return await ctx.send("❌ Debes estar en un canal de voz.")

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


@bot.event
async def on_ready():
    print(f"✅ {bot.user} online y listo.")


bot.run(os.getenv("DISCORD_TOKEN"))
