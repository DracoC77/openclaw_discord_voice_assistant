# DAVE Protocol Compatibility Research

> **Date:** February 19, 2026
> **Deadline:** March 1, 2026 (hard cutoff)
> **Current Library (at time of research):** py-cord[voice] >= 2.6.0

> **Status: IMPLEMENTED** -- Option 1 (switch to discord.py) was implemented.
> The project now uses `discord.py[voice]` + `davey` + `discord-ext-voice-recv`.
> See the migration commit for details.

---

## Executive Summary

Discord's DAVE (Discord Audio & Video End-to-End Encryption) protocol becomes **mandatory on March 1, 2026**. After this date, any client or bot that does not support DAVE will be **blocked from joining voice channels** entirely. Our project currently uses **Pycord**, which has **no DAVE support and no active development effort** toward it. Action is required immediately.

---

## What is DAVE?

DAVE is Discord's end-to-end encryption protocol for voice and video. It ensures that only participants in a call can decrypt media -- not even Discord's servers can access the content.

**Scope:** DM calls, group DM calls, server voice channels, Go Live streams.
**Not covered:** Stage channels and stream previews.

### Timeline

| Date | Milestone |
|------|-----------|
| September 2024 | DAVE launched; supporting clients prefer E2EE |
| January 2026 | discord.py merges DAVE support (PR #10300) |
| **March 1, 2026** | **Hard cutoff -- clients without DAVE are blocked from voice** |

### Technical Components

1. **MLS (Messaging Layer Security, RFC 9420):** Group key exchange via new voice gateway opcodes (25-31)
2. **SFrame-inspired frame encryption:** AES-128-GCM encryption of audio/video frames with truncated 64-bit auth tags
3. **Codec-aware transformer:** Preserves unencrypted codec metadata for WebRTC packetization
4. **Identity keys:** ECDSA P256 key pairs per device (ephemeral by default)
5. **New voice gateway opcodes:** Opcodes 21-31 for protocol transitions, MLS key exchange, epoch management

Clients indicate DAVE support by including `max_dave_protocol_version` in the voice gateway Identify payload (Opcode 0).

---

## Current Project State

### Voice Architecture

```
Discord Voice Gateway (UDP + XSalsa20-Poly1305)
    |
    v
py-cord VoiceClient (automatic Opus decode/encode, encryption)
    |
    v
StreamingSink (custom) -> VAD -> Downsample -> STT -> LLM -> TTS
    |
    v
FFmpegPCMAudio -> py-cord OpusEncoder -> Discord
```

### Key Facts

- **Library:** `py-cord[voice]>=2.6.0`
- **Voice encryption:** Handled entirely by py-cord (XSalsa20-Poly1305 transport encryption only)
- **No DAVE awareness:** Project has zero code for MLS, SFrame, or the new voice gateway opcodes
- **Critical voice files:**
  - `discord_voice_assistant/voice_session.py` -- session orchestration, `channel.connect()`, `start_recording()`, `play()`
  - `discord_voice_assistant/voice_manager.py` -- session lifecycle
  - `discord_voice_assistant/audio/sink.py` -- custom StreamingSink for receiving audio
  - `discord_voice_assistant/audio/tts.py` -- TTS output via FFmpegPCMAudio

---

## Library Ecosystem Status

### Libraries WITH DAVE Support

| Library | Language | Status | Notes |
|---------|----------|--------|-------|
| **discord.py** | Python | **Merged** (Jan 7, 2026) | PR #10300 by Snazzah, uses `davey` package |
| **@discordjs/voice** | Node.js | **Shipped** (v0.14.0+) | Uses `@snazzah/davey` |
| **JDA** | Java | **Shipped** (v6.3.0+) | Via JDAVE or libdave-jvm |

### Libraries WITHOUT DAVE Support

| Library | Language | Status | Notes |
|---------|----------|--------|-------|
| **Pycord (py-cord)** | Python | **No support, no active work** | Zero issues or PRs for DAVE |
| **Songbird** | Rust | Transport crypto only | Not full DAVE E2EE |

### Key External Resources

| Resource | Description |
|----------|-------------|
| [discord/libdave](https://github.com/discord/libdave) | Official C++/JS reference implementation (v1.1.1, Jan 30 2026) |
| [Snazzah/davey](https://github.com/Snazzah/davey) | Community Rust implementation with Python + Node bindings |
| `davey` on PyPI | Python package v0.1.3 (pre-built wheels available) |
| [DAVE Protocol Spec](https://daveprotocol.com/) | Full protocol whitepaper |
| [Discord Voice Docs](https://docs.discord.com/developers/topics/voice-connections) | Updated with DAVE opcodes |

---

## Recommended Options

### Option 1: Switch from Pycord to discord.py (Recommended)

**Effort:** Moderate
**Risk:** Low-Medium
**DAVE Ready:** Yes, immediately

discord.py has had DAVE support merged since January 7, 2026 via the `davey` package. Since Pycord is a fork of discord.py, the APIs are very similar but not identical.

**What changes:**
1. Replace `py-cord[voice]>=2.6.0` with `discord.py[voice]>=2.x` (latest with DAVE) + `davey>=0.1.3` in requirements.txt
2. Update imports: `import discord` remains the same module name, but some API differences exist:
   - Slash commands: Pycord uses `@bot.slash_command()`, discord.py uses `@app_commands` with `discord.ext.commands`
   - `discord.VoiceClient` recording API may differ (Pycord has `start_recording()`/`stop_recording()` with sinks; discord.py may handle this differently)
   - Event decorator patterns may vary slightly
3. Verify/adapt the custom `StreamingSink` class -- the audio receive API is the most likely breaking point
4. Test voice connection, recording, and playback end-to-end
5. DAVE should work transparently once discord.py + davey are installed

**Advantages:**
- DAVE works out of the box (discord.py handles the MLS handshake, frame encryption/decryption internally)
- discord.py is the most actively maintained Python Discord library
- `davey` package ships pre-built native wheels -- no Rust toolchain needed

**Risks:**
- Pycord's `start_recording()` + `StreamingSink` audio receive API may not have a direct discord.py equivalent; custom audio receive code may need rework
- Slash command registration syntax differences
- Need to verify discord.py's voice receive capabilities match what we need

### Option 2: Port discord.py's DAVE Implementation to Pycord

**Effort:** High
**Risk:** Medium-High
**DAVE Ready:** After significant development

Since Pycord is a discord.py fork, the internal voice client code shares a common ancestor. The DAVE changes from discord.py PR #10300 could theoretically be cherry-picked or ported.

**What changes:**
1. Add `davey>=0.1.3` as a dependency
2. Port the DAVE protocol handling from discord.py's voice client into Pycord's voice client:
   - Voice gateway opcode handling (21-31)
   - MLS session management
   - Frame-level encryption/decryption in the audio send/receive path
   - `max_dave_protocol_version` in Identify payload
3. Test thoroughly

**Advantages:**
- No changes to bot-level code (slash commands, sinks, etc.)
- Keeps using Pycord's richer slash command API and recording features

**Risks:**
- Pycord and discord.py have diverged significantly since the fork
- Requires deep understanding of both libraries' voice internals
- Ongoing maintenance burden (must keep DAVE code in sync with protocol updates)
- Time-intensive given the March 1 deadline

### Option 3: Implement DAVE Directly via davey + Pycord Monkey-Patching

**Effort:** Very High
**Risk:** High
**DAVE Ready:** After significant development

Integrate the `davey` Python package directly and patch Pycord's voice client to handle DAVE.

**What changes:**
1. Add `davey>=0.1.3` as a dependency
2. Subclass or monkey-patch `discord.VoiceClient` to:
   - Send `max_dave_protocol_version` in Identify
   - Handle new voice gateway opcodes
   - Encrypt outgoing audio frames via davey
   - Decrypt incoming audio frames via davey
   - Manage MLS sessions and epoch transitions

**Advantages:**
- Stays on Pycord
- Full control over implementation

**Risks:**
- Most complex option
- Fragile (depends on Pycord internals that may change)
- Hardest to maintain

### Option 4: Wait for Pycord to Add DAVE Support

**Effort:** None (for us)
**Risk:** Very High
**DAVE Ready:** Unknown timeline

**Not recommended.** Pycord has zero visible DAVE work (no issues, no PRs). With the March 1 deadline 10 days away, there is no indication this will happen in time.

---

## Recommendation

**Option 1 (switch to discord.py) is the recommended path.** Here's why:

1. **It's the only option that provides DAVE support today** with no custom protocol implementation work
2. **The migration effort is bounded** -- the APIs are similar, and the main work is adapting the audio receive pipeline and slash commands
3. **discord.py is actively maintained** and will track future DAVE protocol updates
4. **The `davey` package handles all cryptographic complexity** transparently

### Suggested Migration Steps

1. **Audit API differences** between Pycord and discord.py for all features we use (voice connect, recording/receive, playback, slash commands, events)
2. **Prototype discord.py voice receive** to confirm we can replicate the StreamingSink audio capture pattern
3. **Create a migration branch** and port each component:
   - `bot.py` -- command registration, event handlers
   - `voice_session.py` -- voice connect, recording, playback
   - `voice_manager.py` -- session lifecycle (likely minimal changes)
   - `audio/sink.py` -- audio receive (most likely to need rework)
   - Other files -- likely minimal or no changes
4. **Add `davey>=0.1.3`** to requirements.txt (and system deps if any)
5. **Test DAVE E2EE** in a real voice channel to confirm the lock icon appears
6. **Deploy** before March 1, 2026

---

## References

- [Discord Blog: Meet DAVE](https://discord.com/blog/meet-dave-e2ee-for-audio-video) (Sept 2024)
- [Discord Blog: Bringing DAVE to All Platforms](https://discord.com/blog/bringing-dave-to-all-discord-platforms) (March 1 2026 deadline)
- [DAVE Protocol Whitepaper](https://daveprotocol.com/)
- [discord/dave-protocol](https://github.com/discord/dave-protocol) (protocol spec)
- [discord/libdave](https://github.com/discord/libdave) (official C++/JS implementation)
- [Snazzah/davey](https://github.com/Snazzah/davey) (Rust implementation with Python bindings)
- [davey on PyPI](https://pypi.org/project/davey/) (v0.1.3)
- [discord.py PR #10300](https://github.com/Rapptz/discord.py/pull/10300) (DAVE support, merged Jan 7 2026)
- [Discord Voice Connections Docs](https://docs.discord.com/developers/topics/voice-connections)
