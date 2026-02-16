"""Speech-to-text using Faster Whisper."""

from __future__ import annotations

import asyncio
import logging
import time
from functools import lru_cache
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from discord_voice_assistant.config import STTConfig

log = logging.getLogger(__name__)


class SpeechToText:
    """Transcribes audio using Faster Whisper (CTranslate2-based Whisper)."""

    def __init__(self, config: STTConfig) -> None:
        self.config = config
        self._model = None
        self._model_lock = asyncio.Lock()

    def _get_model(self):
        """Lazy-load the Whisper model."""
        if self._model is None:
            log.info(
                "Loading Faster Whisper model: %s (device=%s, compute=%s)",
                self.config.model_size,
                self.config.device,
                self.config.compute_type,
            )
            from faster_whisper import WhisperModel

            device = self.config.device
            if device == "auto":
                try:
                    import torch
                    device = "cuda" if torch.cuda.is_available() else "cpu"
                except ImportError:
                    device = "cpu"

            self._model = WhisperModel(
                self.config.model_size,
                device=device,
                compute_type=self.config.compute_type,
            )
            log.info("Whisper model loaded successfully")
        return self._model

    async def warm_up(self) -> None:
        """Pre-load the Whisper model so the first transcription isn't delayed."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._get_model)

    async def transcribe(self, audio_data: bytes, sample_rate: int = 16000) -> str:
        """Transcribe audio bytes (16-bit PCM, mono) to text.

        Args:
            audio_data: Raw 16-bit PCM audio bytes
            sample_rate: Sample rate of the audio (should be 16000)

        Returns:
            Transcribed text, or empty string if nothing detected.
        """
        # Convert bytes to float32 numpy array (Whisper expects float32 in [-1, 1])
        audio_np = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0

        if len(audio_np) == 0:
            log.debug("STT received empty audio, returning empty string")
            return ""

        audio_duration = len(audio_np) / sample_rate
        log.debug(
            "STT transcribing %.2fs of audio (%d samples at %dHz)",
            audio_duration, len(audio_np), sample_rate,
        )

        # Run transcription in a thread to avoid blocking the event loop
        loop = asyncio.get_running_loop()
        text = await loop.run_in_executor(None, self._transcribe_sync, audio_np)
        return text

    def _transcribe_sync(self, audio: np.ndarray) -> str:
        """Synchronous transcription."""
        model = self._get_model()

        t0 = time.monotonic()
        segments, info = model.transcribe(
            audio,
            beam_size=5,
            language="en",
            vad_filter=True,
            vad_parameters=dict(
                min_silence_duration_ms=500,
                speech_pad_ms=200,
            ),
        )

        text_parts = []
        for segment in segments:
            log.debug(
                "STT segment [%.2f-%.2f]: %r",
                segment.start, segment.end, segment.text.strip(),
            )
            text_parts.append(segment.text.strip())

        elapsed = time.monotonic() - t0
        result = " ".join(text_parts).strip()

        if result:
            log.debug(
                "STT result (lang=%s, prob=%.2f, %.3fs): %s",
                info.language,
                info.language_probability,
                elapsed,
                result[:120],
            )
        else:
            log.debug(
                "STT returned no speech (lang=%s, prob=%.2f, %.3fs)",
                info.language, info.language_probability, elapsed,
            )

        return result
