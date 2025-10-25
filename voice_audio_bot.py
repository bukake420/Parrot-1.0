# voice_audio_bot.py — resilient join-sound bot (single-flight connect, hard resets, linger)
# Repo expectations:
#   - Put per-user join sounds in ./audio/ named <discord_user_id>.(mp3|wav|ogg)
#   - Env vars: DISCORD_TOKEN (required), LINGER_SECONDS (default 120), PLAYBACK_VOLUME (default 0.30)
#
# Requires: pip install -U "discord.py[voice]"  (2.4+). ffmpeg must be on PATH.

import os
import sys
import asyncio
import logging
import random
from typing import Dict, Optional, Tuple

import discord
from discord.ext import commands
from discord import FFmpegPCMAudio, PCMVolumeTransformer

# ---------- Windows loop policy ----------
if sys.platform.startswith("win"):
    try:
        from asyncio.windows_events import WindowsSelectorEventLoopPolicy
        asyncio.set_event_loop_policy(WindowsSelectorEventLoopPolicy())
    except Exception:
        pass

# ---------- Config / env ----------
TOKEN = os.getenv("DISCORD_TOKEN", "")
AUDIO_DIR = os.path.join(os.path.dirname(__file__), "audio")
LINGER_SECONDS = int(os.getenv("LINGER_SECONDS", "120"))
PLAYBACK_VOLUME = float(os.getenv("PLAYBACK_VOLUME", "0.30"))
COMMAND_PREFIX = "!"

# ---------- Logging ----------
logger = logging.getLogger("voice-bot")
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
logger.addHandler(handler)
logger.setLevel(logging.INFO)

# ---------- Intents & Bot ----------
intents = discord.Intents.default()
# NOTE: You must also enable Message Content Intent in the Developer Portal.
intents.message_content = True
intents.guilds = True
intents.voice_states = True
intents.members = True

bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)

# ---------- Connect control ----------
_guild_connect_locks: Dict[int, asyncio.Lock] = {}
_linger_tasks: Dict[int, asyncio.Task] = {}  # per-guild linger task
_last_played: Dict[int, float] = {}  # user_id -> unix ts, to avoid spam on rapid rejoin

MAX_ATTEMPTS = 3
CONNECT_TIMEOUT_SECS = 20
REJOIN_SPAM_WINDOW = 3.0  # seconds; ignore joins within this window

def _lock_for_guild(guild_id: int) -> asyncio.Lock:
    lock = _guild_connect_locks.get(guild_id)
    if lock is None:
        lock = asyncio.Lock()
        _guild_connect_locks[guild_id] = lock
    return lock

async def safe_disconnect(vc: Optional[discord.VoiceClient], reason: str = ""):
    if vc and vc.is_connected():
        logger.info(f"🔌 Disconnecting from {vc.channel} {('- ' + reason) if reason else ''}")
        try:
            await vc.disconnect(force=True)
        except Exception as e:
            logger.warning(f"While disconnecting: {e}")

def find_user_audio(user_id: int) -> Optional[str]:
    """Return path to user's audio file if present."""
    for ext in (".mp3", ".wav", ".ogg"):
        p = os.path.join(AUDIO_DIR, f"{user_id}{ext}")
        if os.path.isfile(p):
            return p
    return None

async def ensure_vc(channel: discord.VoiceChannel) -> discord.VoiceClient:
    """Resilient connect with single-flight, hard resets, and backoff."""
    guild = channel.guild
    lock = _lock_for_guild(guild.id)
    async with lock:
        existing = discord.utils.get(bot.voice_clients, guild=guild)
        if existing and existing.channel and existing.is_connected():
            if existing.channel.id == channel.id:
                logger.info(f"✅ Already connected to {channel} — reusing.")
                return existing
            # safer to fully disconnect before moving
            await safe_disconnect(existing, reason="pre-move cleanup")

        last_exc = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                logger.info(f"🔗 Voice connect → {channel.name} (attempt {attempt}/{MAX_ATTEMPTS})")
                vc = await channel.connect(timeout=CONNECT_TIMEOUT_SECS, reconnect=False, self_deaf=True)
                logger.info(f"🎧 Connected to {channel.name}")
                return vc
            except discord.errors.ConnectionClosed as e:
                logger.warning(f"❌ Voice WS closed (code={getattr(e,'code',None)}). Hard reset. {e}")
                await safe_disconnect(discord.utils.get(bot.voice_clients, guild=guild), reason="4006/closed reset")
                last_exc = e
            except asyncio.TimeoutError as e:
                logger.warning("⏳ Voice connect timed out. Reset and retry.")
                await safe_disconnect(discord.utils.get(bot.voice_clients, guild=guild), reason="timeout reset")
                last_exc = e
            except Exception as e:
                logger.error(f"Unexpected voice connect error: {type(e).__name__}: {e}")
                await safe_disconnect(discord.utils.get(bot.voice_clients, guild=guild), reason="unexpected reset")
                last_exc = e

            backoff = min(8, 1.5 ** attempt) + random.uniform(0.2, 0.8)
            await asyncio.sleep(backoff)

        raise RuntimeError(f"Failed to connect to {channel.name} after {MAX_ATTEMPTS} attempts") from last_exc

