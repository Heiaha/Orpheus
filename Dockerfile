# syntax=docker/dockerfile:1
FROM python:3.11-slim

# System deps: ffmpeg for streaming, libopus for Discord voice
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg libopus0 libsodium23 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./

RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "main.py"]
