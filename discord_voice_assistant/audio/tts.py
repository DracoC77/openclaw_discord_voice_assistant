"""Text-to-speech with ElevenLabs and local (Piper) backends."""

from __future__ import annotations

import asyncio
import io
import logging
import re
import struct
import wave
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from discord_voice_assistant.config import TTSConfig

log = logging.getLogger(__name__)

# Regex patterns for stripping markdown/emoji before TTS
_MARKDOWN_PATTERNS = [
    (re.compile(r"```.*?```", re.DOTALL), ""),           # code blocks
    (re.compile(r"`([^`]+)`"), r"\1"),                    # inline code
    (re.compile(r"\*\*(.+?)\*\*"), r"\1"),                # bold
    (re.compile(r"__(.+?)__"), r"\1"),                     # bold alt
    (re.compile(r"\*(.+?)\*"), r"\1"),                     # italic
    (re.compile(r"_(.+?)_"), r"\1"),                       # italic alt
    (re.compile(r"~~(.+?)~~"), r"\1"),                     # strikethrough
    (re.compile(r"^#{1,6}\s+", re.MULTILINE), ""),         # headers
    (re.compile(r"^\s*[-*+]\s+", re.MULTILINE), ""),       # bullet points
    (re.compile(r"\[([^\]]+)\]\([^)]+\)"), r"\1"),         # links
]
# Common emoji ranges
_EMOJI_RE = re.compile(
    "["
    "\U0001f300-\U0001f9ff"  # misc symbols, emoticons, etc.
    "\U00002702-\U000027b0"  # dingbats
    "\U0000fe00-\U0000fe0f"  # variation selectors
    "\U0000200d"             # zero-width joiner
    "\u2600-\u26ff"          # misc symbols
    "\u2700-\u27bf"          # dingbats
    "\u2300-\u23ff"          # misc technical
    "\u2b50-\u2b55"          # stars
    "\u200d"                 # zwj
    "\u2934-\u2935"          # arrows
    "\u25aa-\u25fe"          # geometric shapes
    "\u2139"                 # info
    "\u2194-\u21aa"          # arrows
    "\u2714\u2716\u2728"     # check, x, sparkles
    "]+",
    flags=re.UNICODE,
)


def _clean_for_tts(text: str) -> str:
    """Strip markdown formatting and emoji so TTS reads naturally."""
    for pattern, replacement in _MARKDOWN_PATTERNS:
        text = pattern.sub(replacement, text)
    text = _EMOJI_RE.sub("", text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


class TextToSpeech:
    """Synthesizes speech from text using either ElevenLabs or local Piper TTS."""

    def __init__(self, config: TTSConfig) -> None:
        self.config = config
        self._elevenlabs_client = None

    async def synthesize(self, text: str) -> bytes | None:
        """Convert text to WAV audio bytes.

        Returns:
            WAV audio bytes suitable for FFmpeg playback, or None on failure.
        """
        if not text:
            return None

        # Strip markdown and emoji so TTS reads naturally
        text = _clean_for_tts(text)
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
        """Synchronous Piper TTS synthesis via CLI subprocess.

        Uses the piper CLI (installed by piper-tts) rather than the Python API,
        which has version-specific bugs with wave header writing.
        """
        import subprocess

        model_path = self.config.local_model

        try:
            result = subprocess.run(
                ["piper", "--model", model_path, "--output_file", "-"],
                input=text.encode("utf-8"),
                capture_output=True,
                timeout=60,
            )
            if result.returncode == 0 and len(result.stdout) > 44:
                return result.stdout  # WAV format
            log.warning(
                "Piper produced no audio (rc=%d, stderr=%s)",
                result.returncode,
                result.stderr.decode(errors="replace")[:200],
            )
        except FileNotFoundError:
            log.warning("piper CLI not found, falling back to espeak-ng")
        except subprocess.TimeoutExpired:
            log.warning("Piper TTS timed out")

        return self._synthesize_espeak_fallback(text)

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
