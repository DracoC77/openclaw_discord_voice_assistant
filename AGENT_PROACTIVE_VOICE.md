# Agent Guide: Setting Up Proactive Voice

This document is designed for an OpenClaw agent to read and follow autonomously. It explains how to configure the OpenClaw side for proactive voice communication with the Discord voice assistant sidecar.

## Overview

The Discord voice assistant sidecar exposes a webhook at `POST /speak` that accepts text and speaks it aloud in the user's Discord voice channel — or sends a voice message to their DMs. Your job is to set up the OpenClaw plugin tool and agent context so you can use this capability.

## What You Need to Do

### 1. Create the Speak Plugin

Create the plugin directory and two files:

```bash
mkdir -p ~/.openclaw/extensions/speak-plugin
```

**File: `~/.openclaw/extensions/speak-plugin/openclaw.plugin.json`**

```json
{
  "id": "speak-plugin",
  "name": "Discord Voice Speak",
  "description": "Send voice messages to the Discord voice assistant sidecar",
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

**File: `~/.openclaw/extensions/speak-plugin/index.ts`**

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

### 2. Configure the Webhook Token

The sidecar requires a Bearer token for authentication (unless running without auth on an isolated network). Set the token as an environment variable accessible to the OpenClaw gateway process:

```bash
export VOICE_WEBHOOK_TOKEN=<same token as WEBHOOK_TOKEN in the sidecar .env>
```

Or add it to your OpenClaw environment configuration file.

### 3. Allow the Speak Tool

In `~/.openclaw/openclaw.json`, ensure the `speak` tool is allowed for the agents that should use it:

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

If you use a dedicated `voice` agent, allow it there too.

### 4. Add Voice Context to AGENTS.md

Append the following to `~/.openclaw/workspace/AGENTS.md`:

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

### 5. (Optional) Set Up Cron Jobs for Scheduled Voice

For recurring proactive voice messages, create cron jobs. There are two approaches:

**Approach A: Cron triggers an agent turn that uses the `speak` tool**

```json5
{
  "schedule": { "kind": "cron", "expr": "0 9 * * *", "tz": "America/New_York" },
  "payload": {
    "kind": "agentTurn",
    "message": "Generate a brief morning briefing and use the speak tool to deliver it."
  }
}
```

**Approach B: Cron with webhook delivery directly to the sidecar**

```json5
{
  "schedule": { "kind": "cron", "expr": "0 9 * * *", "tz": "America/New_York" },
  "payload": {
    "kind": "agentTurn",
    "message": "Generate a brief morning status update."
  },
  "delivery": {
    "mode": "webhook",
    "to": "http://discord-voice-assistant:18790/speak"
  }
}
```

Approach A is more flexible (the agent decides what to say and how to deliver it). Approach B is simpler (the cron output goes directly to the sidecar).

## Speak Tool Reference

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `text` | string | (required) | What to say aloud. Keep it to 1-3 sentences. |
| `mode` | string | `"auto"` | Delivery mode: `auto`, `live`, `voicemail`, `notify` |
| `priority` | string | `"normal"` | `"normal"` or `"urgent"` |

### Delivery Modes

- **`auto`** — Tries live voice first. If nobody is in the voice channel, falls back to notify (DM + queue), then voicemail (DM voice message).
- **`live`** — Speaks directly in the voice channel. Fails if nobody is listening.
- **`voicemail`** — Generates a playable voice message and sends it to the user's Discord DMs.
- **`notify`** — Sends a text DM telling the user to join a voice channel, then queues the message. When the user joins, it plays automatically.

### Priority

- **`normal`** — Queued after other messages. Standard FIFO order.
- **`urgent`** — Moves to the front of the queue (but does not interrupt playback in progress).

### Example Calls

```
// Simple proactive message
speak({ text: "Your deployment to production completed successfully." })

// Urgent alert
speak({ text: "Alert: your server's CPU usage has exceeded 90%.", priority: "urgent" })

// Voicemail when user might be away
speak({ text: "I finished the research you asked about. Ask me when you're ready.", mode: "voicemail" })
```

## Network Configuration

The sidecar URL defaults to `http://discord-voice-assistant:18790`. This works when both containers are on the same Docker network. If your setup differs:

- Same host, different ports: `http://localhost:18790`
- Different hosts: `http://<sidecar-host-ip>:18790`
- Behind a reverse proxy: use the proxy URL

Update the `sidecarUrl` in the plugin config or set it via the `VOICE_SIDECAR_URL` environment variable.

## Verification

After setup, test the speak tool:

1. Tell the agent: "Use the speak tool to say hello"
2. Check the response — it should say `Voice delivery: live` (if you're in a voice channel) or `Voice delivery: voicemail`/`Voice delivery: notify`
3. You should hear the message in the voice channel or receive a voice message in your DMs
