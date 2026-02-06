"""Text-to-speech with ElevenLabs and local (Piper) backends."""

from __future__ import annotations

import asyncio
import io
import logging
import struct
import wave
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from clippy.config import TTSConfig

log = logging.getLogger(__name__)


class TextToSpeech:
    """Synthesizes speech from text using either ElevenLabs or local Piper TTS."""

    def __init__(self, config: TTSConfig) -> None:
        self.config = config
        self._piper = None
        self._elevenlabs_client = None

    async def synthesize(self, text: str) -> bytes | None:
        """Convert text to WAV audio bytes.

        Returns:
            WAV audio bytes suitable for FFmpeg playback, or None on failure.
        """
        if not text:
            return None

        try:
            if self.config.provider == "elevenlabs":
                return await self._synthesize_elevenlabs(text)
            else:
                return await self._synthesize_local(text)
        except Exception:
            log.exception("TTS synthesis failed")
            return None

    async def _synthesize_elevenlabs(self, text: str) -> bytes | None:
        """Synthesize using ElevenLabs API."""
        if self._elevenlabs_client is None:
            try:
                from elevenlabs import AsyncElevenLabs
                self._elevenlabs_client = AsyncElevenLabs(
                    api_key=self.config.elevenlabs_api_key
                )
            except ImportError:
                log.error(
                    "elevenlabs package not installed. "
                    "Install with: pip install elevenlabs"
                )
                return None

        audio_stream = await self._elevenlabs_client.text_to_speech.convert(
            voice_id=self.config.elevenlabs_voice_id,
            text=text,
            model_id="eleven_turbo_v2_5",
            output_format="pcm_16000",
        )

        # Collect all chunks
        pcm_data = b""
        async for chunk in audio_stream:
            pcm_data += chunk

        # Wrap raw PCM in WAV container for FFmpeg
        return self._pcm_to_wav(pcm_data, sample_rate=16000, channels=1)

    async def _synthesize_local(self, text: str) -> bytes | None:
        """Synthesize using local Piper TTS."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._synthesize_piper_sync, text)

    def _synthesize_piper_sync(self, text: str) -> bytes | None:
        """Synchronous Piper TTS synthesis."""
        if self._piper is None:
            try:
                from piper import PiperVoice
                log.info("Loading Piper TTS model: %s", self.config.local_model)
                self._piper = PiperVoice.load(self.config.local_model)
            except ImportError:
                log.error(
                    "piper-tts package not installed. "
                    "Install with: pip install piper-tts"
                )
                return self._synthesize_espeak_fallback(text)

        # Synthesize to WAV
        wav_buffer = io.BytesIO()
        with wave.open(wav_buffer, "wb") as wav_file:
            self._piper.synthesize(text, wav_file)

        return wav_buffer.getvalue()

    def _synthesize_espeak_fallback(self, text: str) -> bytes | None:
        """Ultimate fallback: use espeak via subprocess."""
        import subprocess

        try:
            result = subprocess.run(
                ["espeak-ng", "--stdout", "-s", "150", text],
                capture_output=True,
                timeout=30,
            )
            if result.returncode == 0 and result.stdout:
                return result.stdout
        except (FileNotFoundError, subprocess.TimeoutExpired):
            log.warning("espeak-ng not available as fallback TTS")
        return None

    @staticmethod
    def _pcm_to_wav(
        pcm_data: bytes, sample_rate: int = 16000, channels: int = 1
    ) -> bytes:
        """Wrap raw 16-bit PCM data in a WAV container."""
        wav_buffer = io.BytesIO()
        with wave.open(wav_buffer, "wb") as wav_file:
            wav_file.setnchannels(channels)
            wav_file.setsampwidth(2)  # 16-bit
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(pcm_data)
        return wav_buffer.getvalue()
