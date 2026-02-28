"""Send Discord voice messages (voicemail) via the raw REST API.

Discord supports voice messages — audio files rendered with a waveform
visualizer and inline playback — in both DMs and guild channels.  This
requires flag ``8192`` (``IS_VOICE_MESSAGE``) and an ``.ogg`` Opus file.

discord.py does not have built-in support for sending voice messages,
so we use the REST API directly via aiohttp.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import wave

import aiohttp
import numpy as np

log = logging.getLogger(__name__)

DISCORD_API_BASE = "https://discord.com/api/v10"


async def wav_to_ogg_opus(wav_bytes: bytes) -> bytes | None:
    """Convert WAV audio to OGG Opus format using ffmpeg.

    Discord voice messages require ``.ogg`` files with Opus encoding
    (not Vorbis).  Returns ``None`` on failure.
    """
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-i", "pipe:0",
        "-c:a", "libopus", "-b:a", "64k",
        "-vbr", "on", "-application", "voip",
        "-f", "ogg", "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate(wav_bytes)
    if proc.returncode != 0:
        log.error(
            "ffmpeg WAV->OGG conversion failed (rc=%d): %s",
            proc.returncode, stderr.decode(errors="replace")[:500],
        )
        return None
    log.debug("WAV->OGG conversion: %d -> %d bytes", len(wav_bytes), len(stdout))
    return stdout


def calculate_waveform(wav_bytes: bytes, num_bars: int = 256) -> str:
    """Calculate waveform visualization data from WAV audio.

    Returns a base64-encoded byte array of amplitude values (0-255),
    used by Discord to render the waveform visualizer on voice messages.
    """
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            n_channels = wf.getnchannels()
            raw = wf.readframes(wf.getnframes())
            samples = np.frombuffer(raw, dtype=np.int16)

        # Convert stereo to mono
        if n_channels == 2 and len(samples) % 2 == 0:
            samples = samples.reshape(-1, 2).mean(axis=1).astype(np.int16)

        if len(samples) == 0:
            return base64.b64encode(bytes(num_bars)).decode()

        chunk_size = max(1, len(samples) // num_bars)
        bars = []
        for i in range(num_bars):
            start = i * chunk_size
            end = min(start + chunk_size, len(samples))
            if start >= len(samples):
                bars.append(0)
                continue
            segment = samples[start:end].astype(np.float32)
            rms = float(np.sqrt(np.mean(segment ** 2)))
            # Scale to 0-255 with amplification for visibility
            bar = min(255, int(rms / 32768 * 255 * 4))
            bars.append(bar)

        return base64.b64encode(bytes(bars)).decode()
    except Exception:
        log.exception("Failed to calculate waveform")
        return base64.b64encode(bytes(num_bars)).decode()


def get_wav_duration(wav_bytes: bytes) -> float:
    """Get duration of a WAV file in seconds."""
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            return wf.getnframes() / wf.getframerate()
    except Exception:
        return 1.0


async def create_dm_channel(
    http: aiohttp.ClientSession, bot_token: str, user_id: int
) -> int | None:
    """Create or retrieve a DM channel with a user.

    Returns the channel ID on success, ``None`` on failure.
    """
    url = f"{DISCORD_API_BASE}/users/@me/channels"
    headers = {
        "Authorization": f"Bot {bot_token}",
        "Content-Type": "application/json",
    }
    async with http.post(
        url, json={"recipient_id": str(user_id)}, headers=headers,
    ) as resp:
        if resp.status == 200:
            data = await resp.json()
            return int(data["id"])
        text = await resp.text()
        log.error(
            "Failed to create DM channel with user %d: %d %s",
            user_id, resp.status, text[:300],
        )
        return None


async def send_voice_message(
    http: aiohttp.ClientSession,
    bot_token: str,
    channel_id: int,
    ogg_bytes: bytes,
    duration_secs: float,
    waveform_b64: str,
) -> bool:
    """Send a voice message to a Discord channel using the raw REST API.

    Uses the ``IS_VOICE_MESSAGE`` flag (``8192``) so Discord renders it
    as a playable voice message with waveform visualization.

    The process is three steps:
    1. Request an upload URL from Discord.
    2. Upload the ``.ogg`` file to the returned URL.
    3. Send the message with the voice message flag.
    """
    headers = {"Authorization": f"Bot {bot_token}"}
    json_headers = {**headers, "Content-Type": "application/json"}

    # Step 1: Request upload URL
    attach_url = f"{DISCORD_API_BASE}/channels/{channel_id}/attachments"
    attach_payload = {
        "files": [{
            "filename": "voice-message.ogg",
            "file_size": len(ogg_bytes),
            "id": "0",
        }]
    }
    async with http.post(
        attach_url, json=attach_payload, headers=json_headers,
    ) as resp:
        if resp.status != 200:
            text = await resp.text()
            log.error(
                "Failed to request upload URL: %d %s", resp.status, text[:300],
            )
            return False
        data = await resp.json()

    attachments = data.get("attachments", [])
    if not attachments:
        log.error("No upload URL returned from Discord")
        return False

    upload_url = attachments[0]["upload_url"]
    uploaded_filename = attachments[0]["upload_filename"]

    # Step 2: Upload the .ogg file
    async with http.put(
        upload_url, data=ogg_bytes, headers={"Content-Type": "audio/ogg"},
    ) as resp:
        if resp.status not in (200, 204):
            log.error("Failed to upload voice message file: %d", resp.status)
            return False

    # Step 3: Send message with IS_VOICE_MESSAGE flag
    msg_url = f"{DISCORD_API_BASE}/channels/{channel_id}/messages"
    msg_payload = {
        "flags": 8192,
        "attachments": [{
            "id": "0",
            "filename": "voice-message.ogg",
            "uploaded_filename": uploaded_filename,
            "duration_secs": round(duration_secs, 2),
            "waveform": waveform_b64,
        }],
    }
    async with http.post(
        msg_url, json=msg_payload, headers=json_headers,
    ) as resp:
        if resp.status == 200:
            log.info(
                "Voice message sent to channel %d (%.1fs audio)",
                channel_id, duration_secs,
            )
            return True
        text = await resp.text()
        log.error(
            "Failed to send voice message: %d %s", resp.status, text[:500],
        )
        return False
