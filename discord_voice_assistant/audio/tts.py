"""Text-to-speech with ElevenLabs and local (Piper) backends."""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import struct
import time
import wave
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from discord_voice_assistant.config import TTSConfig

log = logging.getLogger(__name__)

# Where Piper models are stored inside the container
_PIPER_MODEL_DIR = Path(os.getenv("PIPER_MODEL_DIR", "/opt/piper"))

# HuggingFace base URL for downloading Piper voice models
_PIPER_HF_BASE = (
    "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0"
)

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
    (re.compile(r"^\s*[-*+•]\s+", re.MULTILINE), "Next, "),  # bullet points → spoken transition
    (re.compile(r"^\s*\d+\.\s+", re.MULTILINE), ""),        # numbered lists
    (re.compile(r"\[([^\]]+)\]\([^)]+\)"), r"\1"),           # links
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


# Amplitude threshold for detecting "silence" in 16-bit PCM.
# Samples with abs(value) below this are considered silent.
_SILENCE_THRESHOLD = 256


def _strip_leading_silence(wav_bytes: bytes) -> bytes:
    """Strip leading silent samples from WAV audio.

    Piper TTS sometimes produces a brief leading silence that adds
    perceived latency to the first sentence.  This detects and removes
    the silent prefix so audio starts immediately.
    """
    try:
        buf = io.BytesIO(wav_bytes)
        with wave.open(buf, "rb") as wf:
            nchannels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            framerate = wf.getframerate()
            frames = wf.readframes(wf.getnframes())

        if sampwidth != 2:
            # Only handle 16-bit PCM (the only format Piper/ElevenLabs produce)
            return wav_bytes

        frame_size = sampwidth * nchannels
        first_audible = 0

        for i in range(0, len(frames), frame_size):
            audible = False
            for ch in range(nchannels):
                sample = struct.unpack_from("<h", frames, i + ch * sampwidth)[0]
                if abs(sample) >= _SILENCE_THRESHOLD:
                    audible = True
                    break
            if audible:
                first_audible = i
                break
        else:
            # Entire clip is silence — return original unchanged
            return wav_bytes

        if first_audible == 0:
            return wav_bytes

        trimmed = frames[first_audible:]
        out = io.BytesIO()
        with wave.open(out, "wb") as wf:
            wf.setnchannels(nchannels)
            wf.setsampwidth(sampwidth)
            wf.setframerate(framerate)
            wf.writeframes(trimmed)

        stripped_ms = (first_audible / frame_size) / framerate * 1000
        log.debug("Stripped %.0fms leading silence from TTS audio", stripped_ms)
        return out.getvalue()
    except Exception:
        log.debug("Failed to strip leading silence, using original", exc_info=True)
        return wav_bytes


