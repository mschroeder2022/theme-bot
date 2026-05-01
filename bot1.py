import os
import sys
import json
import asyncio
import logging
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urlparse

import discord
from discord import app_commands
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

# In-memory cache — loaded once at startup, updated on every write
_themes_cache: Dict[str, dict] = {}

VALID_DOMAINS = {
    "youtube.com", "www.youtube.com",
    "youtu.be",
    "soundcloud.com", "www.soundcloud.com",
}

def validate_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return parsed.scheme in ("http", "https") and parsed.netloc in VALID_DOMAINS
    except Exception:
        return False

def load_themes() -> dict:
    if not THEMES_FILE.exists():
        return {}
    try:
        return json.loads(THEMES_FILE.read_text())
    except Exception:
        return {}

def save_themes(data: dict):
    """Write to disk. Caller is responsible for keeping _themes_cache in sync."""
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
            "--match-filter", "duration <= 600",
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
intents.voice_states = True

bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents)

# Per-guild intro queues — serializes playback so intros queue instead of drop
_guild_queues: Dict[int, asyncio.Queue] = {}
_guild_consumer_tasks: Dict[int, asyncio.Task] = {}

def get_guild_queue(guild_id: int) -> asyncio.Queue:
    if guild_id not in _guild_queues:
        _guild_queues[guild_id] = asyncio.Queue()
    return _guild_queues[guild_id]

def ensure_guild_consumer(guild_id: int):
    """Start a queue consumer for this guild if one isn't already running."""
    existing = _guild_consumer_tasks.get(guild_id)
    if existing is None or existing.done():
        _guild_consumer_tasks[guild_id] = asyncio.create_task(
            _guild_queue_consumer(guild_id)
        )

async def _guild_queue_consumer(guild_id: int):
    """Drains the guild intro queue one entry at a time."""
    queue = get_guild_queue(guild_id)
    while True:
        member, channel, cfg = await queue.get()
        try:
            # Re-verify the member is still present when their turn arrives
            if not member.voice or member.voice.channel != channel:
                log.info("%s left before queued intro could play.", member.name)
                continue
            await play_intro(member, channel, cfg)
        except Exception as e:
            log.warning("Queued intro failed for %s: %s", member.name, e)
        finally:
            queue.task_done()


async def get_voice_client(channel: discord.VoiceChannel) -> discord.VoiceClient:
    """
    Get an existing voice client for the guild or connect a new one.
    Reuses existing connections to eliminate reconnect delay entirely.
    If already in the right channel, returns immediately with zero delay.
    """
    vc: Optional[discord.VoiceClient] = discord.utils.get(
        bot.voice_clients, guild=channel.guild
    )

    if vc and vc.is_connected() and vc.channel == channel:
        log.info("Reusing existing voice connection in %s", channel.name)
        return vc

    if vc and vc.is_connected():
        log.info("Moving voice client to %s", channel.name)
        await vc.move_to(channel)
        await asyncio.sleep(0.3)
        return vc

    if vc:
        try:
            await vc.disconnect(force=True)
        except Exception:
            pass
        await asyncio.sleep(0.3)

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

    # mp3 should already be cached — this returns instantly if so
    mp3 = await ensure_cached_mp3(member.id, url)

    vc = await get_voice_client(channel)

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

    try:
        await vc.disconnect(force=False)
    except Exception:
        pass


async def warmup_cache():
    """Pre-download all user audio on startup so first plays are instant."""
    if not _themes_cache:
        return

    log.info("Warming up audio cache for %d user(s)...", len(_themes_cache))
    tasks = []
    uids = []
    for uid, cfg in _themes_cache.items():
        url = cfg.get("url", "")
        if url:
            tasks.append(ensure_cached_mp3(int(uid), url))
            uids.append(uid)

    results = await asyncio.gather(*tasks, return_exceptions=True)
    for uid, result in zip(uids, results):
        if isinstance(result, Exception):
            log.warning("Cache warmup failed for user %s: %s", uid, result)
        else:
            log.info("Cache ready for user %s", uid)


# ---------------- LIFECYCLE ----------------

