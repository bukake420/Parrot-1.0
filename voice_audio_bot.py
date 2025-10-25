# voice_audio_bot.py — robust voice connect w/ 4006 recovery, per-guild lock, simple ID→file lookup
# Requires: pip install -U "discord.py[voice]"
# Env vars (Render):
#   DISCORD_TOKEN  -> your bot token (REQUIRED)
#   AUDIO_DIR      -> optional, default "./audio"

import os, sys, asyncio, logging, shutil, time
from typing import Optional, Iterable

import discord
from discord.ext import commands
from discord import FFmpegPCMAudio, PCMVolumeTransformer

# ---------- Logging ----------
logger = logging.getLogger("voice-audio-bot")
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
logger.addHandler(handler)
logger.setLevel(logging.INFO)

# ---------- Config ----------
TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
if not TOKEN:
    print("ERROR: Set DISCORD_TOKEN env var.", file=sys.stderr)
    sys.exit(1)

AUDIO_DIR = os.getenv("AUDIO_DIR", "./audio")
os.makedirs(AUDIO_DIR, exist_ok=True)

# Tuneables
LINGER_SECONDS = 90          # disconnect after this long idle
PLAYBACK_TIMEOUT = 45        # stop playback if it stalls
DEFAULT_VOLUME = 0.30        # 30%

# ---------- Intents ----------
intents = discord.Intents.default()
intents.voice_states = True
intents.members = True
intents.guilds = True
# message_content not required for this bot

bot = commands.Bot(command_prefix=".", intents=intents, help_command=None)

# Track users present when bot starts so we don't play for them immediately
initial_users: set[int] = set()
linger_tasks: dict[int, asyncio.Task] = {}

# Per-guild connect/move serialization (prevents overlapping joins → 4006)
_guild_voice_lock: dict[int, asyncio.Lock] = {}
def _lock_for(guild_id: int) -> asyncio.Lock:
    if guild_id not in _guild_voice_lock:
        _guild_voice_lock[guild_id] = asyncio.Lock()
    return _guild_voice_lock[guild_id]

# ---------- FFmpeg / Opus ----------
def log_ffmpeg():
    ff = shutil.which("ffmpeg")
    logger.info(f"ffmpeg found: {ff if ff else 'NOT FOUND'}")

def ensure_opus_loaded():
    if discord.opus.is_loaded():
        logger.info("Opus already loaded")
        return
    # Linux on Render usually has libopus.so.0
    candidates: Iterable[str] = (
        "libopus.so.0",
        "opus",
        "libopus-0.x64.dll",
        "opus.dll",
        "libopus.dll",
    )
    last = None
    for c in candidates:
        try:
            discord.opus.load_opus(c)
            logger.info(f"Opus loaded: {c}")
            return
        except Exception as e:
            last = e
    logger.warning(f"Could not load Opus automatically ({last}). Voice may fail.")

# ---------- Audio lookup ----------
AUDIO_EXTS = (".mp3", ".wav", ".ogg", ".flac", ".m4a")

def find_user_audio(user_id: int) -> Optional[str]:
    base = os.path.join(AUDIO_DIR, str(user_id))
    for ext in AUDIO_EXTS:
        path = base + ext
        if os.path.isfile(path):
            return path
    return None

# ---------- Connect / Move (aggressive 4006 recovery) ----------
async def connect_or_move(channel: discord.VoiceChannel, attempts: int = 2) -> Optional[discord.VoiceClient]:
    """Serialized per-guild connect with proactive invalid-session clearing."""
    guild = channel.guild
    gid = guild.id

    async with _lock_for(gid):
        existing = discord.utils.get(bot.voice_clients, guild=guild)
        if existing and existing.is_connected():
            if existing.channel and existing.channel.id == channel.id:
                return existing
            try:
                await existing.move_to(channel)
                logger.info(f"🔄 Moved to {channel.name}")
                return existing
            except Exception as e:
                logger.warning(f"Move failed: {e}; resetting")
                try:
                    await existing.disconnect(force=True)
                except Exception:
                    pass
                await asyncio.sleep(0.8)

        # Proactively tell Discord “we’re in no channel” before attempt #1
        try:
            await guild.change_voice_state(channel=None, self_mute=False, self_deaf=False)
        except Exception:
            pass
        await asyncio.sleep(1.2)

        last_exc: Optional[Exception] = None
        for i in range(1, attempts + 1):
            try:
                logger.info(f"Connecting to {channel.name} (attempt {i}/{attempts})")

                # Nudge gateway to place us in the channel first
                try:
                    await guild.change_voice_state(channel=channel, self_mute=False, self_deaf=False)
                    await asyncio.sleep(0.8)
                except Exception:
                    pass

                vc = await channel.connect(timeout=18.0, reconnect=False)
                logger.info(f"✅ Connected to {channel.name}")
                return vc

            except discord.errors.ConnectionClosed as e:
                last_exc = e
                logger.warning(f"Connect error: {e}")
                # 4006 invalid session: hard clear and retry
                if e.code == 4006:
                    try:
                        ghost = discord.utils.get(bot.voice_clients, guild=guild)
                        if ghost:
                            await ghost.disconnect(force=True)
                    except Exception:
                        pass
                    try:
                        await guild.change_voice_state(channel=None, self_mute=False, self_deaf=False)
                    except Exception:
                        pass
                    await asyncio.sleep(2.5)
                else:
                    await asyncio.sleep(1.5)

            except asyncio.TimeoutError as e:
                last_exc = e
                logger.warning("Voice connect timeout")
                try:
                    await guild.change_voice_state(channel=None, self_mute=False, self_deaf=False)
                except Exception:
                    pass
                await asyncio.sleep(1.5)

            except discord.ClientException as e:
                last_exc = e
                # Race: “already connected” → try move, else reset
                try:
                    vc2 = discord.utils.get(bot.voice_clients, guild=guild)
                    if vc2:
                        await vc2.move_to(channel)
                        logger.info(f"Moved to {channel.name} after ClientException")
                        return vc2
                except Exception:
                    pass
                try:
                    vc2 = discord.utils.get(bot.voice_clients, guild=guild)
                    if vc2:
                        await vc2.disconnect(force=True)
                except Exception:
                    pass
                await asyncio.sleep(1.0)

            except Exception as e:
                last_exc = e
                logger.warning(f"Connect error: {e}")
                await asyncio.sleep(1.5)

        logger.error(f"❌ Failed to connect to {channel.name}")
        if last_exc:
            logger.debug("Last exception:", exc_info=last_exc)
        return None

