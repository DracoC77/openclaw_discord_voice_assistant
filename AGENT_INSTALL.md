# Agent Installation Guide

This document is designed to be given to an OpenClaw agent so it can autonomously
install, configure, and troubleshoot the Discord Voice Assistant.

## Overview

The Discord Voice Assistant is a **separate Python application** that runs alongside
OpenClaw as a Docker sidecar container. It communicates with OpenClaw over HTTP
(`/v1/chat/completions`). It is NOT an OpenClaw plugin/channel -- it's a standalone bot.

**Architecture:**
```
[Discord] <--voice--> [Voice Assistant Container (Python)]
                              |
                              | HTTP (same Docker network)
                              v
                       [OpenClaw Container (Node.js)]
```

## Prerequisites

Before starting, you need:
1. SSH access to the Docker host (Unraid server, VPS, etc.)
2. Docker and docker-compose installed
3. A running OpenClaw container
4. A Discord Bot Token (from https://discord.com/developers/applications)

## Step-by-Step Installation

### 1. SSH into the Docker host

```bash
ssh user@<server-ip>
```

### 2. Choose install location

On Unraid:
```bash
INSTALL_DIR="/mnt/user/appdata/discord-voice-assistant"
```

On other systems:
```bash
INSTALL_DIR="$HOME/discord-voice-assistant"
```

### 3. Clone the repository

```bash
git clone https://github.com/DracoC77/openclaw_discord_voice_assistant.git "$INSTALL_DIR"
cd "$INSTALL_DIR"
```

### 4. Find the OpenClaw container

```bash
# List running containers to find OpenClaw
docker ps --format "table {{.Names}}\t{{.Image}}\t{{.Ports}}"

# Common names: openclaw, openclaw-gateway, etc.
# Note the container name and port (default gateway port is 18789)
```

### 5. Check what Docker network OpenClaw uses

```bash
# Replace OPENCLAW_CONTAINER with the actual container name
docker inspect <OPENCLAW_CONTAINER> --format '{{range $net, $conf := .NetworkSettings.Networks}}{{$net}} {{end}}'
```

If OpenClaw is on a custom network (not "bridge"), add this to docker-compose.yml:
```yaml
networks:
  default:
    name: <network-name>
    external: true
```

### 6. Create the .env file

```bash
cp .env.example .env
```

Edit `.env` with these **required** settings:
```
DISCORD_BOT_TOKEN=<the-discord-bot-token>
OPENCLAW_URL=http://<openclaw-container-name>:18789
OPENCLAW_API_KEY=<your-gateway-auth-token>
```

Optional but recommended:
```
BOT_NAME=Clippy
AUTHORIZED_USER_IDS=<comma-separated-discord-user-ids>
STT_MODEL_SIZE=base
TTS_PROVIDER=local
INACTIVITY_TIMEOUT=300
```

### 7. Build and start

```bash
docker compose build
docker compose up -d
```

### 8. Verify it's running

```bash
# Check container status
docker compose ps

# Check logs
docker compose logs -f discord-voice-assistant
```

Look for these success indicators in the logs:
- `Logged in as <BotName>#1234 (ID: ...)`
- `Connected to N guild(s)`
- `Voice manager initialized`
- `Synced N slash command(s)`

### 9. Test in Discord

1. Join a voice channel in a server where the bot is invited
2. If `AUTO_JOIN_ENABLED=true`, the bot should join automatically
3. Or use `/join` to summon it manually
4. Speak -- the bot should transcribe, send to OpenClaw, and respond

## Automated Install Script

For a quicker setup, run the install script which auto-detects OpenClaw:

```bash
cd /mnt/user/appdata/discord-voice-assistant
git clone https://github.com/DracoC77/openclaw_discord_voice_assistant.git .
bash scripts/install.sh
```

The script will:
- Detect the OpenClaw container and its network
- Auto-configure `OPENCLAW_URL`
- Create data directories
- Build and start the container

## OpenClaw-Side Configuration

The voice assistant connects to OpenClaw's Gateway HTTP API, which uses the
OpenAI-compatible `/v1/chat/completions` endpoint. **This API is disabled by
default** and must be explicitly enabled.

### 1. Enable the Gateway HTTP API

The gateway's HTTP interface is controlled by the `gateway.bind` setting.
By default it's set to `"loopback"` (localhost only, no external access).

**Option A: Edit `openclaw.json`** (usually at `~/.openclaw/openclaw.json` or
`/home/node/.openclaw/openclaw.json` inside the container):

```json
{
  "gateway": {
    "bind": "lan"
  }
}
```

**Option B: Set the environment variable** on the OpenClaw container:

```
OPENCLAW_GATEWAY_BIND=lan
```

### 2. Set an Authentication Token

When `bind` is set to anything other than `"loopback"`, authentication is
**required**. Set a secret token that the voice assistant will use to
authenticate.

**Option A: Edit `openclaw.json`:**

```json
{
  "gateway": {
    "bind": "lan",
    "auth": {
      "token": "your-secret-token-here"
    }
  }
}
```

**Option B: Environment variable** on the OpenClaw container:

```
OPENCLAW_GATEWAY_TOKEN=your-secret-token-here
```

Then set the same token in the voice assistant's `.env`:

```
OPENCLAW_API_KEY=your-secret-token-here
```

### 3. Verify the Gateway Port

The default gateway port is **18789**. If you've customized it, update
`OPENCLAW_URL` accordingly. To check, look at the OpenClaw startup logs
for a line like:

```
Gateway listening on 0.0.0.0:18789
```

### 4. Restart OpenClaw

After changing the configuration, restart the OpenClaw container:

```bash
docker restart <openclaw-container-name>
```

### 5. Test Connectivity

From the Docker host, test the gateway is reachable:

```bash
curl -H "Authorization: Bearer your-secret-token-here" \
     http://<openclaw-container-name>:18789/v1/chat/completions \
     -d '{"model":"openclaw","messages":[{"role":"user","content":"hello"}]}' \
     -H "Content-Type: application/json"
```

You should get a JSON response with `choices[0].message.content`.

## Troubleshooting

### Bot won't connect to Discord
```bash
docker compose logs discord-voice-assistant 2>&1 | grep -i "error\|token\|login"
```
**Fix:** Verify `DISCORD_BOT_TOKEN` is correct in `.env`. Make sure you copied the
Bot token (not the Client ID or Client Secret).

### Bot connects but can't reach OpenClaw
```bash
# Test connectivity from inside the container
docker compose exec discord-voice-assistant python -c "
import aiohttp, asyncio
async def test():
    async with aiohttp.ClientSession() as s:
        async with s.get('$OPENCLAW_URL') as r:
            print(f'Status: {r.status}')
asyncio.run(test())
"
```
**Fix:** Ensure both containers are on the same Docker network. Use container name
(not localhost) for `OPENCLAW_URL`.

### No audio / FFmpeg errors
```bash
docker compose exec discord-voice-assistant ffmpeg -version
docker compose exec discord-voice-assistant python -c "import discord; print(discord.opus.is_loaded())"
```
**Fix:** The Docker image should include FFmpeg and libopus. If not, rebuild:
`docker compose build --no-cache`

### Whisper model download fails
```bash
docker compose exec discord-voice-assistant python -c "
from faster_whisper import WhisperModel
model = WhisperModel('base', device='cpu', compute_type='int8')
print('Model loaded successfully')
"
```
**Fix:** Ensure the container has internet access. Or download manually:
```bash
docker compose exec discord-voice-assistant python -c "
from huggingface_hub import snapshot_download
snapshot_download('Systran/faster-whisper-base', local_dir='/app/models/whisper-base')
"
```

### High memory usage
Check which model is loaded:
```bash
docker stats discord-voice-assistant --no-stream
```
Reduce memory by using a smaller STT model in `.env`:
```
STT_MODEL_SIZE=tiny    # ~150MB
STT_MODEL_SIZE=base    # ~300MB  (default)
STT_MODEL_SIZE=small   # ~600MB
```

### Container keeps restarting
```bash
docker compose logs --tail=100 discord-voice-assistant
```
Common causes:
- Invalid `DISCORD_BOT_TOKEN`
- Missing required env vars
- Port conflicts

### Updating to a new version
```bash
cd /mnt/user/appdata/discord-voice-assistant
git pull origin main
docker compose build --no-cache
docker compose up -d
```

## Configuration Quick Reference

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DISCORD_BOT_TOKEN` | Yes | - | Discord bot token |
| `OPENCLAW_URL` | Yes | `http://localhost:18789` | OpenClaw Gateway URL |
| `BOT_NAME` | No | `Clippy` | Display name in responses |
| `OPENCLAW_API_KEY` | Recommended | - | Gateway auth token (matches `OPENCLAW_GATEWAY_TOKEN`) |
| `OPENCLAW_AGENT_ID` | No | `default` | Agent for voice sessions |
| `TTS_PROVIDER` | No | `local` | `local` or `elevenlabs` |
| `ELEVENLABS_API_KEY` | No | - | Required if TTS=elevenlabs |
| `STT_MODEL_SIZE` | No | `base` | tiny/base/small/medium/large-v3 |
| `WAKE_WORD_ENABLED` | No | `true` | Enable wake word |
| `AUTO_JOIN_ENABLED` | No | `true` | Auto-join voice channels |
| `INACTIVITY_TIMEOUT` | No | `300` | Seconds before auto-leave |
| `AUTHORIZED_USER_IDS` | No | - | Comma-separated Discord IDs |
| `LOG_LEVEL` | No | `INFO` | DEBUG/INFO/WARNING/ERROR |

## Docker Compose with OpenClaw

If you want to manage both OpenClaw and the voice assistant in a single
docker-compose.yml, here's a template:

```yaml
version: "3.8"

services:
  openclaw:
    image: alpine/openclaw:latest
    container_name: openclaw
    restart: unless-stopped
    ports:
      - "18789:18789"
    environment:
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
      # Enable the gateway HTTP API for the voice assistant
      - OPENCLAW_GATEWAY_BIND=lan
      - OPENCLAW_GATEWAY_TOKEN=${OPENCLAW_GATEWAY_TOKEN}
    volumes:
      - openclaw-config:/home/node/.openclaw
      - openclaw-workspace:/workspace

  discord-voice-assistant:
    image: ghcr.io/dracoc77/openclaw_discord_voice_assistant:latest
    container_name: discord-voice-assistant
    restart: unless-stopped
    depends_on:
      - openclaw
    environment:
      - DISCORD_BOT_TOKEN=${DISCORD_BOT_TOKEN}
      - OPENCLAW_URL=http://openclaw:18789
      - OPENCLAW_API_KEY=${OPENCLAW_GATEWAY_TOKEN}
      - BOT_NAME=${BOT_NAME:-Clippy}
      - AUTHORIZED_USER_IDS=${AUTHORIZED_USER_IDS:-}
      - STT_MODEL_SIZE=${STT_MODEL_SIZE:-base}
      - TTS_PROVIDER=${TTS_PROVIDER:-local}
      - INACTIVITY_TIMEOUT=${INACTIVITY_TIMEOUT:-300}
    volumes:
      - dva-data:/app/data
      - dva-models:/app/models
      - dva-logs:/app/logs

volumes:
  openclaw-config:
  openclaw-workspace:
  dva-data:
  dva-models:
  dva-logs:
```
