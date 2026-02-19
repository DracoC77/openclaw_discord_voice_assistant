# DAVE Protocol: Discord Voice E2EE

## Status: CRITICAL (Deadline March 1, 2026)

Discord is requiring end-to-end encryption (DAVE protocol) for all voice connections starting **March 1, 2026**. After this date, clients and bots without DAVE support will be **blocked from joining voice channels entirely**. There is no grace period.

**Stage channels are exempt** from this requirement.

## Current Impact on This Project

This project uses **py-cord** (`py-cord[voice]>=2.6.0`), which has **no DAVE support**. As of February 2026:

| Library | DAVE Support | Status |
|---------|-------------|--------|
| **py-cord** (this project) | None | No issue filed, no PR, no plans |
| **discord.py** | Merged (PR #10300, Jan 7, 2026) | Ready via `davey` package |
| **disnake** | In progress | Via `libdave.py` bindings |

## What is DAVE?

DAVE (Discord Audio and Video End-to-End Encryption) uses the MLS (Message Layer Security) protocol for group key exchange and AES-128-GCM for media frame encryption. Key components:

- **MLS Group Key Exchange**: Ciphersuite `DHKEMP256_AES128GCM_SHA256_P256`
- **Voice Gateway Opcodes 24-30**: New opcodes for key package exchange, proposals, commits, and welcome messages
- **Frame Encryption**: Each audio/video frame encrypted with per-sender ratcheted keys
- **Binary WebSocket Messages**: MLS-related opcodes use binary framing

Full spec: https://daveprotocol.com/ and https://github.com/discord/dave-protocol/blob/main/protocol.md

## Available Python DAVE Libraries

### `davey` (by Snazzah)
- **Repo**: https://github.com/Snazzah/davey
- **PyPI**: `pip install davey`
- **Implementation**: Rust core (OpenMLS) with Python bindings
- **Used by**: discord.py (official integration)
- **License**: MIT

### `libdave.py` (by DisnakeDev)
- **Repo**: https://github.com/DisnakeDev/libdave.py
- **Implementation**: Python bindings for Discord's official `libdave` C++ library
- **License**: MIT
- **Note**: Requires building from source or prebuilt wheels

## Migration Options

### Option 1: Port DAVE to py-cord (Recommended)

Since py-cord is a fork of discord.py, we can adapt discord.py's DAVE implementation (PR #10300) for py-cord. This involves:

1. Add `davey` as a dependency
2. Modify py-cord's `voice_client.py` to:
   - Send `max_dave_protocol_version` in the voice gateway `identify` opcode
   - Handle opcodes 24-30 for MLS key exchange lifecycle
   - Encrypt outgoing audio frames
   - Decrypt incoming audio frames (needed for the Sink/recording feature)
3. Modify `gateway.py` to handle binary WebSocket messages
4. Add a `dave_session` state object for managing MLS group state

**Why this is recommended**: We depend on py-cord's `Sink` class for audio recording (STT pipeline). discord.py does not have an equivalent recording API. Migrating away from py-cord would require reimplementing the audio recording layer from scratch.

**Estimated complexity**: Medium-high. The core changes from discord.py PR #10300 need adaptation for py-cord's slightly different internal API. The `davey` package handles the heavy cryptography.

### Option 2: Migrate to discord.py

Move from py-cord to discord.py, which already has DAVE support.

**Problem**: discord.py does not have a built-in `Sink` class for recording audio. The entire audio recording pipeline (`StreamingSink`, VAD, per-user buffering) would need to be reimplemented using discord.py's raw voice WebSocket connection and Opus decoder.

**Estimated complexity**: High. Major rewrite of the audio pipeline.

### Option 3: Use Stage Channels Only

Stage channels are exempt from DAVE. The bot could operate exclusively in Stage channels, where it becomes a "speaker" and users are the "audience."

**Problems**:
- Stage channels have a speaker/audience model, not free-form voice
- The bot needs moderator permissions to become a speaker
- Not suitable for conversational interactions
- Stage channels auto-delete when no speakers remain

**Verdict**: Workaround only, not a real solution.

### Option 4: Contribute DAVE to py-cord Upstream

File an issue and PR on the py-cord repository with the DAVE implementation. Benefits the entire py-cord community.

**Risk**: py-cord appears less actively maintained than discord.py. Review and merge timeline uncertain.

## Recommended Action Plan

1. **Immediate**: File an issue on py-cord's GitHub requesting DAVE support
2. **Short-term**: Fork py-cord and port the DAVE changes from discord.py PR #10300 using `davey`
3. **Medium-term**: Submit the fork as a PR to upstream py-cord
4. **If py-cord stalls**: Evaluate migrating to discord.py with a custom audio recording layer

## References

- [Discord Blog: Bringing DAVE to All Platforms](https://discord.com/blog/bringing-dave-to-all-discord-platforms)
- [DAVE Protocol Whitepaper](https://daveprotocol.com/)
- [DAVE Protocol Spec (GitHub)](https://github.com/discord/dave-protocol/blob/main/protocol.md)
- [Discord libdave C++/JS](https://github.com/discord/libdave)
- [discord.py PR #10300 (DAVE merged)](https://github.com/Rapptz/discord.py/pull/10300)
- [discord.py Issue #9948](https://github.com/Rapptz/discord.py/issues/9948)
- [Snazzah/davey](https://github.com/Snazzah/davey)
- [DisnakeDev/libdave.py](https://github.com/DisnakeDev/libdave.py)
- [Discord E2EE Support Article](https://support.discord.com/hc/en-us/articles/25968222946071)
