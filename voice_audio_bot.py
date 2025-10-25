# voice_audio_bot.py — plays a clip when a user joins any voice channel
# Uses filenames in ./audio/ named by Discord user ID, e.g., 201986966324117504.mp3
import os, asyncio, logging, sys, time, shutil
from typing import Optional, Dict, Set
import discord
from discord import FFmpegPCMAudio, PCMVolumeTransformer

# ---------- Logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
log = logging.getLogger(__name__)

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    log.error("DISCORD_TOKEN env var is required")
    sys.exit(1)

AUDIO_DIR = os.path.join(os.path.dirname(__file__), "audio")
DEFAULT_VOLUME = float(os.getenv("BOT_VOLUME", "0.30"))
CONNECT_TIMEOUT = 15.0
PLAYBACK_TIMEOUT = 45.0
LINGER_SECONDS = 90
JOIN_DEBOUNCE_SEC = 2.0   # avoid double-firing when Discord sends multiple states
FAIL_WINDOW_SEC = 300
MAX_FAILS = 3

intents = discord.Intents.default()
intents.voice_states = True
intents.guilds = True
intents.members = True  # enable in Dev Portal

bot = discord.Client(intents=intents)

# Track initial members so we don't fire on startup presence
initial_seen: Set[int] = set()
# Per-guild connect locks to avoid parallel connects (which can cause 4006)
guild_connect_locks: Dict[int, asyncio.Lock] = {}
# Debounce joins per-user
last_join_ts: Dict[int, float] = {}
# Recent failures per guild
recent_failures: Dict[int, tuple[int, float]] = {}
# Linger disconnect tasks
linger_tasks: Dict[int, asyncio.Task] = {}

def log_ffmpeg():
    ff = shutil.which("ffmpeg")
    log.info(f"ffmpeg found: {ff}")

def load_opus():
    # On Linux with libopus0 installed, 'opus' should load
    if discord.opus.is_loaded():
        return
    for name in ("opus", "libopus.so.0", "libopus"):
        try:
            discord.opus.load_opus(name)
            log.info(f"Opus loaded: {name}")
            return
        except Exception:
            continue
    log.warning("Opus failed to load; ensure libopus0 is installed in the container.")

def audio_path_for_user(user_id: int) -> Optional[str]:
    for ext in (".mp3", ".wav", ".ogg", ".m4a", ".flac"):
        p = os.path.join(AUDIO_DIR, f"{user_id}{ext}")
        if os.path.isfile(p):
            return p
    return None

def should_attempt(guild_id: int) -> bool:
    rec = recent_failures.get(guild_id)
    if not rec:
        return True
    fails, ts = rec
    if time.time() - ts > FAIL_WINDOW_SEC:
        recent_failures.pop(guild_id, None)
        return True
    return fails < MAX_FAILS

def record_failure(gid: int):
    fails, _ = recent_failures.get(gid, (0, 0.0))
    recent_failures[gid] = (fails + 1, time.time())

def record_success(gid: int):
    recent_failures.pop(gid, None)

async def disconnect_later(guild_id: int, delay: int):
    try:
        await asyncio.sleep(delay)
        vc = discord.utils.get(bot.voice_clients, guild_id=guild_id)
        if vc and vc.is_connected() and not vc.is_playing():
            try:
                await vc.disconnect(force=True)
                log.info("Disconnected after linger")
            except Exception as e:
                log.warning(f"Linger disconnect failed: {e}")
    finally:
        linger_tasks.pop(guild_id, None)

async def play_clip(vc: discord.VoiceClient, path: str, who: str):
    gid = vc.guild.id
    # cancel linger
    t = linger_tasks.pop(gid, None)
    if t and not t.done():
        t.cancel()

    if vc.is_playing():
        vc.stop()
        await asyncio.sleep(0.25)

    source = PCMVolumeTransformer(FFmpegPCMAudio(path), volume=DEFAULT_VOLUME)
    done = asyncio.Event()

    def _after(err: Optional[Exception]):
        if err:
            log.error(f"Audio error: {err}")
        try:
            bot.loop.call_soon_threadsafe(done.set)
        except Exception:
            pass

    vc.play(source, after=_after)
    log.info(f"Playing {os.path.basename(path)} for {who}")
    try:
        await asyncio.wait_for(done.wait(), timeout=PLAYBACK_TIMEOUT)
    except asyncio.TimeoutError:
        vc.stop()

    if gid not in linger_tasks:
        linger_tasks[gid] = asyncio.create_task(disconnect_later(gid, LINGER_SECONDS))

