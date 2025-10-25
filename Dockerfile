FROM python:3.12-slim

# Install ffmpeg and libopus so voice works
RUN apt-get update && apt-get install -y --no-install-recommends     ffmpeg libopus0  && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render Worker entrypoint
CMD ["python","voice_audio_bot.py"]
