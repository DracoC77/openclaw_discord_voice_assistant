# Human Install Guide — Unraid Deployment

Step-by-step instructions for deploying the Discord Voice Assistant alongside
your existing OpenClaw Docker container on Unraid. No coding required.

## What You'll Need Before Starting

1. **SSH access** to your Unraid server (or the Unraid terminal via the web UI)
2. **A Discord Bot Token** — if you don't have one, see [Create a Discord Bot](#create-a-discord-bot) below
3. **Your OpenClaw container name** — the name shown in the Unraid Docker tab (e.g., `openclaw`, `OpenClaw`, etc.)
4. About **10 minutes**

## Step 1: Create a Discord Bot

If you already have a bot token, skip to [Step 2](#step-2-configure-openclaw).

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications)
2. Click **New Application** → give it a name (e.g., "Clippy Voice") → click **Create**
3. Go to **Bot** in the left sidebar
4. Click **Reset Token** → **Yes, do it!** → copy the token somewhere safe
   - **This is your `DISCORD_BOT_TOKEN`** — you'll need it later
5. Scroll down and enable these **Privileged Gateway Intents**:
   - Server Members Intent ✅
   - Message Content Intent ✅
6. Go to **OAuth2** → **URL Generator** in the left sidebar
7. Under **Scopes**, check:
   - `bot`
   - `applications.commands`
8. Under **Bot Permissions**, check:
   - `Connect`
   - `Speak`
   - `Use Voice Activity`
   - `Send Messages`
   - `Use Slash Commands`
9. Copy the **Generated URL** at the bottom and open it in your browser
10. Select your Discord server and click **Authorize**

## Step 2: Configure OpenClaw

The voice assistant talks to OpenClaw through its Gateway HTTP API. This is
**disabled by default**, so you need to enable it.

### Find Your OpenClaw Config

SSH into your Unraid server (or use the terminal in the Unraid web UI):

```bash
# Find the OpenClaw container name
docker ps --format "table {{.Names}}\t{{.Image}}" | grep -i claw
```

Note the container name (e.g., `openclaw`).

### Enable the Gateway HTTP API

You have two options:

**Option A: Environment Variables (easiest on Unraid)**

1. In the Unraid web UI, go to **Docker** tab
2. Click on your OpenClaw container → **Edit**
3. Click **Add another Path, Port, Variable, Label, or Device** → choose **Variable**
4. Add these two variables:

   | Name | Key | Value |
   |------|-----|-------|
   | Gateway Bind | `OPENCLAW_GATEWAY_BIND` | `lan` |
   | Gateway Token | `OPENCLAW_GATEWAY_TOKEN` | *(pick a secret password)* |

5. Click **Apply** — OpenClaw will restart

**Option B: Edit openclaw.json**

```bash
# Find where OpenClaw stores its config
docker exec <openclaw-container> cat /home/node/.openclaw/openclaw.json
```

Edit the file (you may need to `docker exec -it <container> sh` into the
container, or edit it via the mapped Unraid appdata path):

```json
{
  "gateway": {
    "bind": "lan",
    "auth": {
      "token": "pick-a-secret-password-here"
    }
  }
}
```

Then restart the container: `docker restart <openclaw-container>`

### Verify It Worked

```bash
# Replace with YOUR container name and token
curl -s -H "Authorization: Bearer pick-a-secret-password-here" \
     -H "Content-Type: application/json" \
     -d '{"model":"openclaw","messages":[{"role":"user","content":"hello"}]}' \
     http://<openclaw-container-name>:18789/v1/chat/completions
```

You should see a JSON response. If you get "connection refused", the gateway
isn't enabled yet. If you get a 401, your token doesn't match.

### Create a Voice Agent (Recommended)

For the best voice experience, create a **separate OpenClaw agent** specifically
for voice conversations. This is important because:

- Without it, the bot's responses may be **long and verbose** (great for chat,
  terrible for voice)
- The agent might use **markdown formatting** that TTS reads literally
  (e.g. "asterisk asterisk bold text asterisk asterisk")
- A voice agent lets you tune the personality independently from your chat agent

**How to set it up:**

1. In OpenClaw, create a new agent (e.g. named `voice`)
2. Give it this system prompt (customize to taste):

   > You are a voice assistant responding in a Discord voice channel. Your
   > responses will be converted to speech by a text-to-speech engine and played
   > aloud. Be concise and conversational — match your response length to the
   > complexity of the question. Simple questions should get short answers;
   > complex topics can be longer but stay focused and avoid rambling. Never use
   > markdown formatting, bullet points, numbered lists, code blocks, or emoji
   > — these will be read literally by TTS. Respond in plain, natural,
   > conversational speech.

3. Note the agent ID (e.g. `voice`) — you'll use it in the next step when
   configuring `OPENCLAW_AGENT_ID`

> **If you skip this step**, the bot includes a fallback instruction in each
> message asking for concise plain-text responses. It works but is less reliable
> than a dedicated agent prompt.

## Step 3: Install the Voice Assistant

### Option A: Automated Script (recommended)