async def connect_or_move(channel: discord.VoiceChannel, attempts: int = 2) -> Optional[discord.VoiceClient]:
    gid = channel.guild.id

    if not should_attempt(gid):
        log.warning(f"Too many recent failures for guild {gid}; skipping")
        return None

    lock = guild_connect_locks.setdefault(gid, asyncio.Lock())
    async with lock:
        # move if connected
        vc = discord.utils.get(bot.voice_clients, guild=channel.guild)
        if vc and vc.is_connected():
            if vc.channel and vc.channel.id == channel.id:
                record_success(gid)
                return vc
            try:
                await vc.move_to(channel)
                log.info(f"Moved to {channel.name}")
                record_success(gid)
                return vc
            except Exception as e:
                log.warning(f"Move failed: {e}; reconnecting")

        last_exc: Optional[Exception] = None
        for i in range(1, attempts + 1):
            try:
                log.info(f"Connecting to {channel.name} (attempt {i}/{attempts})")
                vc = await channel.connect(timeout=CONNECT_TIMEOUT, reconnect=False)
                log.info(f"Connected to {channel.name}")
                record_success(gid)
                return vc

            except discord.errors.ConnectionClosed as e:
                last_exc = e
                log.warning(f"Connect error: {e}")
                # 4006 invalid session recovery
                if e.code == 4006:
                    try:
                        ghost = discord.utils.get(bot.voice_clients, guild=channel.guild)
                        if ghost:
                            await ghost.disconnect(force=True)
                    except Exception:
                        pass
                    try:
                        await channel.guild.change_voice_state(channel=None, self_mute=False, self_deaf=False)
                    except Exception:
                        pass
                    await asyncio.sleep(2.5)
                    try:
                        await channel.guild.change_voice_state(channel=channel, self_mute=False, self_deaf=False)
                    except Exception:
                        pass
                    await asyncio.sleep(1.5)
                else:
                    await asyncio.sleep(1.5)

            except asyncio.TimeoutError as e:
                last_exc = e
                log.warning("Voice connect timeout")
                await asyncio.sleep(1.5)

            except discord.ClientException as e:
                last_exc = e
                # try move path
                try:
                    vc = discord.utils.get(bot.voice_clients, guild=channel.guild)
                    if vc:
                        await vc.move_to(channel)
                        log.info(f"Moved to {channel.name} after ClientException")
                        record_success(gid)
                        return vc
                except Exception:
                    await asyncio.sleep(1.0)

            except Exception as e:
                last_exc = e
                log.warning(f"Connect error: {e}")
                await asyncio.sleep(1.5)

        record_failure(gid)
        log.error(f"Failed to connect to {channel.name}")
        if last_exc:
            log.debug("Last exception", exc_info=last_exc)
        return None

@bot.event
async def on_ready():
    log_ffmpeg()
    load_opus()
    # collect initial users in voice to avoid firing immediately
    initial_seen.clear()
    for g in bot.guilds:
        for m in g.members:
            if m.voice and m.voice.channel:
                initial_seen.add(m.id)
    log.info(f"Logged in as {bot.user}")

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if member.bot:
        return
    # ignore moves/leaves
    if before.channel == after.channel:
        return
    # only on joins
    if before.channel is None and after.channel is not None:
        now = time.time()
        # if this was "skipped" at startup and they join later again, we will announce because we only skip once
        if member.id in initial_seen:
            # skip exactly once; remove so next join announces
            initial_seen.discard(member.id)
            return
        # debounce same user rapid events
        ts = last_join_ts.get(member.id, 0.0)
        if now - ts < JOIN_DEBOUNCE_SEC:
            return
        last_join_ts[member.id] = now

        path = audio_path_for_user(member.id)
        if not path:
            return  # no clip configured

        vc = await connect_or_move(after.channel)
        if not vc or not vc.is_connected():
            return
        try:
            await play_clip(vc, path, member.display_name)
        except Exception as e:
            log.error(f"Playback error: {e}")

if __name__ == "__main__":
    bot.run(TOKEN)
