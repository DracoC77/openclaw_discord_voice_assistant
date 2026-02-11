# Discord Voice Assistant for OpenClaw

A Discord bot that brings voice conversation capabilities to [OpenClaw](https://openclaw.ai). It joins your Discord voice channels, listens for speech, communicates with your OpenClaw AI agent, and speaks responses back — enabling hands-free AI conversations.

The bot's display name is configurable via `BOT_NAME` (defaults to "Clippy").

## Features

- **Auto-join voice channels** — automatically joins when authorized users enter a voice channel
- **Speech-to-text** — real-time transcription using [Faster Whisper](https://github.com/SYSTRAN/faster-whisper) (runs locally, no API costs)
- **Text-to-speech** — natural voice output via [ElevenLabs](https://elevenlabs.io) or local [Piper TTS](https://github.com/rhasspy/piper) / espeak-ng fallback
- **Wake word detection** — configurable hotword via [openWakeWord](https://github.com/dscripka/openWakeWord) for multi-user channels
- **Speaker identification** — identifies who is speaking using voice embeddings ([Resemblyzer](https://github.com/resemble-ai/Resemblyzer))
- **Authorization system** — restrict interactions to specific Discord users, with wake-word gating for others
- **Inactivity timeout** — automatically leaves voice channels after configurable idle period
- **Slash commands** — `/join`, `/leave`, `/rejoin`, `/enroll`, `/status`, `/timeout`, and more
- **OpenClaw integration** — creates sessions with your OpenClaw agent for each voice conversation
- **Docker sidecar** — runs alongside your existing OpenClaw container on the same Docker network
- **Unraid ready** — ships with Dockerfile, docker-compose, Unraid XML template, and install script

## How It Works

This is a **standalone Python application** that runs as a Docker sidecar alongside your existing OpenClaw container. It does NOT modify or run inside the OpenClaw container — it communicates over HTTP on the same Docker network.

```
[Discord Voice Channel]
       │
  [Voice Assistant Container]  ── Python, Pycord, FFmpeg
       │  receives per-user audio streams
       │  wake word detection (openWakeWord)
       │  speech-to-text (Faster Whisper)
       │
       │  HTTP (/v1/chat/completions)
       v
  [OpenClaw Container]  ── Node.js, your AI agent
       │
       v
  [Voice Assistant Container]
       │  text-to-speech (ElevenLabs/Piper)
       │  plays audio in voice channel
       v
  [Discord Voice Channel]
```

## Quick Start

### Prerequisites

- Docker and docker-compose (recommended) OR Python 3.10+ with FFmpeg
- A Discord Bot Token ([create one here](https://discord.com/developers/applications))
- A running OpenClaw instance with the [Gateway HTTP API enabled](#openclaw-side-setup)

### Option 1: Docker alongside OpenClaw (recommended)

A pre-built image is published to GHCR on every push to `main`.

```bash
# Clone (for config files and docker-compose.yml)
git clone https://github.com/DracoC77/openclaw_discord_voice_assistant.git
cd openclaw_discord_voice_assistant

# Configure
cp .env.example .env
# Edit .env: set DISCORD_BOT_TOKEN, OPENCLAW_URL, and OPENCLAW_API_KEY

# Run (pulls image from ghcr.io automatically)
docker compose up -d
```

### Option 2: Automated install script

```bash
git clone https://github.com/DracoC77/openclaw_discord_voice_assistant.git
cd openclaw_discord_voice_assistant
bash scripts/install.sh
```

The install script auto-detects your OpenClaw container, configures networking, and starts the bot.

### Option 3: Standalone Python

```bash
pip install -e .
cp .env.example .env
# Edit .env
python -m discord_voice_assistant.main
```

### Create Discord Bot

1. Go to [Discord Developer Portal](https://discord.com/developers/applications)
2. Create a **New Application**
3. Go to **Bot** → click **Reset Token** → copy the token to `DISCORD_BOT_TOKEN`
4. Enable these **Privileged Gateway Intents**:
   - Server Members Intent
   - Message Content Intent
5. Go to **OAuth2** → **URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Bot Permissions: `Connect`, `Speak`, `Use Voice Activity`, `Send Messages`, `Use Slash Commands`
6. Use the generated URL to invite the bot to your server

## Slash Commands

| Command | Description |
|---------|-------------|
| `/ping` | Check bot latency |
| `/status` | Show bot status and configuration |
| `/help` | Show all available commands |
| `/join` | Summon bot to your voice channel |
| `/leave` | Make bot leave the voice channel |
| `/rejoin` | Rejoin after inactivity disconnect |
| `/enroll` | Record a voice sample for speaker identification |
| `/voice-status` | Show details about the current voice session |
| `/timeout <seconds>` | Set inactivity timeout (0 to disable) |
| `/authorize @user` | Add user to authorized list (owner only) |
| `/deauthorize @user` | Remove user from authorized list (owner only) |

## Voice Behavior

### Auto-Join
When `AUTO_JOIN_ENABLED=true`, the bot automatically joins a voice channel when an authorized user connects. It follows the authorized user if they switch channels.

### Wake Word
In multi-user channels (more than just you and the bot), the wake word must be spoken before a command. This prevents the bot from responding to conversations not directed at it. Set `BOT_NAME` to configure what name appears in help text.

For unauthorized users, the wake word is always required (configurable via `REQUIRE_WAKE_WORD_FOR_UNAUTHORIZED`).

#### Custom Wake Word
To train a custom wake word model:
1. Use [openWakeWord's training notebook](https://github.com/dscripka/openWakeWord#training-new-models) on Google Colab
2. Train with your desired wake word
3. Place the `.tflite` model file in the `models/` directory
4. Set `WAKE_WORD_MODEL_PATH=models/your_model.tflite`

### Inactivity Timeout
The bot leaves the voice channel after `INACTIVITY_TIMEOUT` seconds of no speech activity. Default is 300 seconds (5 minutes). Set to `0` to disable.

When all human users leave the channel, the bot leaves immediately. When only unauthorized users remain, it starts a 30-second leave timer.

### Speaker Identification
Use `/enroll` to record a 10-second voice sample. The bot uses this to verify speaker identity via voice biometric embeddings, adding an extra verification layer on top of Discord's per-user audio streams.

## OpenClaw-Side Setup

The voice assistant communicates with OpenClaw through its Gateway's OpenAI-compatible HTTP API (`/v1/chat/completions`). This endpoint is **disabled by default** and must be enabled.

### 1. Enable the Chat Completions Endpoint

Add the following to your OpenClaw configuration file (`~/.openclaw/openclaw.json`, or the config volume in Docker):

```json5
{
  gateway: {
    bind: "lan",
    auth: {
      token: "your-secret-token"
    },
    http: {
      endpoints: {
        chatCompletions: {
          enabled: true
        }
      }
    }
  }
}
```

- **`gateway.bind: "lan"`** — exposes the gateway beyond localhost (required when the voice assistant runs in a separate Docker container)
- **`gateway.auth.token`** — secures the gateway when `bind` is not `"loopback"`
- **`gateway.http.endpoints.chatCompletions.enabled: true`** — enables the `/v1/chat/completions` endpoint (this is what causes `405 Method Not Allowed` if missing)

### 2. Configure the Voice Assistant

Set these in the voice assistant's `.env` or Docker environment:

```
OPENCLAW_URL=http://<openclaw-host>:18789
OPENCLAW_API_KEY=your-secret-token
OPENCLAW_AGENT_ID=main
```

- **`OPENCLAW_URL`** — use your host's LAN IP or Docker container name (not `localhost`, which refers to the voice assistant container itself)
- **`OPENCLAW_API_KEY`** — the same token you set in `gateway.auth.token`
- **`OPENCLAW_AGENT_ID`** — the OpenClaw agent to route requests to (e.g. `main`); set to `default` to omit the header

### 3. Verify

After restarting OpenClaw, test from the machine running the voice assistant:
```bash
curl -sS http://<openclaw-host>:18789/v1/chat/completions \
     -H "Authorization: Bearer your-secret-token" \
     -H "Content-Type: application/json" \
     -H "x-openclaw-agent-id: main" \
     -d '{"model":"openclaw","messages":[{"role":"user","content":"hello"}]}'
```

You should get a JSON response with `choices[0].message.content`. If you get:
- **405 Method Not Allowed** — `chatCompletions.enabled` is not set to `true`
- **401 Unauthorized** — auth token mismatch between voice assistant and OpenClaw
- **Connection refused** — wrong IP/port, or `gateway.bind` is still `"loopback"`

See [`AGENT_INSTALL.md`](AGENT_INSTALL.md#openclaw-side-configuration) for detailed steps and the [OpenClaw docs](https://docs.openclaw.ai/gateway/openai-http-api) for the full HTTP API reference.

## Deploying with OpenClaw

### Docker Compose Sidecar (recommended)

This runs the voice assistant as a separate container on the same Docker network as OpenClaw:

```bash
cp .env.example .env
# Set DISCORD_BOT_TOKEN and OPENCLAW_URL=http://<openclaw-container-name>:18789
docker compose up -d
```

If OpenClaw is on a custom Docker network, add this to `docker-compose.yml`:
```yaml
networks:
  default:
    name: your_openclaw_network
    external: true
```

### Combined docker-compose.yml

To manage both OpenClaw and the voice assistant together, see the template in [`AGENT_INSTALL.md`](AGENT_INSTALL.md#docker-compose-with-openclaw).

### Why Not Inside the OpenClaw Container?

OpenClaw runs on Node.js 22 (Debian Bookworm). It does **not** include Python, FFmpeg, or the audio libraries this bot needs. Installing them inside the OpenClaw container would:
- Add ~2GB+ of dependencies (Python, Whisper models, audio libs)
- Be fragile across OpenClaw updates
- Require a process manager to run both Node.js and Python

The sidecar approach keeps both containers clean and independently updatable.

## Unraid Deployment

### Option A: Install Script (easiest)

```bash
ssh root@your-unraid-ip
mkdir -p /mnt/user/appdata/discord-voice-assistant
cd /mnt/user/appdata/discord-voice-assistant
git clone https://github.com/DracoC77/openclaw_discord_voice_assistant.git .
bash scripts/install.sh
```

### Option B: Docker Compose

```bash
cd /mnt/user/appdata/discord-voice-assistant
git clone https://github.com/DracoC77/openclaw_discord_voice_assistant.git .
cp .env.example .env
nano .env  # Configure your settings
docker compose up -d
docker compose logs -f discord-voice-assistant
```

### Option C: Unraid Template

1. Copy `unraid-template.xml` to your Unraid templates directory
2. In the Unraid web UI: **Docker** → **Add Container** → **Template** → select "discord-voice-assistant"
3. Fill in the configuration fields and click **Apply**

### Connecting to OpenClaw on Unraid

```bash
# Find the OpenClaw container name
docker ps | grep -i openclaw

# Set OPENCLAW_URL to the container name (internal Docker DNS)
OPENCLAW_URL=http://openclaw-container-name:18789

# Or use the Unraid host IP
OPENCLAW_URL=http://192.168.x.x:18789
```

### Troubleshooting

See [`AGENT_INSTALL.md`](AGENT_INSTALL.md#troubleshooting) for detailed troubleshooting steps, or check:

```bash
docker logs discord-voice-assistant          # Container logs
docker exec discord-voice-assistant ffmpeg -version  # Verify FFmpeg
docker stats discord-voice-assistant         # Memory usage
```

**Memory by STT model:** tiny ~150MB, base ~300MB, small ~600MB, medium ~1.5GB, large-v3 ~3GB

## Installation Guides

- **[`HUMAN_INSTALL.md`](HUMAN_INSTALL.md)** — Step-by-step guide for manual Unraid deployment (start here)
- **[`AGENT_INSTALL.md`](AGENT_INSTALL.md)** — Guide designed for an OpenClaw agent to follow autonomously

Quick prompt for your agent:

> I need you to deploy the Discord Voice Assistant on my Unraid server.
> Read the AGENT_INSTALL.md file at https://github.com/DracoC77/openclaw_discord_voice_assistant
> for step-by-step instructions. My Discord bot token is [token] and OpenClaw
> is running at http://[container-name]:18789.

## Configuration Reference

See [`.env.example`](.env.example) for all available options. Key settings:

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DISCORD_BOT_TOKEN` | Yes | — | Discord bot token |
| `OPENCLAW_URL` | Yes | `http://localhost:18789` | OpenClaw Gateway URL (use host LAN IP in Docker) |
| `OPENCLAW_API_KEY` | Yes* | — | Gateway auth token (required when `bind` != `loopback`) |
| `OPENCLAW_AGENT_ID` | No | `default` | OpenClaw agent to route to (e.g. `main`) |
| `BOT_NAME` | No | `Clippy` | Display name in bot responses |
| `AUTHORIZED_USER_IDS` | No | — | Comma-separated Discord user IDs |
| `STT_MODEL_SIZE` | No | `base` | tiny/base/small/medium/large-v3 |
| `TTS_PROVIDER` | No | `local` | `local` or `elevenlabs` |
| `WAKE_WORD_ENABLED` | No | `true` | Enable wake word detection |
| `AUTO_JOIN_ENABLED` | No | `true` | Auto-join voice channels |
| `INACTIVITY_TIMEOUT` | No | `300` | Seconds before auto-leave |

## Project Structure

```
discord_voice_assistant/
├── main.py              # Entry point
├── bot.py               # Discord bot core
├── config.py            # Configuration management
├── voice_manager.py     # Voice channel lifecycle
├── voice_session.py     # Per-channel voice session
├── audio/
│   ├── sink.py          # Streaming audio receiver with VAD
│   ├── stt.py           # Speech-to-text (Faster Whisper)
│   ├── tts.py           # Text-to-speech (ElevenLabs/Piper)
│   ├── wake_word.py     # Wake word detection (openWakeWord)
│   └── voice_id.py      # Speaker identification (Resemblyzer)
├── commands/
│   ├── general.py       # General slash commands
│   └── voice.py         # Voice-specific slash commands
└── integrations/
    └── openclaw.py      # OpenClaw API client
scripts/
└── install.sh           # Automated install script
AGENT_INSTALL.md         # Guide for OpenClaw agent deployment
HUMAN_INSTALL.md         # Step-by-step human install guide for Unraid
```

## License

MIT