```bash
# SSH into Unraid
ssh root@<your-unraid-ip>

# Create the app directory and clone
mkdir -p /mnt/user/appdata/discord-voice-assistant
cd /mnt/user/appdata/discord-voice-assistant
git clone https://github.com/DracoC77/openclaw_discord_voice_assistant.git .

# Run the installer
bash scripts/install.sh
```

The script will:
- Auto-detect your OpenClaw container and its Docker network
- Set `OPENCLAW_URL` automatically
- Prompt you to edit `.env` for your Discord bot token

After the script runs, edit `.env` to fill in:
```bash
nano /mnt/user/appdata/discord-voice-assistant/.env
```
Set these values:
```
DISCORD_BOT_TOKEN=your-discord-bot-token-from-step-1
OPENCLAW_API_KEY=the-same-secret-password-from-step-2
OPENCLAW_AGENT_ID=voice
```
> Set `OPENCLAW_AGENT_ID` to the voice agent you created in Step 2. If you
> skipped that step, leave it as `default`.

Then restart:
```bash
cd /mnt/user/appdata/discord-voice-assistant
docker compose down && docker compose up -d
```

### Option B: Manual Setup

```bash
# SSH into Unraid
ssh root@<your-unraid-ip>

# Clone
mkdir -p /mnt/user/appdata/discord-voice-assistant
cd /mnt/user/appdata/discord-voice-assistant
git clone https://github.com/DracoC77/openclaw_discord_voice_assistant.git .

# Create .env from template
cp .env.example .env
nano .env
```

Fill in these values in `.env`:
```
DISCORD_BOT_TOKEN=your-discord-bot-token
OPENCLAW_URL=http://<openclaw-container-name>:18789
OPENCLAW_API_KEY=the-same-gateway-token-from-step-2
OPENCLAW_AGENT_ID=voice
```

Check if OpenClaw is on a custom Docker network:
```bash
docker inspect <openclaw-container> --format '{{range $net, $conf := .NetworkSettings.Networks}}{{$net}}{{end}}'
```

If it shows something other than `bridge` (e.g., `openclaw_default`), add this
to the end of `docker-compose.yml`:

```yaml
networks:
  default:
    name: openclaw_default    # <-- replace with actual network name
    external: true
```

Start (pulls the pre-built image from GHCR automatically):
```bash
docker compose up -d
```

To build locally instead of pulling, edit `docker-compose.yml` and uncomment the
`build: .` line, then run `docker compose build && docker compose up -d`.

### Option C: Unraid Template (UI-based)

This uses the pre-built Docker image from GitHub Container Registry — no building needed.

1. Copy `unraid-template.xml` from the repo to `/boot/config/plugins/dockerMan/templates-user/`:
   ```bash
   cp /mnt/user/appdata/discord-voice-assistant/unraid-template.xml \
      /boot/config/plugins/dockerMan/templates-user/discord-voice-assistant.xml
   ```
2. In the Unraid web UI: **Docker** → **Add Container** → **Template** dropdown → select **discord-voice-assistant**
3. Fill in the fields:
   - **Discord Bot Token**: from Step 1
   - **OpenClaw URL**: `http://<openclaw-container-name>:18789`
   - **OpenClaw API Key**: the gateway token from Step 2
4. Click **Apply**

> **Note:** The repo must be public on GitHub for the GHCR image to be pullable
> without authentication. If the repo is private, you'd need to configure a
> GHCR access token on Unraid (`docker login ghcr.io`).

## Step 4: Verify It's Running

```bash
cd /mnt/user/appdata/discord-voice-assistant

# Check container status
docker compose ps

# Watch the logs (Ctrl+C to exit)
docker compose logs -f
```

Look for these lines in the logs — they mean everything is working:
```
Logged in as YourBotName#1234 (ID: ...)
Connected to N guild(s)
Voice manager initialized
Synced N slash command(s)
```

## Step 5: Test in Discord

1. Join a voice channel in your Discord server
2. The bot should auto-join (if `AUTO_JOIN_ENABLED=true`)
3. Or type `/join` in any text channel to summon it
4. Speak! The bot will:
   - Transcribe your speech
   - Send it to your OpenClaw agent
   - Speak the response back

## Configuration Cheat Sheet

These go in `/mnt/user/appdata/discord-voice-assistant/.env`:

| Setting | Default | What It Does |
|---------|---------|-------------|
| `DISCORD_BOT_TOKEN` | *(required)* | Your Discord bot token |
| `OPENCLAW_URL` | `http://localhost:18789` | OpenClaw gateway address |
| `OPENCLAW_API_KEY` | *(empty)* | Gateway auth token |
| `OPENCLAW_AGENT_ID` | `default` | Voice agent ID ([see above](#create-a-voice-agent-recommended)) |
| `BOT_NAME` | `Clippy` | Name shown in bot responses |
| `STT_MODEL_SIZE` | `base` | Speech-to-text model: `tiny`, `base`, `small`, `medium`, `large-v3` |
| `TTS_PROVIDER` | `local` | `local` (free, runs on CPU) or `elevenlabs` (paid, better quality) |
| `WAKE_WORD_ENABLED` | `true` | Require wake word in multi-user channels |
| `AUTO_JOIN_ENABLED` | `true` | Auto-join when you enter a voice channel |
| `INACTIVITY_TIMEOUT` | `300` | Seconds of silence before leaving (0 = never) |
| `AUTHORIZED_USER_IDS` | *(empty)* | Comma-separated Discord user IDs (empty = everyone) |

### How to Get a Discord User ID

1. In Discord, go to **Settings** → **Advanced** → enable **Developer Mode**
2. Right-click on a user → **Copy User ID**

## Compute Requirements

All AI models run locally in the container — no cloud API costs for speech
processing (unless you opt for ElevenLabs TTS).

### CPU-Only (no GPU needed)

The default configuration runs entirely on CPU with `int8` quantization:

| Component | RAM Usage | CPU Impact | Notes |
|-----------|-----------|------------|-------|
| **Whisper tiny** | ~150 MB | Low | Fastest, less accurate |
| **Whisper base** (default) | ~300 MB | Moderate | Good accuracy, recommended starting point |
| **Whisper small** | ~600 MB | Higher | Better accuracy |
| **Whisper medium** | ~1.5 GB | High | Diminishing returns for voice commands |
| **openWakeWord** | ~50 MB | Very low | Always running, minimal overhead |
| **Piper TTS** | ~100 MB | Low | Fast local speech synthesis |
| **Resemblyzer** | ~100 MB | Low per-check | Only runs during voice verification |

**Total RAM for default config (base model):** ~800 MB active, 1-2 GB recommended

### Minimum System Requirements

- **CPU:** Any x86_64 processor from the last ~10 years. 2+ cores recommended.
  Transcription of a 5-second voice clip takes ~1-3 seconds on a modern CPU
  with the `base` model.
- **RAM:** 2 GB free minimum (for `base` model). 4 GB free recommended.
- **Disk:** ~2 GB for the Docker image + models.
- **GPU:** Not required. If you have an NVIDIA GPU with CUDA support, set
  `STT_DEVICE=cuda` and `STT_COMPUTE_TYPE=float16` for faster transcription.

### Performance Tips for Older Servers

If your server is older or resource-constrained:

1. **Use the `tiny` model**: Set `STT_MODEL_SIZE=tiny` — uses ~150 MB RAM and
   transcribes faster, though with slightly lower accuracy (still fine for
   clear voice commands).
2. **Disable wake word**: Set `WAKE_WORD_ENABLED=false` if you're the only one
   in voice channels. Saves ~50 MB and some CPU.
3. **Use `local` TTS**: The default `local` TTS (Piper/espeak) is very
   lightweight. Only switch to ElevenLabs if you want premium voice quality.
4. **Set memory limits**: The `docker-compose.yml` limits memory to 4 GB. For
   tight systems, reduce to 2 GB: edit the `limits: memory:` line.

## Troubleshooting

### Bot won't start / container keeps restarting

```bash
docker compose logs --tail=50
```

Common causes:
- Invalid `DISCORD_BOT_TOKEN` — double-check you copied the Bot token (not Client ID)
- Missing `.env` file — make sure you ran `cp .env.example .env`

### Bot is online but doesn't respond to voice

```bash
# Check if FFmpeg is working
docker compose exec discord-voice-assistant ffmpeg -version

# Check if audio libraries are loaded
docker compose exec discord-voice-assistant python -c "import discord; print('opus loaded:', discord.opus.is_loaded())"
```

### Bot can't reach OpenClaw (empty responses)

```bash
# Test from inside the voice assistant container
docker compose exec discord-voice-assistant python -c "
import aiohttp, asyncio
async def test():
    async with aiohttp.ClientSession() as s:
        headers = {'Authorization': 'Bearer YOUR_TOKEN', 'Content-Type': 'application/json'}
        data = {'model': 'openclaw', 'messages': [{'role': 'user', 'content': 'test'}]}
        async with s.post('http://YOUR_OPENCLAW_CONTAINER:18789/v1/chat/completions', json=data, headers=headers) as r:
            print(f'Status: {r.status}')
            print(await r.text())
asyncio.run(test())
"
```

- **Connection refused**: Gateway not enabled — go back to [Step 2](#step-2-configure-openclaw)
- **401 Unauthorized**: Token mismatch — make sure `OPENCLAW_API_KEY` matches `OPENCLAW_GATEWAY_TOKEN`
- **404 Not Found**: Gateway endpoint not available — ensure OpenClaw is updated and gateway is enabled

### Containers are on different Docker networks

```bash
# Check what network each container is on
docker inspect openclaw --format '{{range $net, $conf := .NetworkSettings.Networks}}{{$net}}{{end}}'
docker inspect discord-voice-assistant --format '{{range $net, $conf := .NetworkSettings.Networks}}{{$net}}{{end}}'
```

If they're on different networks, add the network config to `docker-compose.yml`
(see [Manual Setup](#option-b-manual-setup)).

### Updating to a New Version

```bash
cd /mnt/user/appdata/discord-voice-assistant
git pull origin main
docker compose build --no-cache
docker compose up -d
```