@bot.event
async def on_ready():
    log.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)

    # Load themes into the in-memory cache
    data = load_themes()
    _themes_cache.clear()
    _themes_cache.update(data)

    # Migrate any legacy entries that lack the "enabled" field
    changed = False
    for cfg in _themes_cache.values():
        if "enabled" not in cfg:
            cfg["enabled"] = True
            changed = True
    if changed:
        save_themes(_themes_cache)

    # Clean up stale voice sessions from previous crashes
    for vc in list(bot.voice_clients):
        try:
            await vc.disconnect(force=True)
        except Exception:
            pass

    # Start a queue consumer for every guild the bot is already in
    for guild in bot.guilds:
        ensure_guild_consumer(guild.id)

    # Register slash commands globally
    await bot.tree.sync()
    log.info("Slash commands synced.")

    await warmup_cache()
    log.info("Bot ready — all caches warm.")


@bot.event
async def on_guild_join(guild: discord.Guild):
    ensure_guild_consumer(guild.id)


# ---------------- SLASH COMMANDS ----------------

@bot.tree.command(name="addtheme", description="Save a YouTube or SoundCloud URL as your intro theme.")
@app_commands.describe(url="YouTube or SoundCloud URL (max 10 minutes)")
async def cmd_addtheme(interaction: discord.Interaction, url: str):
    if not validate_url(url):
        await interaction.response.send_message(
            "❌ Invalid URL. Only youtube.com, youtu.be, and soundcloud.com are accepted.",
            ephemeral=True,
        )
        return

    await interaction.response.defer()

    uid = interaction.user.id
    uid_str = str(uid)

    # Clear old cached file
    p = user_audio_path(uid)
    if p.exists():
        p.unlink()

    # Preserve existing clip settings; apply defaults for new fields
    existing = _themes_cache.get(uid_str, {})
    _themes_cache[uid_str] = {
        "url": url,
        "start": existing.get("start", DEFAULT_START),
        "duration": existing.get("duration", DEFAULT_DURATION),
        "enabled": existing.get("enabled", True),
    }
    save_themes(_themes_cache)

    await interaction.followup.send("✅ Theme saved! Downloading audio now...")

    async def _prefetch():
        try:
            await ensure_cached_mp3(uid, url)
            await interaction.followup.send(
                "✅ Audio ready — your intro will play instantly on join!"
            )
        except Exception as e:
            log.warning("Prefetch failed for user %s: %s", uid, e)
            err = str(e)
            if "does not pass filter" in err or "duration" in err.lower():
                await interaction.followup.send(
                    "❌ Video is over 10 minutes. Please choose a shorter track."
                )
            else:
                await interaction.followup.send(
                    "⚠️ Audio download failed. Check the URL and try again."
                )

    asyncio.create_task(_prefetch())


@bot.tree.command(name="setclip", description="Set which part of your theme audio to play.")
@app_commands.describe(
    start="Start position in seconds (>= 0)",
    duration="How many seconds to play (1–30)",
)
async def cmd_setclip(interaction: discord.Interaction, start: int, duration: int):
    if start < 0:
        await interaction.response.send_message("❌ start must be >= 0.", ephemeral=True)
        return
    if not (1 <= duration <= 30):
        await interaction.response.send_message(
            "❌ duration must be between 1 and 30 seconds.", ephemeral=True
        )
        return

    cfg = _themes_cache.get(str(interaction.user.id))
    if not cfg:
        await interaction.response.send_message(
            "❌ No theme set. Use `/addtheme` first.", ephemeral=True
        )
        return

    cfg["start"] = start
    cfg["duration"] = duration
    save_themes(_themes_cache)
    await interaction.response.send_message(
        f"✅ Clip updated: start={start}s, duration={duration}s."
    )


@bot.tree.command(name="cleartheme", description="Remove your intro theme and delete the cached audio file.")
async def cmd_cleartheme(interaction: discord.Interaction):
    uid_str = str(interaction.user.id)
    if uid_str not in _themes_cache:
        await interaction.response.send_message(
            "ℹ️ You don't have a theme set.", ephemeral=True
        )
        return

    _themes_cache.pop(uid_str)
    save_themes(_themes_cache)

    p = user_audio_path(interaction.user.id)
    if p.exists():
        p.unlink()

    await interaction.response.send_message("✅ Theme cleared.")


