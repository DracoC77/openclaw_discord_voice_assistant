# Proactive Voice Setup Guide

This guide walks you through enabling proactive voice — the ability for your OpenClaw agent to speak to you in Discord without you asking first.

## How It Works

The sidecar runs a webhook server (default port `18790`) that accepts `POST /speak` requests. When OpenClaw's agent decides it has something to say, it calls the `speak` plugin tool, which POSTs to this endpoint. The sidecar then:

- **Live**: Speaks the message in the active voice channel (if you're there)
- **Voicemail**: Sends a playable voice message to your Discord DMs
- **Notify**: DMs you to join a voice channel, then speaks when you arrive
- **Auto** (default): Tries live, falls back to notify, then voicemail

## Prerequisites

- A working Discord voice assistant sidecar (follow `HUMAN_INSTALL.md` first)
- OpenClaw with gateway HTTP API enabled (`gateway.bind: "lan"`)

## Step 1: Configure the Sidecar

Add these to your `.env` file:

```env
# Enable the webhook server (default: true)
WEBHOOK_ENABLED=true

# Port for the webhook server
WEBHOOK_PORT=18790

# IMPORTANT: Set a strong shared secret for authentication.
# Generate one with: openssl rand -hex 32
WEBHOOK_TOKEN=your-secret-token-here

# Default delivery mode (auto recommended)
WEBHOOK_DEFAULT_MODE=auto

# Your Discord user ID for voicemail/notify fallback
# (Find yours: Discord Settings > Advanced > Developer Mode, then right-click your name)
WEBHOOK_NOTIFY_USER_IDS=123456789012345678
```

## Step 2: Expose the Port in Docker

If using the default `docker-compose.yml`, the port is already mapped. If you customized your setup, ensure the webhook port is exposed:

```yaml
ports:
  - "18790:18790"
```

If your OpenClaw and sidecar containers are on the same Docker network, use the container name as the hostname (e.g., `http://discord-voice-assistant:18790`).

## Step 3: Set Up the OpenClaw Speak Plugin

Create the plugin directory and files on your OpenClaw host:

```bash
mkdir -p ~/.openclaw/extensions/speak-plugin
```

Create `~/.openclaw/extensions/speak-plugin/openclaw.plugin.json`:

```json
{
  "id": "speak-plugin",
  "name": "Discord Voice Speak",
  "description": "Send voice messages to the Discord voice assistant",
  "configSchema": {
    "type": "object",
    "properties": {
      "sidecarUrl": {
        "type": "string",
        "default": "http://discord-voice-assistant:18790"
      }
    }
  }
}
```

Create `~/.openclaw/extensions/speak-plugin/index.ts`:

```typescript
import { Type } from "@sinclair/typebox";

export default function (api: any) {
  const sidecarUrl =
    api.config?.sidecarUrl || "http://discord-voice-assistant:18790";
  const token = process.env.VOICE_WEBHOOK_TOKEN || "";

  api.registerTool({
    name: "speak",
    description:
      "Send a voice message to the Discord voice channel. " +
      "Use this when you want to proactively say something aloud to the user. " +
      "Modes: live (voice channel), voicemail (DM audio), notify (DM + wait), auto (try all).",
    parameters: Type.Object({
      text: Type.String({ description: "The text to speak aloud" }),
      mode: Type.Optional(
        Type.Union([
          Type.Literal("auto"),
          Type.Literal("live"),
          Type.Literal("voicemail"),
          Type.Literal("notify"),
        ]),
      ),
      priority: Type.Optional(
        Type.Union([Type.Literal("normal"), Type.Literal("urgent")]),
      ),
    }),
    async execute(
      _id: string,
      params: { text: string; mode?: string; priority?: string },
    ) {
      const headers: Record<string, string> = {
        "Content-Type": "application/json",
      };
      if (token) {
        headers["Authorization"] = `Bearer ${token}`;
      }

      const res = await fetch(`${sidecarUrl}/speak`, {
        method: "POST",
        headers,
        body: JSON.stringify({
          text: params.text,
          mode: params.mode || "auto",
          priority: params.priority || "normal",
        }),
      });

      const result = await res.json();
      return {
        content: [
          {
            type: "text",
            text: `Voice delivery: ${result.delivery || result.error || "unknown"}`,
          },
        ],
      };
    },
  });
}
```

## Step 4: Add Agent Context

Add voice behavior instructions to `~/.openclaw/workspace/AGENTS.md` (create it if it doesn't exist, or append to the existing file):

```markdown
## Voice Behavior

You have a `speak` tool connected to a Discord voice channel sidecar.
Use it to proactively say something aloud when the situation warrants it.

When to proactively speak:
- A scheduled task or cron job completes with notable results
- You detect something urgent (monitoring alert, important email, etc.)
- A reminder or timer you set has fired
- You finished a background task the user asked about

When NOT to proactively speak:
- Routine/low-priority events (suppress with HEARTBEAT_OK)
- The user is already in a conversation with you (just reply normally)
- Trivial status updates that can wait

Voice message guidelines:
- Keep messages to 1-3 sentences
- Use natural, conversational language
- No markdown, code blocks, or emoji (it will be read aloud by TTS)
- For urgent matters, set priority to "urgent"
```

## Step 5: (Optional) Set Up Cron Jobs

For scheduled proactive messages (daily briefings, periodic checks), add cron jobs to OpenClaw. Cron jobs with `delivery.mode: "webhook"` POST directly to the sidecar:

```json5
// In ~/.openclaw/cron/jobs.json (or via the OpenClaw CLI)
{
  "daily-briefing": {
    "schedule": { "kind": "cron", "expr": "0 9 * * *", "tz": "America/New_York" },
    "payload": {
      "kind": "agentTurn",
      "message": "Generate a brief morning status update. Use the speak tool to deliver it."
    },
    "delivery": {
      "mode": "webhook",
      "to": "http://discord-voice-assistant:18790/speak"
    }
  }
}
```

For cron webhook delivery, set the webhook token in OpenClaw's cron config:

```json5
// In ~/.openclaw/openclaw.json
{
  "cron": {
    "enabled": true,
    "webhookToken": "your-secret-token-here"  // Same as WEBHOOK_TOKEN
  }
}
```

## Step 6: Allow the Speak Tool

Ensure the speak tool is allowed for your agent in `openclaw.json`:

```json5
{
  "agents": {
    "list": [{
      "id": "main",
      "tools": { "allow": ["speak"] }
    }]
  }
}
```

## Step 7: Set the Webhook Token as an Environment Variable

The speak plugin reads the token from an environment variable. Add it to your OpenClaw environment:

```bash
export VOICE_WEBHOOK_TOKEN=your-secret-token-here
```

Or add it to your OpenClaw `.env` or systemd service file.

## Testing

### Test the webhook directly:

```bash
# Live mode (you must be in a voice channel)
curl -X POST http://localhost:18790/speak \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-secret-token-here" \
  -d '{"text": "Hello! This is a proactive voice test.", "mode": "live"}'

# Voicemail mode (sends a voice message to your DM)
curl -X POST http://localhost:18790/speak \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-secret-token-here" \
  -d '{"text": "This is a voicemail test.", "mode": "voicemail", "user_id": "YOUR_DISCORD_ID"}'

# Auto mode (tries live, falls back to notify, then voicemail)
curl -X POST http://localhost:18790/speak \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-secret-token-here" \
  -d '{"text": "This is an auto-mode test."}'

# Health check
curl http://localhost:18790/health
```

### Test from OpenClaw:

```bash
openclaw agent \
  --message "Use the speak tool to say hello to me in the voice channel." \
  --agent main
```

## API Reference

### POST /speak

**Headers:**
- `Content-Type: application/json`
- `Authorization: Bearer <WEBHOOK_TOKEN>` (if configured)

**Body:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `text` | string | (required) | The message to speak |
| `mode` | string | `auto` | `auto`, `live`, `voicemail`, or `notify` |
| `priority` | string | `normal` | `normal` or `urgent` |
| `guild_id` | string | (auto) | Target Discord guild ID |
| `channel_id` | string | (auto) | Target Discord channel ID |
| `user_id` | string | (auto) | Target Discord user ID (for voicemail/notify) |

**Response:**

```json
{
  "status": "ok",
  "delivery": "live"
}
```

### GET /health

Returns webhook server status and active session count.

## Troubleshooting

**Webhook returns 401 Unauthorized:**
Check that the `Authorization: Bearer <token>` header matches `WEBHOOK_TOKEN` in your `.env`.

**"no active voice session with listeners":**
Nobody is in a voice channel with the bot. Use `mode: "auto"` to fall back to voicemail/notify, or join a voice channel first.

**Voicemail not appearing in DMs:**
The bot needs permission to DM the user. The user must share a server with the bot and not have DMs disabled for that server.

**"user_id required for voicemail/notify delivery":**
Set `WEBHOOK_NOTIFY_USER_IDS` in `.env` or pass `user_id` in the request body.

**Port not accessible from OpenClaw container:**
Ensure both containers are on the same Docker network. Use the container name as hostname (`http://discord-voice-assistant:18790`).
