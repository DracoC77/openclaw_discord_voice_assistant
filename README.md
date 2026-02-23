# Discord Voice Assistant for OpenClaw

A Discord bot that brings voice conversation capabilities to [OpenClaw](https://openclaw.ai). It joins your Discord voice channels, listens for speech, communicates with your OpenClaw AI agent, and speaks responses back — enabling hands-free AI conversations.

The bot's display name is configurable via `BOT_NAME` (defaults to "Clippy").

## Features

- **Auto-join voice channels** — automatically joins when authorized users enter a voice channel
- **Speech-to-text** — real-time transcription using [Faster Whisper](https://github.com/SYSTRAN/faster-whisper) (runs locally, no API costs)
- **Text-to-speech** — natural voice output via [ElevenLabs](https://elevenlabs.io) or local [Piper TTS](https://github.com/rhasspy/piper) / espeak-ng fallback
- **Wake word detection** — configurable hotword via [openWakeWord](https://github.com/dscripka/openWakeWord) for multi-user channels
- **Authorization system** — restrict interactions to specific Discord users, with wake-word gating for others
- **Inactivity timeout** — automatically leaves voice channels after configurable idle period
- **Slash commands** — `/join`, `/leave`, `/rejoin`, `/status`, `/timeout`, and more
- **OpenClaw integration** — creates sessions with your OpenClaw agent for each voice conversation
- **Docker sidecar** — runs alongside your existing OpenClaw container on the same Docker network
- **Unraid ready** — ships with Dockerfile, docker-compose, Unraid XML template, and install script

## How It Works

This is a **standalone Python application** that runs as a Docker sidecar alongside your existing OpenClaw container. It does NOT modify or run inside the OpenClaw container — it communicates over HTTP on the same Docker network.

```
[Discord Voice Channel]
       │
  [Voice Bridge Container]  ── Node.js, @discordjs/voice, DAVE E2EE
       │  DAVE encryption/decryption
       │  Opus encode/decode
       │  per-user audio streams via WebSocket
       │
  [Voice Assistant Container]  ── Python, discord.py, FFmpeg
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
       │  sends audio to bridge via WebSocket
       v
  [Voice Bridge Container]
       │  Opus encode, DAVE encrypt
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
| `/voice-status` | Show details about the current voice session |
| `/timeout <seconds>` | Set inactivity timeout (0 to disable) |
| `/authorize @user` | Add user to authorized list (owner only) |
| `/new` | Start a fresh conversation (clears all context) |
| `/compact` | Summarize conversation history to free up context space |
| `/deauthorize @user` | Remove user from authorized list (owner only) |

## Voice Behavior

### Auto-Join
When `AUTO_JOIN_ENABLED=true`, the bot automatically joins a voice channel when an authorized user connects. It follows the authorized user if they switch channels.

### Per-User Audio Streams

Discord provides **separate audio streams for each user** in a voice channel. The bot receives each person's voice as independent audio packets tagged with their Discord user ID. This means:

- Each user's speech is buffered and processed independently
- The bot always knows *who* is speaking without any ambiguity
- Voice activity detection (VAD) runs per-user, so overlapping speech is handled correctly
- Wake word detection and transcription happen on each user's isolated audio stream

You do **not** need wake words just to distinguish speakers — Discord handles that at the protocol level. Wake words are useful for a different reason: preventing the bot from responding to conversations not directed at it (see below).

### Wake Word

When `WAKE_WORD_ENABLED=true`, the bot requires a wake word before processing speech. This is useful in two scenarios:

1. **Multi-user channels** (more than just you and the bot) — prevents the bot from responding to side conversations between other people. Even though Discord provides per-user audio, the bot would otherwise try to respond to *everyone* who speaks.
2. **Unauthorized users** — when `REQUIRE_WAKE_WORD_FOR_UNAUTHORIZED=true` (the default), users not in `AUTHORIZED_USER_IDS` must say the wake word before the bot will respond to them.

For authorized users in a **1-on-1** channel (just you and the bot), no wake word is needed — the bot responds to everything you say.

Without a custom model, openWakeWord ships with built-in wake words like "hey jarvis". To use a custom wake word:

1. Use [openWakeWord's training notebook](https://github.com/dscripka/openWakeWord#training-new-models) on Google Colab
2. Train with your desired wake phrase (collecting ~50+ positive samples works best)
3. Export the `.tflite` model file and place it in the `models/` directory
4. Set `WAKE_WORD_MODEL_PATH=models/your_model.tflite`
5. Adjust `WAKE_WORD_THRESHOLD` (0.0–1.0) — lower values are more sensitive, higher values reduce false positives

### Speech-to-Text

Transcription uses [Faster Whisper](https://github.com/SYSTRAN/faster-whisper), a CTranslate2-based re-implementation of OpenAI's Whisper. It runs entirely locally with no API costs.

| Model | Memory | Speed | Accuracy |
|-------|--------|-------|----------|
| `tiny` | ~150MB | Fastest | Basic |
| `base` | ~300MB | Fast | Good (default) |
| `small` | ~600MB | Moderate | Better |
| `medium` | ~1.5GB | Slow | High |
| `large-v3` | ~3GB | Slowest | Best |

Set `STT_DEVICE=cuda` for GPU acceleration (requires NVIDIA GPU + CUDA). The default `auto` detects CUDA availability and falls back to CPU. Quantization via `STT_COMPUTE_TYPE` (`int8`, `float16`, `float32`) trades accuracy for speed — `int8` (default) is fastest on CPU.

By default (`STT_PRELOAD=true`), the Whisper model is loaded once at startup and kept in memory across voice sessions. This means rejoins are instant — no 10-second model reload. Set `STT_PRELOAD=false` if you prefer to free memory when the bot is idle (the model will be loaded on-demand when someone joins voice).

The bot includes built-in VAD (voice activity detection) that waits for 500ms of silence before sending audio to Whisper, and discards clips shorter than 1 second.

### Inactivity Timeout
The bot leaves the voice channel after `INACTIVITY_TIMEOUT` seconds of no speech activity. Default is 300 seconds (5 minutes). Set to `0` to disable.

When all human users leave the channel, the bot leaves immediately. When only unauthorized users remain, it starts a 30-second leave timer.

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

### 2. Create a Voice Agent (Recommended)

For best results, create a dedicated OpenClaw agent optimized for voice interactions. The agent's system prompt controls how responses are formatted — without a voice-specific prompt, responses will be long, verbose, and may include markdown that TTS reads literally (e.g. "asterisk asterisk").

In your OpenClaw agent configuration, create a new agent (e.g. `voice`) with a system prompt like:

> You are a voice assistant responding in a Discord voice channel. Your responses will be converted to speech by a text-to-speech engine and played aloud. Be concise and conversational — match your response length to the complexity of the question. Simple questions should get short answers; complex topics can be longer but stay focused and avoid rambling. Never use markdown formatting, bullet points, numbered lists, code blocks, or emoji — these will be read literally by TTS. Respond in plain, natural, conversational speech.

You can customize this prompt to fit your use case — the key requirements are:
- **Adaptive length** — short answers for simple questions, longer for complex topics, but always focused
- **No markdown/formatting** so TTS doesn't read "asterisk asterisk bold text"
- **Conversational tone** since the output is spoken, not read on screen

Note the agent ID you create (e.g. `voice`) — you'll use it in the next step.

> **Without a dedicated voice agent**, the bot includes a fallback instruction in each message asking for concise, plain-text responses. This works but is less reliable than a proper agent system prompt.

### 3. Configure the Voice Assistant

Set these in the voice assistant's `.env` or Docker environment:

```
OPENCLAW_URL=http://<openclaw-host>:18789
OPENCLAW_API_KEY=your-secret-token
OPENCLAW_AGENT_ID=voice
```

- **`OPENCLAW_URL`** — use your host's LAN IP or Docker container name (not `localhost`, which refers to the voice assistant container itself)
- **`OPENCLAW_API_KEY`** — the same token you set in `gateway.auth.token`
- **`OPENCLAW_AGENT_ID`** — the OpenClaw agent to route requests to (e.g. `voice`); set to `default` to omit the header and use the default agent

### 4. Verify

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

See [`.env.example`](.env.example) for all available options with comments.

### Core Settings

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DISCORD_BOT_TOKEN` | Yes | — | Discord bot token |
| `BOT_NAME` | No | `Clippy` | Display name in bot responses |
| `OPENCLAW_URL` | Yes | `http://localhost:18789` | OpenClaw Gateway URL (use container name in Docker) |
| `OPENCLAW_API_KEY` | Yes* | — | Gateway auth token (required when `bind` != `loopback`) |
| `OPENCLAW_AGENT_ID` | No | `voice` | OpenClaw agent to route to (`voice` recommended, `default` for fallback) |

### Speech-to-Text (Whisper)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `STT_MODEL_SIZE` | No | `base` | Whisper model: `tiny`, `base`, `small`, `medium`, `large-v2`, `large-v3` |
| `STT_DEVICE` | No | `auto` | Inference device: `cpu`, `cuda`, `auto` |
| `STT_COMPUTE_TYPE` | No | `int8` | Quantization: `int8`, `float16`, `float32` |
| `STT_PRELOAD` | No | `true` | Keep Whisper model in memory between sessions (instant rejoins) |

### Text-to-Speech

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TTS_PROVIDER` | No | `local` | `local` (Piper/espeak) or `elevenlabs` |
| `LOCAL_TTS_MODEL` | No | `en_US-hfc_male-medium` | Piper model name (auto-downloads from HuggingFace) |
| `ELEVENLABS_API_KEY` | If elevenlabs | — | ElevenLabs API key |
| `ELEVENLABS_VOICE_ID` | No | `21m00Tcm4TlvDq8ikWAM` | ElevenLabs voice ID |

### Voice Bridge (DAVE E2EE)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `VOICE_BRIDGE_URL` | No | `ws://voice-bridge:9876` | WebSocket URL for the Node.js voice bridge |

### Voice Channel Behavior

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `AUTO_JOIN_ENABLED` | No | `true` | Auto-join when authorized users enter a voice channel |
| `INACTIVITY_TIMEOUT` | No | `300` | Seconds of inactivity before leaving (0 = disable) |
| `MAX_SESSION_DURATION` | No | `0` | Max session length in seconds (0 = unlimited) |

### Wake Word

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `WAKE_WORD_ENABLED` | No | `false` | Enable wake word detection (disabled by default) |
| `WAKE_WORD_THRESHOLD` | No | `0.5` | Detection sensitivity (0.0–1.0, higher = stricter) |
| `WAKE_WORD_MODEL_PATH` | No | — | Path to custom `.tflite` wake word model |

### Authorization

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `AUTHORIZED_USER_IDS` | No | — | Comma-separated Discord user IDs (empty = allow all) |
| `REQUIRE_WAKE_WORD_FOR_UNAUTHORIZED` | No | `true` | Require wake word from non-authorized users |

### Logging & Debugging

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `LOG_LEVEL` | No | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `DEBUG_VOICE_PIPELINE` | No | `false` | Verbose voice pipeline logging (timing, audio stats, RMS) |

## Project Structure

```
discord_voice_assistant/
├── main.py              # Entry point
├── bot.py               # Discord bot core
├── config.py            # Configuration management
├── voice_bridge.py      # WebSocket client for Node.js voice bridge
├── voice_manager.py     # Voice channel lifecycle
├── voice_session.py     # Per-channel voice session
├── audio/
│   ├── sink.py          # Streaming audio receiver with VAD
│   ├── stt.py           # Speech-to-text (Faster Whisper)
│   ├── tts.py           # Text-to-speech (ElevenLabs/Piper)
│   └── wake_word.py     # Wake word detection (openWakeWord)
├── commands/
│   ├── general.py       # General slash commands
│   └── voice.py         # Voice-specific slash commands
└── integrations/
    └── openclaw.py      # OpenClaw API client
voice_bridge/
├── Dockerfile           # Node.js voice bridge container
├── package.json         # Node.js dependencies
└── src/
    └── index.js         # Voice bridge server (DAVE E2EE)
scripts/
└── install.sh           # Automated install script
AGENT_INSTALL.md         # Guide for OpenClaw agent deployment
HUMAN_INSTALL.md         # Step-by-step human install guide for Unraid
```

## License

MIT
