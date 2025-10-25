# voice_audio_bot.py — plays per-user clips on voice join (Render-ready)
import os, sys, asyncio, logging, shutil
from typing import Optional, Iterable

if sys.platform.startswith("win"):
    try:
        from asyncio.windows_events import WindowsSelectorEventLoopPolicy
        asyncio.set_event_loop_policy(WindowsSelectorEventLoopPolicy())
    except Exception:
        pass

import discord
from discord.ext import commands
from discord import FFmpegPCMAudio, PCMVolumeTransformer

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("audio-join-bot")

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
LINGER_SECONDS = int(os.getenv("LINGER_SECONDS", "120"))
PLAYBACK_VOLUME = float(os.getenv("PLAYBACK_VOLUME", "0.30"))
AUDIO_DIR = os.getenv("AUDIO_DIR", "audio")
ALLOWED_EXTS: Iterable[str] = (".mp3", ".wav", ".ogg")

if not TOKEN:
    log.error("DISCORD_TOKEN is not set. Exiting.")
    raise SystemExit(1)

intents = discord.Intents.default()
intents.voice_states = True
intents.members = True
intents.guilds = True
bot = commands.Bot(command_prefix=".", intents=intents, help_command=None)

initial_users: set[int] = set()

def which_ffmpeg() -> Optional[str]:
    path = shutil.which("ffmpeg")
    if path:
        log.info(f"ffmpeg: {path}")
    else:
        log.warning("ffmpeg not found in PATH. Audio playback will fail.")
    return path

def find_user_audio(uid: int) -> Optional[str]:
    for ext in ALLOWED_EXTS:
        p = os.path.join(AUDIO_DIR, f"{uid}{ext}")
        if os.path.isfile(p):
            return p
    return None

async def connect_or_move(channel: discord.VoiceChannel, attempts: int = 2) -> Optional[discord.VoiceClient]:
    guild = channel.guild
    vc = discord.utils.get(bot.voice_clients, guild=guild)
    if vc and vc.is_connected():
        if vc.channel and vc.channel.id == channel.id:
            return vc
        try:
            await vc.move_to(channel)
            log.info(f"Moved to {channel.name}")
            return vc
        except Exception as e:
            log.warning(f"Move failed, reconnecting: {e}")
            try:
                await vc.disconnect(force=True)
            except Exception:
                pass
    last_exc = None
    for i in range(1, attempts + 1):
        try:
            log.info(f"Connecting to {channel.name} (attempt {i}/{attempts})")
            vc = await channel.connect(timeout=15.0, reconnect=False)
            log.info(f"Connected to {channel.name}")
            return vc
        except discord.errors.ConnectionClosed as e:
            last_exc = e
            log.error(f"WS closed ({e.code}): {e}")
            if e.code == 4006:
                try:
                    await guild.change_voice_state(channel=None, self_mute=False, self_deaf=False)
                except Exception:
                    pass
                await asyncio.sleep(2.0)
            await asyncio.sleep(1.5)
        except asyncio.TimeoutError as e:
            last_exc = e
            log.error("Voice connect timeout")
            await asyncio.sleep(1.5)
        except Exception as e:
            last_exc = e
            log.error(f"Connect error: {e}")
            await asyncio.sleep(1.5)
    log.error(f"Failed to connect to {channel.name}")
    if last_exc:
        log.debug("Last exception", exc_info=last_exc)
    return None

async def play_clip(vc: discord.VoiceClient, file_path: str, display_name: str):
    if vc.is_playing():
        vc.stop()
        await asyncio.sleep(0.2)
    if not which_ffmpeg():
        log.error("Cannot play without ffmpeg; skipping.")
        return
    source = PCMVolumeTransformer(FFmpegPCMAudio(file_path), volume=PLAYBACK_VOLUME)
    done = asyncio.Event()
    def _after(err: Optional[Exception]):
        if err:
            log.error(f"Playback error: {err}")
        try:
            bot.loop.call_soon_threadsafe(done.set)
        except Exception:
            pass
    vc.play(source, after=_after)
    log.info(f"Playing {os.path.basename(file_path)} for {display_name}")
    try:
        await asyncio.wait_for(done.wait(), timeout=60)
    except asyncio.TimeoutError:
        log.warning("Playback timed out; stopping")
        vc.stop()
    try:
        await asyncio.sleep(LINGER_SECONDS)
        if vc.is_connected() and not vc.is_playing():
            await vc.disconnect(force=True)
            log.info("Disconnected after linger")
    except Exception as e:
        log.warning(f"Linger disconnect failed: {e}")

@bot.event
async def on_ready():
    which_ffmpeg()
    initial_users.clear()
    for g in bot.guilds:
        for m in g.members:
            if m.voice and m.voice.channel:
                initial_users.add(m.id)
    log.info(f"Logged in as {bot.user} (audio bot)")

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if member.bot:
        return
    if before.channel == after.channel:
        return
    if before.channel is None and after.channel is not None:
        if member.id in initial_users:
            initial_users.discard(member.id)
            return
        file_path = find_user_audio(member.id)
        if not file_path:
            return
        vc = await connect_or_move(after.channel, attempts=2)
        if not vc or not vc.is_connected():
            return
        try:
            await play_clip(vc, file_path, member.display_name)
        except Exception as e:
            log.error(f"Playback exception: {e}")

@bot.command()
async def ping(ctx: commands.Context):
    await ctx.send("pong")

if __name__ == "__main__":
    bot.run(TOKEN)
