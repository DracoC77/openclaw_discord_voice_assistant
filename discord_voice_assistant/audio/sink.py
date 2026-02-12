"""Custom Discord audio sink that streams per-user audio chunks for real-time processing."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from typing import Callable, Awaitable

import discord
import numpy as np

log = logging.getLogger(__name__)

# Discord sends Opus at 48kHz stereo, decoded to PCM by Pycord
DISCORD_SAMPLE_RATE = 48000
TARGET_SAMPLE_RATE = 16000
# Process audio in chunks of this many seconds
CHUNK_DURATION = 3.0
# Silence threshold - below this RMS, consider it silence
SILENCE_THRESHOLD = 300
# How long to wait after speech stops before processing (voice activity detection)
VAD_SILENCE_DURATION = 1.0


class StreamingSink(discord.sinks.Sink):
    """A custom sink that buffers per-user audio and triggers processing
    when speech segments are detected (via simple energy-based VAD).

    Audio flow:
    1. Discord delivers decoded PCM (48kHz, stereo, 16-bit) per user
    2. We buffer it per-user
    3. When we detect end-of-speech (silence after audio), we:
       a. Downsample 48kHz stereo -> 16kHz mono
       b. Call the callback with the processed audio
    """

    def __init__(
        self,
        callback: Callable[[int, bytes, int], Awaitable[None]],
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        super().__init__()
        self._callback = callback
        self._loop = loop
        # user_id -> list of raw PCM bytes chunks
        self._buffers: dict[int, bytearray] = defaultdict(bytearray)
        # user_id -> timestamp of last voice activity
        self._last_speech: dict[int, float] = {}
        # user_id -> whether currently speaking
        self._speaking: dict[int, bool] = defaultdict(bool)
        # user_id -> asyncio.Task for silence monitoring
        self._silence_tasks: dict[int, asyncio.Task] = {}

    @discord.sinks.Filters.container
    def write(self, data: bytes, user: int) -> None:
        """Called by Pycord for each audio packet from a user.

        NOTE: This runs in the recv_audio thread, NOT the event loop.
        Data is raw PCM: 48kHz, 2 channels (stereo), 16-bit signed LE.
        Each packet is typically 20ms of audio = 3840 bytes.
        """
        # Check if this chunk contains actual speech (energy-based VAD)
        rms = self._compute_rms(data)

        if rms > SILENCE_THRESHOLD:
            if not self._speaking[user]:
                log.debug(
                    "Speech START for user %d (rms=%.0f, threshold=%d)",
                    user, rms, SILENCE_THRESHOLD,
                )
            self._speaking[user] = True
            self._last_speech[user] = time.monotonic()
            self._buffers[user].extend(data)
            log.debug(
                "Audio buffered user=%d rms=%.0f buf=%d bytes",
                user, rms, len(self._buffers[user]),
            )

            # Cancel any pending silence task (thread-safe via call_soon_threadsafe)
            if user in self._silence_tasks:
                self._loop.call_soon_threadsafe(self._cancel_silence_task, user)
        elif self._speaking[user]:
            # Still accumulate a little silence at the end for natural cutoff
            self._buffers[user].extend(data)

            # Start silence monitoring if not already running
            if user not in self._silence_tasks:
                log.debug(
                    "Silence after speech for user %d (rms=%.0f), starting VAD timer",
                    user, rms,
                )
                self._loop.call_soon_threadsafe(self._start_silence_check, user)

    def _start_silence_check(self, user: int) -> None:
        """Schedule a silence check task on the event loop. Must be called from event loop."""
        if user in self._silence_tasks:
            return
        self._silence_tasks[user] = self._loop.create_task(
            self._check_silence(user)
        )

    def _cancel_silence_task(self, user: int) -> None:
        """Cancel a pending silence check. Must be called from event loop."""
        task = self._silence_tasks.pop(user, None)
        if task:
            task.cancel()

    async def _check_silence(self, user: int) -> None:
        """Wait for sustained silence, then flush the buffer for processing."""
        try:
            await asyncio.sleep(VAD_SILENCE_DURATION)

            # If still silent, process the buffered audio
            if not self._speaking.get(user):
                return

            now = time.monotonic()
            last = self._last_speech.get(user, 0)
            if now - last >= VAD_SILENCE_DURATION:
                log.debug(
                    "Speech END for user %d (silence=%.2fs), flushing buffer",
                    user, now - last,
                )
                self._speaking[user] = False
                await self._flush_buffer(user)
        except asyncio.CancelledError:
            pass
        finally:
            self._silence_tasks.pop(user, None)

    async def _flush_buffer(self, user: int) -> None:
        """Process accumulated audio for a user."""
        if user not in self._buffers or not self._buffers[user]:
            log.debug("Flush called for user %d but buffer is empty", user)
            return

        raw = bytes(self._buffers[user])
        self._buffers[user].clear()

        log.debug(
            "Flushing buffer for user %d: %d bytes raw (%.2fs at 48kHz stereo)",
            user, len(raw), len(raw) / (DISCORD_SAMPLE_RATE * 2 * 2),
        )

        # Convert to mono 16kHz
        try:
            mono_16k = self._downsample(raw)
        except Exception:
            log.exception("Error downsampling audio for user %d", user)
            return

        audio_duration = len(mono_16k) / (TARGET_SAMPLE_RATE * 2)  # 16-bit = 2 bytes/sample

        # Minimum audio length check (~0.5s at 16kHz)
        if len(mono_16k) < TARGET_SAMPLE_RATE:
            log.debug(
                "Audio too short for user %d (%.2fs), skipping", user, audio_duration,
            )
            return

        log.debug(
            "Sending audio chunk to pipeline: user=%d, %d bytes (%.2fs at 16kHz)",
            user, len(mono_16k), audio_duration,
        )

        try:
            await self._callback(user, mono_16k, TARGET_SAMPLE_RATE)
        except Exception:
            log.exception("Error in audio callback for user %d", user)

    @staticmethod
    def _compute_rms(data: bytes) -> float:
        """Compute RMS (root mean square) of 16-bit PCM audio."""
        if len(data) < 2:
            return 0.0
        samples = np.frombuffer(data, dtype=np.int16)
        if len(samples) == 0:
            return 0.0
        return float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))

    @staticmethod
    def _downsample(raw_pcm: bytes) -> bytes:
        """Convert 48kHz stereo 16-bit PCM to 16kHz mono 16-bit PCM."""
        samples = np.frombuffer(raw_pcm, dtype=np.int16)

        # Stereo to mono: average pairs of samples
        if len(samples) % 2 == 0:
            stereo = samples.reshape(-1, 2)
            mono = stereo.mean(axis=1).astype(np.int16)
        else:
            mono = samples

        # Downsample 48kHz -> 16kHz (factor of 3)
        # Simple decimation (take every 3rd sample) - works well for speech
        mono_16k = mono[::3]

        return mono_16k.tobytes()

    def cleanup(self) -> None:
        """Clean up resources."""
        for task in self._silence_tasks.values():
            task.cancel()
        self._silence_tasks.clear()
        self._buffers.clear()
        super().cleanup()
