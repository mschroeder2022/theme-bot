import os
import sys
import json
import asyncio
import logging
from pathlib import Path
from typing import Dict, Optional

import discord
from discord.ext import commands
from dotenv import load_dotenv

# ---------------- PATHS ----------------
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DOWNLOADS_DIR = DATA_DIR / "downloads"
THEMES_FILE = DATA_DIR / "themes.json"

# ---------------- LOGGING ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("theme-bot")

# ---------------- ENV ----------------
def load_env():
    load_dotenv(BASE_DIR / ".env")

def ensure_dirs():
    DATA_DIR.mkdir(exist_ok=True)
    DOWNLOADS_DIR.mkdir(exist_ok=True)

# ---------------- THEME STORAGE ----------------
DEFAULT_START = 0
DEFAULT_DURATION = 8

def load_themes() -> dict:
    if not THEMES_FILE.exists():
        return {}
    try:
        return json.loads(THEMES_FILE.read_text())
    except Exception:
        return {}

def save_themes(data: dict):
    THEMES_FILE.write_text(json.dumps(data, indent=2))

def user_audio_path(uid: int) -> Path:
    return DOWNLOADS_DIR / f"user_{uid}.mp3"

# ---------------- DOWNLOAD ----------------
_user_locks: Dict[int, asyncio.Lock] = {}

def get_user_lock(uid: int) -> asyncio.Lock:
    if uid not in _user_locks:
        _user_locks[uid] = asyncio.Lock()
    return _user_locks[uid]

async def ensure_cached_mp3(uid: int, url: str) -> Path:
    """Returns cached mp3 instantly if already downloaded, otherwise downloads it."""
    lock = get_user_lock(uid)
    async with lock:
        out = user_audio_path(uid)
        if out.exists() and out.stat().st_size > 50000:
            return out

        log.info("Downloading audio for user %s ...", uid)
        cmd = [
            "yt-dlp", "-x",
            "--audio-format", "mp3",
            "--audio-quality", "0",
            "-o", str(out.with_suffix(".%(ext)s")),
            url,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr_b = await proc.communicate()

        if not out.exists() or out.stat().st_size < 50000:
            raise RuntimeError(f"Download failed: {stderr_b.decode(errors='ignore')[-500:]}")

        log.info("Download complete (%d bytes)", out.stat().st_size)
        return out

# ---------------- DISCORD ----------------
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Per-guild playback lock — ensures only one intro plays at a time
_guild_locks: Dict[int, asyncio.Lock] = {}

def get_guild_lock(guild_id: int) -> asyncio.Lock:
    if guild_id not in _guild_locks:
        _guild_locks[guild_id] = asyncio.Lock()
    return _guild_locks[guild_id]


async def get_voice_client(channel: discord.VoiceChannel) -> discord.VoiceClient:
    """
    Get an existing voice client for the guild or connect a new one.
    Reuses existing connections to eliminate reconnect delay entirely.
    If already in the right channel, returns immediately with zero delay.
    """
    vc: Optional[discord.VoiceClient] = discord.utils.get(
        bot.voice_clients, guild=channel.guild
    )

    # Already connected to the correct channel — reuse it, zero delay
    if vc and vc.is_connected() and vc.channel == channel:
        log.info("Reusing existing voice connection in %s", channel.name)
        return vc

    # Connected but wrong channel — move instead of reconnect
    if vc and vc.is_connected():
        log.info("Moving voice client to %s", channel.name)
        await vc.move_to(channel)
        await asyncio.sleep(0.3)
        return vc

    # Stale/dead client — clean it up
    if vc:
        try:
            await vc.disconnect(force=True)
        except Exception:
            pass
        await asyncio.sleep(0.3)

    # Fresh connect
    log.info("Connecting to voice channel %s", channel.name)
    vc = await channel.connect(timeout=30, reconnect=False)
    await asyncio.sleep(0.5)
    return vc


async def play_intro(member: discord.Member, channel: discord.VoiceChannel, cfg: dict):
    url      = cfg.get("url", "")
    start    = int(cfg.get("start", DEFAULT_START))
    duration = int(cfg.get("duration", DEFAULT_DURATION))

    if not url:
        return

    # mp3 should already be cached from the prefetch on join event
    # this returns instantly if already cached
    mp3 = await ensure_cached_mp3(member.id, url)

    vc = await get_voice_client(channel)

    if vc.is_playing():
        log.info("Already playing in %s, skipping %s", channel.name, member.name)
        return

    source = discord.FFmpegPCMAudio(
        str(mp3),
        before_options=f"-nostdin -ss {start}",
        options=f"-t {duration} -vn",
    )
    audio = discord.PCMVolumeTransformer(source, volume=1.0)

    vc.play(audio)
    log.info("▶ Playing intro for %s", member.name)

    while vc.is_playing():
        await asyncio.sleep(0.05)

    log.info("■ Intro finished for %s", member.name)

    # Disconnect after playing — leaves the channel clean for next join
    try:
        await vc.disconnect(force=False)
    except Exception:
        pass


async def warmup_cache():
    """Pre-download all user audio on startup so first plays are instant."""
    themes = load_themes()
    if not themes:
        return

    log.info("Warming up audio cache for %d user(s)...", len(themes))
    tasks = []
    for uid, cfg in themes.items():
        url = cfg.get("url", "")
        if url:
            tasks.append(ensure_cached_mp3(int(uid), url))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    for uid, result in zip(themes.keys(), results):
        if isinstance(result, Exception):
            log.warning("Cache warmup failed for user %s: %s", uid, result)
        else:
            log.info("Cache ready for user %s", uid)


@bot.event
async def on_ready():
    log.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)

    # Clean up any stale voice sessions from previous crashes
    for vc in list(bot.voice_clients):
        try:
            await vc.disconnect(force=True)
        except Exception:
            pass

    # Download all audio in parallel so nothing waits on first join
    await warmup_cache()
    log.info("Bot ready — all caches warm.")


