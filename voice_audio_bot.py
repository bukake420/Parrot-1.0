import os, sys, asyncio, logging, shutil
import discord
from discord.ext import commands
from discord import FFmpegPCMAudio, PCMVolumeTransformer

if sys.platform.startswith("win"):
    try:
        from asyncio.windows_events import WindowsSelectorEventLoopPolicy
        asyncio.set_event_loop_policy(WindowsSelectorEventLoopPolicy())
    except Exception:
        pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("audio-join-bot")

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
LINGER_SECONDS = int(os.getenv("LINGER_SECONDS", "120"))
PLAYBACK_VOLUME = float(os.getenv("PLAYBACK_VOLUME", "0.30"))
AUDIO_DIR = os.getenv("AUDIO_DIR", "audio")
ALLOWED_EXTS = (".mp3", ".wav", ".ogg")

if not TOKEN:
    log.error("DISCORD_TOKEN is not set. Exiting.")
    raise SystemExit(1)

intents = discord.Intents.default()
intents.voice_states = True
intents.members = True
intents.guilds = True
bot = commands.Bot(command_prefix=".", intents=intents, help_command=None)
initial_users = set()

def which_ffmpeg():
    path = shutil.which("ffmpeg")
    if path:
        log.info(f"ffmpeg found: {path}")
    else:
        log.warning("ffmpeg not found in PATH. Audio playback will fail.")
    return path

def find_user_audio(uid: int):
    for ext in ALLOWED_EXTS:
        p = os.path.join(AUDIO_DIR, f"{uid}{ext}")
        if os.path.isfile(p):
            return p
    return None

async def connect_or_move(channel: discord.VoiceChannel, attempts: int = 2):
    guild = channel.guild
    vc = discord.utils.get(bot.voice_clients, guild=guild)
    if vc and vc.is_connected():
        if vc.channel and vc.channel.id == channel.id:
            return vc
        try:
            await vc.move_to(channel)
            return vc
        except:
            try:
                await vc.disconnect(force=True)
            except:
                pass
    for i in range(1, attempts + 1):
        try:
            log.info(f"Connecting to {channel.name} (attempt {i}/{attempts})")
            vc = await channel.connect(timeout=15.0, reconnect=False)
            return vc
        except Exception as e:
            log.warning(f"Connect error: {e}")
            await asyncio.sleep(2)
    return None

async def play_clip(vc: discord.VoiceClient, file_path: str, display_name: str):
    if vc.is_playing():
        vc.stop()
        await asyncio.sleep(0.2)
    if not which_ffmpeg():
        return
    source = PCMVolumeTransformer(FFmpegPCMAudio(file_path), volume=PLAYBACK_VOLUME)
    done = asyncio.Event()
    def _after(err):
        bot.loop.call_soon_threadsafe(done.set)
    vc.play(source, after=_after)
    log.info(f"Playing {os.path.basename(file_path)} for {display_name}")
    try:
        await asyncio.wait_for(done.wait(), timeout=60)
    except asyncio.TimeoutError:
        vc.stop()
    await asyncio.sleep(LINGER_SECONDS)
    if vc.is_connected() and not vc.is_playing():
        await vc.disconnect(force=True)
        log.info("Disconnected after linger")

@bot.event
async def on_ready():
    which_ffmpeg()
    initial_users.clear()
    for g in bot.guilds:
        for m in g.members:
            if m.voice and m.voice.channel:
                initial_users.add(m.id)
    log.info(f"Logged in as {bot.user}")

@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot or before.channel == after.channel:
        return
    if before.channel is None and after.channel is not None:
        if member.id in initial_users:
            initial_users.discard(member.id)
            return
        file_path = find_user_audio(member.id)
        if not file_path:
            return
        vc = await connect_or_move(after.channel)
        if not vc:
            return
        await play_clip(vc, file_path, member.display_name)

if __name__ == "__main__":
    bot.run(TOKEN)
