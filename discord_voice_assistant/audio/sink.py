"""Custom audio sink that streams per-user audio chunks for real-time processing.

Receives decoded PCM audio (48kHz stereo 16-bit) from the voice bridge
and handles buffering and downsampling before passing to the pipeline.

The voice bridge already performs silence-based segmentation (EndBehaviorType.AfterSilence),
so each audio message is a complete speech utterance. The process_segment() method handles
this case directly without additional VAD.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from typing import Callable, Awaitable

import numpy as np

log = logging.getLogger(__name__)

# Discord sends Opus at 48kHz stereo, decoded to PCM by the voice bridge
DISCORD_SAMPLE_RATE = 48000
TARGET_SAMPLE_RATE = 16000
# Process audio in chunks of this many seconds
CHUNK_DURATION = 3.0
# Silence threshold - below this RMS, consider it silence
SILENCE_THRESHOLD = 300
# Higher threshold used while the bot is playing audio.  During playback,
# microphone pickup (echo, ambient noise, road noise) can produce segments
# with RMS 200-400 which Whisper may hallucinate text from, triggering a
# cascade of self-responses.  Real speech directed at the mic typically
# registers RMS 1500+ even in noisy environments.
PLAYBACK_SPEECH_THRESHOLD = 1200
# How long to wait after speech stops before processing (voice activity detection)
VAD_SILENCE_DURATION = 1.0
# Maximum buffer size per user (bytes) — ~120s of 48kHz stereo 16-bit audio
MAX_BUFFER_SIZE = DISCORD_SAMPLE_RATE * 2 * 2 * 120  # ~23 MB


class StreamingSink:
    """A custom sink that buffers per-user audio and triggers processing
    when speech segments are detected (via simple energy-based VAD).

    Audio flow:
    1. Voice bridge delivers decoded PCM (48kHz, stereo, 16-bit) per user
    2. We buffer it per-user
    3. When we detect end-of-speech (silence after audio), we:
       a. Downsample 48kHz stereo -> 16kHz mono
       b. Call the callback with the processed audio

    Pipeline tasks are decoupled from silence detection tasks so that
    new speech arriving while the pipeline is running (STT -> LLM -> TTS)
    does not cancel the in-progress response.
    """

    def __init__(
        self,
        callback: Callable[[int, bytes, int], Awaitable[None]],
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._callback = callback
        self._loop = loop
        # user_id -> list of raw PCM bytes chunks
        self._buffers: dict[int, bytearray] = defaultdict(bytearray)
        # user_id -> timestamp of last voice activity
        self._last_speech: dict[int, float] = {}
        # user_id -> whether currently speaking
        self._speaking: dict[int, bool] = defaultdict(bool)
        # user_id -> asyncio.Task for silence monitoring (cancelable by new speech)
        self._silence_tasks: dict[int, asyncio.Task] = {}
        # Independent pipeline tasks that must NOT be canceled by new speech
        self._pipeline_tasks: set[asyncio.Task] = set()
        # True while the bot is playing TTS audio.  When active, only
        # segments exceeding PLAYBACK_SPEECH_THRESHOLD are accepted
        # (filters echo/ambient noise, allows real barge-in speech).
        self._playback_active: bool = False
        # Monotonically increasing counter bumped by drain().  Pipeline
        # tasks capture the current epoch; if drain() fires before they
        # start processing, the epoch mismatch causes them to be skipped.
        self._epoch: int = 0

    def write(self, user_id: int, pcm: bytes) -> None:
        """Process a chunk of PCM audio from a user.

        Called from the async context when audio arrives from the bridge.
        Data is raw PCM: 48kHz, 2 channels (stereo), 16-bit signed LE.
        """
        if not pcm:
            return

        # Check if this chunk contains actual speech (energy-based VAD)
        rms = self._compute_rms(pcm)

        if rms > SILENCE_THRESHOLD:
            if not self._speaking[user_id]:
                log.debug(
                    "Speech START for user %d (rms=%.0f, threshold=%d)",
                    user_id, rms, SILENCE_THRESHOLD,
                )
            self._speaking[user_id] = True
            self._last_speech[user_id] = time.monotonic()

            # Protect against unbounded buffer growth
            if len(self._buffers[user_id]) + len(pcm) > MAX_BUFFER_SIZE:
                log.warning(
                    "Buffer overflow for user %d (%d bytes), flushing",
                    user_id, len(self._buffers[user_id]),
                )
                self._speaking[user_id] = False
                self._cancel_silence_task(user_id)
                task = self._loop.create_task(self._flush_buffer(user_id))
                self._pipeline_tasks.add(task)
                task.add_done_callback(self._pipeline_tasks.discard)
                return

            self._buffers[user_id].extend(pcm)
            log.debug(
                "Audio buffered user=%d rms=%.0f buf=%d bytes",
                user_id, rms, len(self._buffers[user_id]),
            )

            # Cancel any pending silence task
            if user_id in self._silence_tasks:
                self._cancel_silence_task(user_id)
        elif self._speaking[user_id]:
            # Still accumulate a little silence at the end for natural cutoff
            self._buffers[user_id].extend(pcm)

            # Start silence monitoring if not already running
            if user_id not in self._silence_tasks:
                log.debug(
                    "Silence after speech for user %d (rms=%.0f), starting VAD timer",
                    user_id, rms,
                )
                self._start_silence_check(user_id)

    @property
    def playback_active(self) -> bool:
        """Whether the bot is currently playing TTS audio."""
        return self._playback_active

    def set_playback_active(self, active: bool) -> None:
        """Toggle playback state, which raises the speech detection threshold."""
        self._playback_active = active
        if active:
            log.debug(
                "Playback active: speech threshold raised to %d",
                PLAYBACK_SPEECH_THRESHOLD,
            )
        else:
            log.debug("Playback ended: speech threshold restored to %d", SILENCE_THRESHOLD)

    def process_segment(self, user_id: int, pcm: bytes) -> None:
        """Process a complete speech segment from the voice bridge.

        The bridge already handles silence detection (EndBehaviorType.AfterSilence),
        so each audio message is a complete utterance. This method bypasses the
        sink's VAD and directly schedules the audio for pipeline processing.

        Data is raw PCM: 48kHz, 2 channels (stereo), 16-bit signed LE.
        """
        if not pcm:
            return

        rms = self._compute_rms(pcm)
        audio_secs = len(pcm) / (DISCORD_SAMPLE_RATE * 2 * 2)
        log.debug(
            "Complete segment from user %d: %d bytes (%.2fs, rms=%.0f)",
            user_id, len(pcm), audio_secs, rms,
        )

        threshold = PLAYBACK_SPEECH_THRESHOLD if self._playback_active else SILENCE_THRESHOLD
        if rms <= threshold:
            if self._playback_active:
                log.debug(
                    "Segment during playback below speech threshold "
                    "(rms=%.0f, need>%d), skipping",
                    rms, threshold,
                )
            else:
                log.debug("Segment is silence (rms=%.0f), skipping", rms)
            return

        # Schedule processing as an independent task.
        # Capture the current epoch so stale tasks (created before a drain)
        # can be detected and skipped in _process_raw_segment.
        epoch = self._epoch
        task = self._loop.create_task(self._process_raw_segment(user_id, pcm, epoch))
        self._pipeline_tasks.add(task)
        task.add_done_callback(self._pipeline_tasks.discard)

    async def _process_raw_segment(self, user_id: int, raw: bytes, epoch: int) -> None:
        """Downsample and pass a complete segment to the pipeline callback."""
        log.debug(
            "Processing segment for user %d: %d bytes raw (%.2fs at 48kHz stereo)",
            user_id, len(raw), len(raw) / (DISCORD_SAMPLE_RATE * 2 * 2),
        )

        try:
            mono_16k = self._downsample(raw)
        except Exception:
            log.exception("Error downsampling audio for user %d", user_id)
            return

        audio_duration = len(mono_16k) / (TARGET_SAMPLE_RATE * 2)

        min_bytes = TARGET_SAMPLE_RATE  # 16000 bytes = 0.5s
        if len(mono_16k) < min_bytes:
            log.debug(
                "Segment too short for user %d (%.2fs), skipping", user_id, audio_duration,
            )
            return

        # A drain() since this task was created means the audio is stale
        # (e.g. echo captured during playback that was drained after).
        if epoch != self._epoch:
            log.debug(
                "Segment for user %d is stale (epoch %d vs current %d), skipping",
                user_id, epoch, self._epoch,
            )
            return

        log.debug(
            "Sending segment to pipeline: user=%d, %d bytes (%.2fs at 16kHz)",
            user_id, len(mono_16k), audio_duration,
        )

        try:
            await self._callback(user_id, mono_16k, TARGET_SAMPLE_RATE)
        except Exception:
            log.exception("Error in audio callback for user %d", user_id)

    def _start_silence_check(self, user_id: int) -> None:
        """Schedule a silence check task on the event loop."""
        if user_id in self._silence_tasks:
            return
        self._silence_tasks[user_id] = self._loop.create_task(
            self._check_silence(user_id)
        )

    def _cancel_silence_task(self, user_id: int) -> None:
        """Cancel a pending silence check."""
        task = self._silence_tasks.pop(user_id, None)
        if task:
            task.cancel()

    async def _check_silence(self, user_id: int) -> None:
        """Wait for sustained silence, then flush the buffer for processing.

        This task is cancelable (new speech cancels it to reset the timer).
        When silence is confirmed, the actual pipeline work is scheduled as
        a separate independent task so it survives new-speech cancellation.
        """
        try:
            await asyncio.sleep(VAD_SILENCE_DURATION)

            # If still silent, process the buffered audio
            if not self._speaking.get(user_id):
                return

            now = time.monotonic()
            last = self._last_speech.get(user_id, 0)
            if now - last >= VAD_SILENCE_DURATION:
                log.debug(
                    "Speech END for user %d (silence=%.2fs), flushing buffer",
                    user_id, now - last,
                )
                self._speaking[user_id] = False
                # Schedule flush as an independent task so new speech
                # detection won't cancel the in-progress pipeline.
                task = self._loop.create_task(self._flush_buffer(user_id))
                self._pipeline_tasks.add(task)
                task.add_done_callback(self._pipeline_tasks.discard)
        except asyncio.CancelledError:
            pass
        finally:
            self._silence_tasks.pop(user_id, None)

    async def _flush_buffer(self, user_id: int) -> None:
        """Process accumulated audio for a user."""
        if user_id not in self._buffers or not self._buffers[user_id]:
            log.debug("Flush called for user %d but buffer is empty", user_id)
            return

        raw = bytes(self._buffers[user_id])
        self._buffers[user_id].clear()

        log.debug(
            "Flushing buffer for user %d: %d bytes raw (%.2fs at 48kHz stereo)",
            user_id, len(raw), len(raw) / (DISCORD_SAMPLE_RATE * 2 * 2),
        )

        # Convert to mono 16kHz
        try:
            mono_16k = self._downsample(raw)
        except Exception:
            log.exception("Error downsampling audio for user %d", user_id)
            return

        audio_duration = len(mono_16k) / (TARGET_SAMPLE_RATE * 2)  # 16-bit = 2 bytes/sample

        # Minimum audio length check: require at least 0.5s of audio
        # 16-bit audio = 2 bytes per sample, so 0.5s = sample_rate * 2 * 0.5
        min_bytes = TARGET_SAMPLE_RATE  # 16000 bytes = 8000 samples = 0.5s
        if len(mono_16k) < min_bytes:
            log.debug(
                "Audio too short for user %d (%.2fs), skipping", user_id, audio_duration,
            )
            return

        log.debug(
            "Sending audio chunk to pipeline: user=%d, %d bytes (%.2fs at 16kHz)",
            user_id, len(mono_16k), audio_duration,
        )

        try:
            await self._callback(user_id, mono_16k, TARGET_SAMPLE_RATE)
        except Exception:
            log.exception("Error in audio callback for user %d", user_id)

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
        """Convert 48kHz stereo 16-bit PCM to 16kHz mono 16-bit PCM.

        Uses a simple low-pass averaging filter before decimation to reduce
        aliasing artifacts, which is important for speech quality.
        """
        samples = np.frombuffer(raw_pcm, dtype=np.int16)

        # Stereo to mono: average pairs of samples
        if len(samples) % 2 == 0:
            stereo = samples.reshape(-1, 2)
            mono = stereo.mean(axis=1).astype(np.float32)
        else:
            mono = samples.astype(np.float32)

        # Downsample 48kHz -> 16kHz (factor of 3) with anti-aliasing.
        # Average groups of 3 samples instead of naive decimation to act
        # as a simple low-pass filter and reduce aliasing.
        trim = len(mono) - (len(mono) % 3)
        mono = mono[:trim]
        mono_16k = mono.reshape(-1, 3).mean(axis=1).astype(np.int16)

        return mono_16k.tobytes()

    def drain(self) -> None:
        """Discard all buffered audio and reset speaking states.

        Call after TTS playback to prevent the bot from processing audio
        that accumulated during playback (e.g. echo from users' microphones
        picking up the bot's speech).  Does NOT cancel in-progress pipeline
        tasks — only pending silence timers and unprocessed buffers.
        The epoch increment causes pending pipeline tasks to detect
        staleness and skip processing.
        """
        self._epoch += 1
        for uid in list(self._silence_tasks):
            self._cancel_silence_task(uid)
        self._buffers.clear()
        self._speaking.clear()
        self._last_speech.clear()
        log.debug("Audio drain: cleared all buffers and speaking states (epoch=%d)", self._epoch)

    def cleanup(self) -> None:
        """Clean up resources."""
        for task in self._silence_tasks.values():
            task.cancel()
        self._silence_tasks.clear()
        for task in self._pipeline_tasks:
            task.cancel()
        self._pipeline_tasks.clear()
        self._buffers.clear()