@bot.event
async def on_command_error(ctx: commands.Context, error: Exception):
    if isinstance(error, commands.CommandNotFound):
        return
    log.error("Command error: %s", error)


@bot.command()
async def addtheme(ctx: commands.Context, url: str):
    themes = load_themes()
    themes[str(ctx.author.id)] = {
        "url": url,
        "start": DEFAULT_START,
        "duration": DEFAULT_DURATION,
    }
    save_themes(themes)

    # Invalidate old cache
    p = user_audio_path(ctx.author.id)
    if p.exists():
        p.unlink()

    await ctx.send("✅ Theme saved! Downloading audio now...")

    async def _prefetch():
        try:
            await ensure_cached_mp3(ctx.author.id, url)
            await ctx.send("✅ Audio ready — your intro will play instantly on join!")
        except Exception as e:
            log.warning("Prefetch failed: %s", e)
            await ctx.send("⚠️ Audio prefetch failed. It will download on your first join.")

    bot.loop.create_task(_prefetch())


@bot.command()
async def setclip(ctx: commands.Context, start: int, duration: int):
    if start < 0:
        await ctx.send("❌ start must be >= 0")
        return
    if not (1 <= duration <= 30):
        await ctx.send("❌ duration must be 1–30 seconds")
        return

    themes = load_themes()
    cfg = themes.get(str(ctx.author.id))
    if not cfg:
        await ctx.send("❌ No theme set. Use `!addtheme <url>` first.")
        return

    cfg["start"] = start
    cfg["duration"] = duration
    save_themes(themes)
    await ctx.send(f"✅ Clip updated: start={start}s, duration={duration}s")


@bot.command()
async def cleartheme(ctx: commands.Context):
    themes = load_themes()
    themes.pop(str(ctx.author.id), None)
    save_themes(themes)
    p = user_audio_path(ctx.author.id)
    if p.exists():
        p.unlink()
    await ctx.send("✅ Theme cleared.")


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
):
    if member.bot:
        return

    # Only fire on fresh joins, not moves or leaves
    if before.channel is not None or after.channel is None:
        return

    themes = load_themes()
    cfg = themes.get(str(member.id))
    if not cfg:
        return

    # Kick off cache fetch immediately and in parallel —
    # by the time the lock is acquired and voice connects, mp3 is ready
    url = cfg.get("url", "")
    if url:
        cache_task = bot.loop.create_task(ensure_cached_mp3(member.id, url))
    else:
        return

    lock = get_guild_lock(after.channel.guild.id)

    async def _run():
        async with lock:
            # Minimal delay — just enough for Discord to settle the voice state
            await asyncio.sleep(0.2)

            # Verify member is still in that channel
            if not member.voice or member.voice.channel != after.channel:
                log.info("%s left before intro could play.", member.name)
                cache_task.cancel()
                return

            # Wait for cache to be ready (usually already done by now)
            try:
                await cache_task
            except Exception as e:
                log.warning("Cache fetch failed for %s: %s", member.name, e)
                return

            try:
                await play_intro(member, after.channel, cfg)
            except Exception as e:
                log.warning("Intro failed for %s: %s", member.name, e)

    bot.loop.create_task(_run())


def main():
    load_env()
    ensure_dirs()

    # Linux Opus loading — tries common library names used by Ubuntu/Debian
    if not discord.opus.is_loaded():
        opus_names = ["libopus.so.0", "libopus.so", "opus"]
        for name in opus_names:
            try:
                discord.opus.load_opus(name)
                log.info("Opus loaded: %s", name)
                break
            except Exception:
                continue
        if not discord.opus.is_loaded():
            log.warning(
                "Could not manually load Opus. "
                "Run: sudo apt install libopus0 libopus-dev"
            )

    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        log.error("DISCORD_BOT_TOKEN missing from .env")
        sys.exit(1)

    bot.run(token)


if __name__ == "__main__":
    main()