async def schedule_linger(guild: discord.Guild):
    """(Re)start a linger timer for this guild; disconnect when it fires if not playing."""
    # cancel existing
    t = _linger_tasks.get(guild.id)
    if t and not t.done():
        t.cancel()
    async def _linger():
        try:
            await asyncio.sleep(LINGER_SECONDS)
            vc = discord.utils.get(bot.voice_clients, guild=guild)
            if vc and vc.is_connected() and not vc.is_playing():
                await safe_disconnect(vc, reason=f"linger {LINGER_SECONDS}s elapsed")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"Linger task error: {e}")
    _linger_tasks[guild.id] = asyncio.create_task(_linger())

def _fmt_user(u: discord.abc.User) -> str:
    return f"{u} ({u.id})"

# ---------- Events ----------
@bot.event
async def on_ready():
    logger.info(f"✅ Logged in as {bot.user} (discord.py {discord.__version__})")
    # quick ffmpeg visibility
    from shutil import which
    logger.info(f"FFmpeg: {which('ffmpeg') or 'NOT FOUND'}")
    # ensure audio dir exists
    os.makedirs(AUDIO_DIR, exist_ok=True)

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    # ignore bot users joining/moving
    if member.bot:
        return

    # user just joined a channel (or moved into one)
    joined_channel = after.channel
    if joined_channel and joined_channel != before.channel:
        # small anti-spam window (rapid reconnect spam)
        import time
        now = time.time()
        last = _last_played.get(member.id, 0.0)
        if now - last < REJOIN_SPAM_WINDOW:
            logger.info(f"⏭️ Ignoring rapid rejoin from {_fmt_user(member)}")
            return
        _last_played[member.id] = now

        # locate user audio
        path = find_user_audio(member.id)
        if not path:
            logger.info(f"🎵 No audio for {_fmt_user(member)} in {AUDIO_DIR}/")
            # Optional: still connect and beep? For now, do nothing.
            return

        # connect and play
        try:
            vc = await ensure_vc(joined_channel)
        except Exception as e:
            logger.error(f"Connect failed for {_fmt_user(member)} → {joined_channel}: {e}")
            return

        # stop current audio if any
        if vc.is_playing():
            vc.stop()

        try:
            source = FFmpegPCMAudio(path)
            v = max(0.0, min(1.0, PLAYBACK_VOLUME))
            vc.play(PCMVolumeTransformer(source, volume=v))
            logger.info(f"▶️ Playing {os.path.basename(path)} for {_fmt_user(member)} at volume={v:.2f}")
        except Exception as e:
            logger.error(f"Playback error for {_fmt_user(member)}: {e}")
            # still schedule linger so we disconnect eventually
            await schedule_linger(member.guild)
            return

        # when playback finishes, start/update linger timer
        def after_play(err: Optional[Exception]):
            if err:
                logger.warning(f"Playback finished with error: {err}")
            # schedule linger asynchronously
            asyncio.run_coroutine_threadsafe(schedule_linger(member.guild), bot.loop)

        # discord.py calls "after" in a different thread
        vc.source.after = after_play  # type: ignore[attr-defined]

# ---------- Commands (quick testing) ----------
@bot.command()
async def join(ctx: commands.Context):
    if not ctx.author.voice or not ctx.author.voice.channel:
        return await ctx.reply("Join a voice channel first.")
    try:
        await ensure_vc(ctx.author.voice.channel)
        await ctx.reply(f"Joined **{ctx.author.voice.channel.name}** ✅")
    except Exception as e:
        await ctx.reply(f"Failed to connect: `{type(e).__name__}: {e}`")

@bot.command()
async def leave(ctx: commands.Context):
    vc = discord.utils.get(bot.voice_clients, guild=ctx.guild)
    if not vc:
        return await ctx.reply("Not in voice here.")
    await safe_disconnect(vc, reason="user requested")
    await ctx.reply("Disconnected. 🔌")

@bot.command()
async def sayfile(ctx: commands.Context, *, path: str):
    if not os.path.isfile(path):
        return await ctx.reply("File not found.")
    vc = discord.utils.get(bot.voice_clients, guild=ctx.guild)
    if not vc or not vc.is_connected():
        if not ctx.author.voice or not ctx.author.voice.channel:
            return await ctx.reply("Join a voice channel first.")
        try:
            vc = await ensure_vc(ctx.author.voice.channel)
        except Exception as e:
            return await ctx.reply(f"Failed to connect: `{type(e).__name__}: {e}`")

    if vc.is_playing():
        vc.stop()
    try:
        source = FFmpegPCMAudio(path)
        vc.play(PCMVolumeTransformer(source, volume=max(0.0, min(1.0, PLAYBACK_VOLUME))))
        await ctx.reply(f"Now playing: `{os.path.basename(path)}`")
        await schedule_linger(ctx.guild)
    except Exception as e:
        await ctx.reply(f"Playback error: `{e}`")

# ---------- Run ----------
if __name__ == "__main__":
    if not TOKEN:
        logger.error("Set DISCORD_TOKEN in your environment.")
        raise SystemExit(1)
    bot.run(TOKEN)