@bot.tree.command(name="toggletheme", description="Enable or disable your intro theme without removing it.")
async def cmd_toggletheme(interaction: discord.Interaction):
    cfg = _themes_cache.get(str(interaction.user.id))
    if not cfg:
        await interaction.response.send_message(
            "❌ No theme set. Use `/addtheme` first.", ephemeral=True
        )
        return

    new_state = not cfg.get("enabled", True)
    cfg["enabled"] = new_state
    save_themes(_themes_cache)

    label = "enabled ✅" if new_state else "disabled ⏸️"
    await interaction.response.send_message(f"Your intro theme is now **{label}**.")


@bot.tree.command(name="mytheme", description="Show your current intro theme configuration.")
async def cmd_mytheme(interaction: discord.Interaction):
    cfg = _themes_cache.get(str(interaction.user.id))
    if not cfg:
        await interaction.response.send_message(
            "ℹ️ You have no theme set. Use `/addtheme` to get started.",
            ephemeral=True,
        )
        return

    url      = cfg.get("url", "—")
    start    = cfg.get("start", DEFAULT_START)
    duration = cfg.get("duration", DEFAULT_DURATION)
    enabled  = cfg.get("enabled", True)
    cached   = user_audio_path(interaction.user.id)
    is_cached = cached.exists() and cached.stat().st_size > 50000

    embed = discord.Embed(title="🎵 Your Intro Theme", color=discord.Color.blurple())
    embed.add_field(name="URL", value=url, inline=False)
    embed.add_field(name="Start", value=f"{start}s", inline=True)
    embed.add_field(name="Duration", value=f"{duration}s", inline=True)
    embed.add_field(name="Enabled", value="✅ Yes" if enabled else "⏸️ No", inline=True)
    embed.add_field(name="Audio Cached", value="✅ Yes" if is_cached else "⏳ Not yet", inline=True)

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="help", description="List all available slash commands.")
async def cmd_help(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🤖 Theme Bot — Commands",
        description="Plays a custom audio clip when you join a voice channel.",
        color=discord.Color.green(),
    )
    embed.add_field(
        name="/addtheme <url>",
        value="Set a YouTube or SoundCloud URL as your intro (max 10 min).",
        inline=False,
    )
    embed.add_field(
        name="/setclip <start> <duration>",
        value="Choose which part to play — e.g. `/setclip 30 8` plays 8s starting at 30s.",
        inline=False,
    )
    embed.add_field(
        name="/cleartheme",
        value="Remove your theme and delete the cached audio file.",
        inline=False,
    )
    embed.add_field(
        name="/toggletheme",
        value="Enable or disable your intro without removing it.",
        inline=False,
    )
    embed.add_field(
        name="/mytheme",
        value="Show your current theme settings.",
        inline=False,
    )
    embed.add_field(
        name="/help",
        value="Show this message.",
        inline=False,
    )
    await interaction.response.send_message(embed=embed)


# ---------------- VOICE EVENTS ----------------

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

    cfg = _themes_cache.get(str(member.id))
    if not cfg:
        return

    # Skip entirely if the user has disabled their theme
    if not cfg.get("enabled", True):
        return

    url = cfg.get("url", "")
    if not url:
        return

    # Kick off cache fetch in parallel — by the time the queue consumer
    # gets to this intro, the mp3 will already be on disk
    cache_task = asyncio.create_task(ensure_cached_mp3(member.id, url))

    async def _run():
        # Minimal delay for Discord to settle the voice state
        await asyncio.sleep(0.2)

        # Verify the member is still in that channel
        if not member.voice or member.voice.channel != after.channel:
            log.info("%s left before intro could play.", member.name)
            cache_task.cancel()
            return

        try:
            await cache_task
        except Exception as e:
            log.warning("Cache fetch failed for %s: %s", member.name, e)
            return

        ensure_guild_consumer(after.channel.guild.id)
        queue = get_guild_queue(after.channel.guild.id)
        await queue.put((member, after.channel, cfg))

    asyncio.create_task(_run())


# ---------------- ENTRYPOINT ----------------

def main():
    load_env()
    ensure_dirs()

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
                "Could not load Opus from any of %s — voice will not work. "
                "Fix: sudo apt install libopus0",
                opus_names,
            )

    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        log.error("DISCORD_BOT_TOKEN missing from .env")
        sys.exit(1)

    bot.run(token)


if __name__ == "__main__":
    main()
