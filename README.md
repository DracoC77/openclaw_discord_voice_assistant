# Clippy - Discord Voice Assistant for OpenClaw

A Discord bot that brings voice conversation capabilities to [OpenClaw](https://openclaw.ai). Clippy joins your Discord voice channels, listens for speech, communicates with your OpenClaw AI agent, and speaks responses back — enabling hands-free AI conversations.

## Features

- **Auto-join voice channels** — automatically joins when authorized users enter a voice channel
- **Speech-to-text** — real-time transcription using [Faster Whisper](https://github.com/SYSTRAN/faster-whisper) (runs locally, no API costs)
- **Text-to-speech** — natural voice output via [ElevenLabs](https://elevenlabs.io) or local [Piper TTS](https://github.com/rhasspy/piper) / espeak-ng fallback
- **Wake word detection** — "Clippy" hotword via [openWakeWord](https://github.com/dscripka/openWakeWord) for multi-user channels
- **Speaker identification** — identifies who is speaking using voice embeddings ([Resemblyzer](https://github.com/resemble-ai/Resemblyzer))
- **Authorization system** — restrict interactions to specific Discord users, with wake-word gating for others
- **Inactivity timeout** — automatically leaves voice channels after configurable idle period
- **Slash commands** — `/join`, `/leave`, `/rejoin`, `/enroll`, `/status`, `/timeout`, and more
- **OpenClaw integration** — creates sessions with your OpenClaw agent for each voice conversation
- **Docker + Unraid ready** — ships with Dockerfile, docker-compose, and Unraid XML template

## Architecture

```
Discord Voice Channel
       │
  [Pycord]  ── receives per-user audio streams
       │
  [StreamingSink]  ── buffers audio, energy-based VAD
       │
  [openWakeWord]  ── wake word detection (multi-user channels)
       │
  [Faster Whisper]  ── speech-to-text transcription
       │
  [OpenClaw API]  ── sends text, receives AI response
       │
  [ElevenLabs / Piper]  ── text-to-speech synthesis
       │
  [Pycord Voice Send]  ── plays audio in voice channel
```

## Quick Start

### Prerequisites

- Python 3.10+
- FFmpeg installed (`apt install ffmpeg` or `brew install ffmpeg`)
- A Discord Bot Token ([create one here](https://discord.com/developers/applications))
- A running OpenClaw instance

### 1. Clone and install

```bash
git clone https://github.com/DracoC77/openclaw_discord_voice_assistant.git
cd openclaw_discord_voice_assistant
pip install -e .
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env with your settings
```

**Required settings:**
- `DISCORD_BOT_TOKEN` — your Discord bot token
- `OPENCLAW_URL` — URL of your OpenClaw instance (e.g., `http://localhost:3000`)

**Recommended settings:**
- `AUTHORIZED_USER_IDS` — comma-separated Discord user IDs to restrict access
- `STT_MODEL_SIZE` — `base` is a good balance of speed and accuracy; `small` for better accuracy
- `TTS_PROVIDER` — `local` for free (uses Piper/espeak), `elevenlabs` for premium voice quality

### 3. Create Discord Bot

1. Go to [Discord Developer Portal](https://discord.com/developers/applications)
2. Create a **New Application** → name it "Clippy" (or whatever you like)
3. Go to **Bot** → click **Reset Token** → copy the token to `DISCORD_BOT_TOKEN`
4. Enable these **Privileged Gateway Intents**:
   - Server Members Intent
   - Message Content Intent
5. Go to **OAuth2** → **URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Bot Permissions: `Connect`, `Speak`, `Use Voice Activity`, `Send Messages`, `Use Slash Commands`
6. Use the generated URL to invite the bot to your server

### 4. Run

```bash
python -m clippy.main
```

## Slash Commands

| Command | Description |
|---------|-------------|
| `/ping` | Check bot latency |
| `/status` | Show bot status and configuration |
| `/help` | Show all available commands |
| `/join` | Summon Clippy to your voice channel |
| `/leave` | Make Clippy leave the voice channel |
| `/rejoin` | Rejoin after inactivity disconnect |
| `/enroll` | Record a voice sample for speaker identification |
| `/voice-status` | Show details about the current voice session |
| `/timeout <seconds>` | Set inactivity timeout (0 to disable) |
| `/authorize @user` | Add user to authorized list (owner only) |
| `/deauthorize @user` | Remove user from authorized list (owner only) |

## Voice Behavior

### Auto-Join
When `AUTO_JOIN_ENABLED=true`, Clippy automatically joins a voice channel when an authorized user connects. It follows the authorized user if they switch channels.

### Wake Word
In multi-user channels (more than just you and Clippy), the wake word "Clippy" must be spoken before a command. This prevents the bot from responding to conversations not directed at it.

For unauthorized users, the wake word is always required (configurable via `REQUIRE_WAKE_WORD_FOR_UNAUTHORIZED`).

#### Custom Wake Word
To train a custom wake word model:
1. Use [openWakeWord's training notebook](https://github.com/dscripka/openWakeWord#training-new-models) on Google Colab
2. Train with your desired wake word (e.g., "hey clippy")
3. Place the `.tflite` model file in the `models/` directory
4. Set `WAKE_WORD_MODEL_PATH=models/your_model.tflite`

### Inactivity Timeout
Clippy leaves the voice channel after `INACTIVITY_TIMEOUT` seconds of no speech activity. Default is 300 seconds (5 minutes). Set to `0` to disable.

When all human users leave the channel, Clippy leaves immediately. When only unauthorized users remain, it starts a 30-second leave timer.

### Speaker Identification
Use `/enroll` to record a 10-second voice sample. Clippy uses this to verify speaker identity. This is useful in multi-user scenarios where Discord's per-user audio streams provide user identification at the Discord level, but voice biometrics add an extra verification layer.

## Docker Deployment

### Using Docker Compose

```bash
cp .env.example .env
# Edit .env with your settings

docker compose up -d
```

### Building the Image

```bash
docker build -t clippy-voice-assistant .
```

### Running Directly

```bash
docker run -d \
  --name clippy-voice \
  --restart unless-stopped \
  --env-file .env \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/models:/app/models \
  -v $(pwd)/logs:/app/logs \
  clippy-voice-assistant
```

## Unraid Deployment

### Option A: Docker Compose (Recommended)

1. Install the **Docker Compose Manager** plugin from Community Applications
2. SSH into your Unraid server
3. Clone this repo or copy the files to `/mnt/user/appdata/clippy-voice/`

```bash
cd /mnt/user/appdata/clippy-voice
git clone https://github.com/DracoC77/openclaw_discord_voice_assistant.git .
cp .env.example .env
nano .env  # Configure your settings
```

4. Build and start:

```bash
docker compose up -d
```

5. View logs:

```bash
docker compose logs -f clippy
```

### Option B: Unraid Template

1. Copy `unraid-template.xml` to your Unraid templates directory
2. In the Unraid web UI, go to **Docker** → **Add Container** → **Template** → select "clippy-voice"
3. Fill in the configuration fields
4. Click **Apply**

### Unraid File Path Notes

Unraid uses `/mnt/user/appdata/` for Docker container persistent data. The template maps:

| Container Path | Unraid Default Path | Purpose |
|---|---|---|
| `/app/data` | `/mnt/user/appdata/clippy-voice/data` | Voice profiles, session data |
| `/app/models` | `/mnt/user/appdata/clippy-voice/models` | AI model cache (Whisper, etc.) |
| `/app/logs` | `/mnt/user/appdata/clippy-voice/logs` | Application logs |

**Important**: The first startup will download AI models (Whisper, openWakeWord). This may take several minutes and requires internet access. Models are cached in the models volume for subsequent runs.

### Connecting to OpenClaw on Unraid

If OpenClaw is also running as a Docker container on the same Unraid server:

1. Find the OpenClaw container name: `docker ps | grep openclaw`
2. Use Docker's internal networking: Set `OPENCLAW_URL=http://openclaw-container-name:3000`
3. Or use the Unraid host IP: `OPENCLAW_URL=http://192.168.x.x:3000`

### Troubleshooting Unraid

**Container won't start:**
```bash
docker logs clippy-voice
```

**Permission issues:**
```bash
# Fix permissions on appdata directories
chmod -R 755 /mnt/user/appdata/clippy-voice/
```

**No audio / FFmpeg errors:**
The Docker image includes FFmpeg. If you see codec errors, ensure the image built correctly:
```bash
docker exec clippy-voice ffmpeg -version
```

**Model download failures:**
If behind a proxy or with limited internet:
```bash
# Download models manually on a machine with internet access
pip install faster-whisper
python -c "from faster_whisper import WhisperModel; WhisperModel('base')"
# Copy the cached model from ~/.cache/huggingface/ to your models volume
```

**High memory usage:**
Reduce the Whisper model size. Memory usage by model:
- `tiny`: ~150MB
- `base`: ~300MB
- `small`: ~600MB
- `medium`: ~1.5GB
- `large-v3`: ~3GB

## Configuration Reference

See [`.env.example`](.env.example) for all available configuration options with descriptions.

## Project Structure

```
clippy/
├── __init__.py
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
```

## Giving This to Your OpenClaw Agent

If you want your OpenClaw agent to deploy and manage this bot, here's a prompt you can use:

> I need you to deploy the Clippy Discord Voice Assistant on my Unraid server.
> The code is at https://github.com/DracoC77/openclaw_discord_voice_assistant
>
> My Unraid server details:
> - SSH access at [your-ip]
> - Docker appdata path: /mnt/user/appdata/
> - OpenClaw is running at http://[openclaw-container]:3000
>
> Steps:
> 1. Clone the repo to /mnt/user/appdata/clippy-voice/
> 2. Create .env from .env.example with my Discord token: [token]
> 3. Set OPENCLAW_URL to point to the OpenClaw container
> 4. Run docker compose up -d
> 5. Check logs to verify it's working
>
> If there are errors, troubleshoot using the README's troubleshooting section.

## License

MIT