def _clean_for_tts(text: str) -> str:
    """Strip markdown formatting and emoji so TTS reads naturally."""
    for pattern, replacement in _MARKDOWN_PATTERNS:
        text = pattern.sub(replacement, text)
    text = _EMOJI_RE.sub("", text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    # Clean up spoken transitions at boundaries
    if text.startswith("Next, "):
        text = text[6:]
    if text.endswith("Next,"):
        text = text[:-5].strip()
    return text


def _resolve_piper_model(model: str) -> str:
    """Resolve a Piper model name or path to an absolute .onnx path.

    Accepts:
      - Full path:  /opt/piper/en_US-lessac-medium.onnx  (returned as-is)
      - Model name:  en_US-lessac-medium  (resolved to /opt/piper/<name>.onnx)

    If the resolved file doesn't exist, attempts to download it from HuggingFace.
    """
    # If it's already a full path, use it directly
    if os.path.sep in model or model.endswith(".onnx"):
        return model

    # It's a model name like "en_US-lessac-medium" — resolve to path
    onnx_path = _PIPER_MODEL_DIR / f"{model}.onnx"
    json_path = _PIPER_MODEL_DIR / f"{model}.onnx.json"

    if onnx_path.exists():
        return str(onnx_path)

    # Auto-download from HuggingFace
    # Model name format: en_US-lessac-medium → en/en_US/lessac/medium/en_US-lessac-medium.onnx
    try:
        parts = model.split("-", 1)
        if len(parts) != 2:
            log.error("Invalid Piper model name '%s' — expected format: en_US-voice-quality", model)
            return str(onnx_path)

        locale = parts[0]  # e.g. "en_US"
        voice_quality = parts[1]  # e.g. "lessac-medium"
        lang = locale.split("_")[0]  # e.g. "en"

        # voice_quality could be "lessac-medium" or "hfc_female-medium"
        # Split on the LAST hyphen to get voice and quality
        last_dash = voice_quality.rfind("-")
        if last_dash == -1:
            log.error("Invalid Piper model name '%s' — missing quality level", model)
            return str(onnx_path)

        voice = voice_quality[:last_dash]   # e.g. "lessac" or "hfc_female"
        quality = voice_quality[last_dash + 1:]  # e.g. "medium" or "high"

        base_url = f"{_PIPER_HF_BASE}/{lang}/{locale}/{voice}/{quality}"
        onnx_url = f"{base_url}/{model}.onnx"
        json_url = f"{base_url}/{model}.onnx.json"

        log.info("Downloading Piper model '%s' from HuggingFace...", model)
        _PIPER_MODEL_DIR.mkdir(parents=True, exist_ok=True)

        import urllib.request
        urllib.request.urlretrieve(onnx_url, str(onnx_path))
        urllib.request.urlretrieve(json_url, str(json_path))

        log.info("Piper model '%s' downloaded to %s", model, onnx_path)
        return str(onnx_path)

    except Exception:
        log.exception("Failed to download Piper model '%s'", model)
        # Clean up partial downloads
        onnx_path.unlink(missing_ok=True)
        json_path.unlink(missing_ok=True)
        return str(onnx_path)


def generate_thinking_sound(
    tone1_hz: float = 130,
    tone2_hz: float = 130,
    tone_mix: float = 0.7,
    pulse_hz: float = 0.3,
    volume: float = 0.2,
    duration: float = 2.5,
    sample_rate: int = 48000,
) -> bytes:
    """Generate a subtle thinking/processing sound as WAV bytes.

    Creates a soft, repeating tonal pulse — gentle enough to signal
    "I'm working on it" without being annoying on loop.

    The clip is snapped to an exact whole number of pulse cycles so the
    waveform loops seamlessly.  The envelope uses ``(1 - cos)`` so
    amplitude is zero at both ends of each cycle, eliminating clicks at
    the loop boundary.

    Args:
        tone1_hz: Primary tone frequency in Hz (default 130 = C3)
        tone2_hz: Secondary tone frequency in Hz (default 130 = C3)
        tone_mix: Mix ratio for tone1 vs tone2 (0.0–1.0, default 0.7 = 70% tone1)
        pulse_hz: How fast the volume pulses (Hz, default 0.3 = fades in/out ~every 3s)
        volume: Overall volume (0.0–1.0, default 0.2 = 20%)
        duration: Approximate length of the WAV clip in seconds (snapped
            to the nearest whole pulse cycle for seamless looping)
        sample_rate: Audio sample rate
    """
    import math

    # Snap duration to a whole number of pulse cycles so the loop is
    # seamless.  E.g. pulse_hz=0.3 → period=3.333s, duration=2.5 snaps
    # to 3.333s (one cycle).
    if pulse_hz > 0:
        pulse_period = 1.0 / pulse_hz
        n_cycles = max(1, round(duration / pulse_period))
        duration = n_cycles * pulse_period

    num_samples = int(sample_rate * duration)
    samples = []

    tone2_mix = 1.0 - tone_mix

    for i in range(num_samples):
        t = i / sample_rate

        # Pulse envelope: ``(1 - cos)`` shape so amplitude is zero at
        # t=0 and t=duration (the loop boundary).  Each pulse fades in
        # smoothly, peaks at the midpoint, and fades back to silence.
        pulse = 0.5 * (1.0 - math.cos(2 * math.pi * pulse_hz * t))

        # Two sine tones for a warm timbre
        t1 = math.sin(2 * math.pi * tone1_hz * t)
        t2 = math.sin(2 * math.pi * tone2_hz * t)

        # Mix tones and apply pulse envelope
        sample = (tone_mix * t1 + tone2_mix * t2) * pulse * volume

        # Convert to 16-bit PCM
        sample_int = max(-32768, min(32767, int(sample * 32767)))
        samples.append(struct.pack("<h", sample_int))

    pcm_data = b"".join(samples)
    wav_buffer = io.BytesIO()
    with wave.open(wav_buffer, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_data)
    return wav_buffer.getvalue()


class TextToSpeech:
    """Synthesizes speech from text using either ElevenLabs or local Piper TTS.

    Supports per-user voice overrides: callers can pass ``provider``,
    ``elevenlabs_voice_id``, or ``local_model`` to ``synthesize()`` to
    override the config defaults on a per-request basis.
    """

    def __init__(self, config: TTSConfig) -> None:
        self.config = config
        self._elevenlabs_client = None
        # Cache of resolved Piper model paths: model_name -> absolute path
        self._piper_model_cache: dict[str, str] = {}
        self._strip_silence = config.strip_leading_silence

    async def warm_up(self) -> None:
        """Pre-resolve the TTS model so the first synthesis isn't delayed."""
        if self.config.provider == "elevenlabs":
            await self._ensure_elevenlabs_client()
        else:
            loop = asyncio.get_running_loop()
            resolved = await loop.run_in_executor(
                None, _resolve_piper_model, self.config.local_model
            )
            self._piper_model_cache[self.config.local_model] = resolved
            log.debug("Piper model pre-resolved: %s", resolved)

    async def _ensure_elevenlabs_client(self) -> bool:
        """Lazily initialize the ElevenLabs client. Returns True on success."""
        if self._elevenlabs_client is not None:
            return True
        try:
            from elevenlabs import AsyncElevenLabs
            self._elevenlabs_client = AsyncElevenLabs(
                api_key=self.config.elevenlabs_api_key
            )
            log.debug("ElevenLabs client initialized")
            return True
        except ImportError:
            log.warning("elevenlabs package not installed")
            return False

    async def synthesize(
        self,
        text: str,
        *,
        provider: str | None = None,
        elevenlabs_voice_id: str | None = None,
        local_model: str | None = None,
    ) -> bytes | None:
        """Convert text to WAV audio bytes.

        Args:
            text: Text to synthesize.
            provider: Override TTS provider ('local' or 'elevenlabs').
                Falls back to config default if None.
            elevenlabs_voice_id: Override ElevenLabs voice ID for this request.
            local_model: Override Piper model name for this request.

        Returns:
            WAV audio bytes suitable for FFmpeg playback, or None on failure.
        """
        if not text:
            log.debug("TTS received empty text, returning None")
            return None

        # Strip markdown and emoji so TTS reads naturally
        original_len = len(text)
        text = _clean_for_tts(text)
        if not text:
            log.debug("TTS text empty after cleaning (was %d chars)", original_len)
            return None
        log.debug(
            "TTS cleaned text (%d -> %d chars): %r",
            original_len, len(text), text[:300],
        )

        effective_provider = provider or self.config.provider

        try:
            t0 = time.monotonic()
            if effective_provider == "elevenlabs":
                voice_id = elevenlabs_voice_id or self.config.elevenlabs_voice_id
                result = await self._synthesize_elevenlabs(text, voice_id=voice_id)
            else:
                model = local_model or self.config.local_model
                result = await self._synthesize_local(text, model=model)
            elapsed = time.monotonic() - t0
            if result and self._strip_silence:
                result = _strip_leading_silence(result)
            if result:
                log.debug(
                    "TTS synthesis complete: provider=%s, %d bytes, %.3fs",
                    effective_provider, len(result), elapsed,
                )
            else:
                log.warning(
                    "TTS synthesis returned no audio: provider=%s, %.3fs, text=%r",
                    effective_provider, elapsed, text[:300],
                )
            return result
        except Exception:
            log.exception("TTS synthesis failed")
            return None

    async def _synthesize_elevenlabs(
        self, text: str, *, voice_id: str | None = None
    ) -> bytes | None:
        """Synthesize using ElevenLabs API."""
        if not await self._ensure_elevenlabs_client():
            log.error(
                "elevenlabs package not installed. "
                "Install with: pip install elevenlabs"
            )
            return None

        effective_voice = voice_id or self.config.elevenlabs_voice_id
        log.debug(
            "ElevenLabs request: voice=%s, text_len=%d",
            effective_voice, len(text),
        )
        audio_stream = await self._elevenlabs_client.text_to_speech.convert(
            voice_id=effective_voice,
            text=text,
            model_id="eleven_turbo_v2_5",
            output_format="pcm_16000",
        )

        # Collect all chunks
        pcm_data = b""
        async for chunk in audio_stream:
            pcm_data += chunk

        log.debug("ElevenLabs returned %d bytes of PCM", len(pcm_data))
        # Wrap raw PCM in WAV container for FFmpeg
        return self._pcm_to_wav(pcm_data, sample_rate=16000, channels=1)

    async def _synthesize_local(
        self, text: str, *, model: str | None = None
    ) -> bytes | None:
        """Synthesize using local Piper TTS."""
        effective_model = model or self.config.local_model
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._synthesize_piper_sync, text, effective_model
        )

    def _synthesize_piper_sync(
        self, text: str, model_name: str | None = None
    ) -> bytes | None:
        """Synchronous Piper TTS synthesis via CLI subprocess.

        Uses the piper CLI (installed by piper-tts) rather than the Python API,
        which has version-specific bugs with wave header writing.
        """
        import subprocess

        effective_model = model_name or self.config.local_model
        # Resolve model name to path (only cache if the file actually exists)
        if effective_model in self._piper_model_cache:
            model_path = self._piper_model_cache[effective_model]
        else:
            model_path = _resolve_piper_model(effective_model)
            if os.path.isfile(model_path):
                self._piper_model_cache[effective_model] = model_path
            else:
                log.warning(
                    "Piper model '%s' resolved to non-existent path: %s "
                    "(not caching — will retry next time)",
                    effective_model,
                    model_path,
                )

        log.debug("Piper TTS: model=%s, text_len=%d", model_path, len(text))

        try:
            t0 = time.monotonic()
            result = subprocess.run(
                ["piper", "--model", model_path, "--output_file", "-"],
                input=text.encode("utf-8"),
                capture_output=True,
                timeout=60,
            )
            elapsed = time.monotonic() - t0
            if result.returncode == 0 and len(result.stdout) > 44:
                log.debug(
                    "Piper produced %d bytes in %.3fs (rc=0)",
                    len(result.stdout), elapsed,
                )
                return result.stdout  # WAV format
            stderr_text = result.stderr.decode(errors="replace")
            log.warning(
                "Piper produced no audio (rc=%d, %.3fs, stderr=%s)",
                result.returncode, elapsed, stderr_text[:500],
            )
            if result.returncode != 0:
                log.debug("Full Piper stderr: %s", stderr_text)
        except FileNotFoundError:
            log.warning("piper CLI not found, falling back to espeak-ng")
        except subprocess.TimeoutExpired:
            log.warning("Piper TTS timed out after 60s")

        return self._synthesize_espeak_fallback(text)

    def _synthesize_espeak_fallback(self, text: str) -> bytes | None:
        """Ultimate fallback: use espeak via subprocess."""
        import subprocess

        log.warning(
            "Falling back to espeak-ng (Piper unavailable), "
            "output will sound robotic — text=%r",
            text[:200],
        )
        try:
            result = subprocess.run(
                ["espeak-ng", "--stdout", "-s", "150", text],
                capture_output=True,
                timeout=30,
            )
            if result.returncode == 0 and result.stdout:
                log.warning(
                    "espeak-ng fallback produced %d bytes "
                    "(check Piper model configuration)",
                    len(result.stdout),
                )
                return result.stdout
            log.warning(
                "espeak-ng fallback failed (rc=%d)", result.returncode
            )
        except FileNotFoundError:
            log.warning("espeak-ng not installed — no TTS fallback available")
        except subprocess.TimeoutExpired:
            log.warning("espeak-ng fallback timed out after 30s")
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
