# Audio Join Bot (Render-ready)
Drop files into `audio/` named by Discord user ID (e.g., `201986966324117504.mp3`).

Supported extensions: .mp3, .wav, .ogg

## Env
- DISCORD_TOKEN (required)
- LINGER_SECONDS (default 120)
- PLAYBACK_VOLUME (default 0.30)

## Local
python -m venv .venv
. .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
set DISCORD_TOKEN=your_token   # PowerShell: $env:DISCORD_TOKEN='...'
python voice_audio_bot.py

## Render (Docker Worker)
- Connect repo on Render → New → Worker → Use Docker
- Add env var DISCORD_TOKEN
- Deploy