# ---------- Playback ----------
async def disconnect_later(guild_id: int, delay: int):
    try:
        await asyncio.sleep(delay)
        vc = discord.utils.get(bot.voice_clients, guild_id=guild_id)
        if vc and vc.is_connected() and not vc.is_playing():
            try:
                await vc.disconnect(force=True)
                logger.info("🔌 Linger timeout — disconnected")
            except Exception as e:
                logger.warning(f"Linger disconnect failed: {e}")
    finally:
        linger_tasks.pop(guild_id, None)

async def play_clip(vc: discord.VoiceClient, path: str, member_name: str):
    gid = vc.guild.id
    # cancel any scheduled disconnect
    t = linger_tasks.pop(gid, None)
    if t and not t.done():
        t.cancel()

    if vc.is_playing():
        vc.stop()
        await asyncio.sleep(0.3)

    source = PCMVolumeTransformer(FFmpegPCMAudio(path), volume=DEFAULT_VOLUME)
    done = asyncio.Event()

    def _after(err: Optional[Exception]):
        if err:
            logger.error(f"Audio error: {err}")
        try:
            bot.loop.call_soon_threadsafe(done.set)
        except Exception:
            pass

    vc.play(source, after=_after)
    logger.info(f"🔊 Playing {os.path.basename(path)} for {member_name}")

    try:
        await asyncio.wait_for(done.wait(), timeout=PLAYBACK_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning("⏱️ Playback timeout — stopping")
        vc.stop()

    if gid not in linger_tasks:
        linger_tasks[gid] = asyncio.create_task(disconnect_later(gid, LINGER_SECONDS))

# ---------- Events ----------
@bot.event
async def on_ready():
    log_ffmpeg()
    ensure_opus_loaded()

    # Build initial “present in voice” set to avoid playing immediately for current users
    initial_users.clear()
    for g in bot.guilds:
        for m in g.members:
            if m.voice and m.voice.channel:
                initial_users.add(m.id)

    logger.info(f"Logged in as {bot.user}")

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if member.bot:
        return
    # no change
    if before.channel == after.channel:
        return

    # skip users who were already in voice when the bot started
    if before.channel is None and after.channel and member.id in initial_users:
        initial_users.discard(member.id)  # next time we will play
        return

    # join event
    if before.channel is None and after.channel:
        path = find_user_audio(member.id)
        if not path:
            # no file for this user; nothing to do
            return

        vc = await connect_or_move(after.channel)
        if not vc or not vc.is_connected():
            return

        try:
            await play_clip(vc, path, member.display_name)
        except Exception as e:
            logger.error(f"Playback error: {e}")

# ---------- Simple command to test your own sound ----------
@bot.command()
async def test_sound(ctx: commands.Context, user_id: Optional[int] = None):
    if not ctx.author.voice or not ctx.author.voice.channel:
        return await ctx.send("Join a voice channel first.")
    uid = user_id or ctx.author.id
    path = find_user_audio(uid)
    if not path:
        return await ctx.send(f"No audio file found for `{uid}` in {AUDIO_DIR}.")
    vc = await connect_or_move(ctx.author.voice.channel)
    if not vc or not vc.is_connected():
        return await ctx.send("Couldn't connect to voice.")
    try:
        await play_clip(vc, path, f"TestUser_{uid}")
        await ctx.send("Test played.")
    except Exception as e:
        await ctx.send(f"Playback error: {e}")

# ---------- Main ----------
if __name__ == "__main__":
    try:
        bot.run(TOKEN)
    except KeyboardInterrupt:
        logger.info("Bye!")
