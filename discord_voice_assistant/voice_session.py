"""Represents a single voice conversation session in a channel."""

from __future__ import annotations

import asyncio
import io
import logging
import os
import tempfile
import time
from typing import TYPE_CHECKING

import discord

from discord_voice_assistant.audio.sink import StreamingSink
from discord_voice_assistant.audio.stt import SpeechToText
from discord_voice_assistant.audio.tts import TextToSpeech, generate_thinking_sound
from discord_voice_assistant.audio.wake_word import WakeWordDetector
from discord_voice_assistant.audio.voice_id import VoiceIdentifier
from discord_voice_assistant.integrations.openclaw import OpenClawClient

if TYPE_CHECKING:
    from discord_voice_assistant.bot import VoiceAssistantBot
    from discord_voice_assistant.config import Config

log = logging.getLogger(__name__)


class VoiceSession:
    """A single voice conversation in a channel, processing audio in real-time."""

    def __init__(
        self, bot: VoiceAssistantBot, config: Config, channel: discord.VoiceChannel
    ) -> None:
        self.bot = bot
        self.config = config
        self.channel = channel
        self.guild = channel.guild
        self.voice_client: discord.VoiceClient | None = None
        self.is_active = False

        # Audio pipeline components (lazy init)
        self._stt: SpeechToText | None = None
        self._tts: TextToSpeech | None = None
        self._wake_word: WakeWordDetector | None = None
        self._voice_id: VoiceIdentifier | None = None
        self._openclaw: OpenClawClient | None = None
        self._sink: StreamingSink | None = None
        self._processing_lock = asyncio.Lock()
        self._session_id: str | None = None
        self._start_time: float = 0
        self._thinking_sound: bytes | None = None  # lazy-generated
        self._thinking_temp_path: str | None = None  # temp file for FFmpeg looping

    async def start(self) -> None:
        """Connect to the voice channel and begin listening."""
        try:
            self.voice_client = await self.channel.connect(cls=discord.VoiceClient)
        except discord.ClientException:
            # Already connected, try to move
            if self.guild.voice_client:
                await self.guild.voice_client.move_to(self.channel)
                self.voice_client = self.guild.voice_client
            else:
                raise

        self.is_active = True
        self._start_time = time.monotonic()

        # Initialize pipeline components
        self._stt = SpeechToText(self.config.stt)
        self._tts = TextToSpeech(self.config.tts)
        if self.config.wake_word.enabled:
            self._wake_word = WakeWordDetector(self.config.wake_word)
        self._voice_id = VoiceIdentifier(self.config.data_dir / "voice_profiles")
        self._openclaw = OpenClawClient(self.config.openclaw)

        # Start a new OpenClaw session
        self._session_id = await self._openclaw.create_session(
            context=f"discord:voice:{self.guild.id}:{self.channel.id}"
        )

        # Start recording with our streaming sink
        self._sink = StreamingSink(self._on_audio_chunk, asyncio.get_running_loop())
        self.voice_client.start_recording(self._sink, self._on_recording_stop)

        log.info(
            "Voice session started in %s/%s (session: %s)",
            self.guild.name,
            self.channel.name,
            self._session_id,
        )

    async def stop(self) -> None:
        """Disconnect and clean up the session."""
        self.is_active = False

        if self.voice_client:
            try:
                if self.voice_client.recording:
                    self.voice_client.stop_recording()
            except Exception:
                log.debug("Error stopping recording (expected during cleanup)")
            try:
                if self.voice_client.is_connected():
                    await self.voice_client.disconnect(force=True)
            except Exception:
                log.debug("Error disconnecting voice client (expected during cleanup)")
            self.voice_client = None

        if self._openclaw and self._session_id:
            await self._openclaw.end_session(self._session_id)

        # Clean up thinking sound temp file
        if self._thinking_temp_path:
            try:
                os.unlink(self._thinking_temp_path)
            except OSError:
                pass
            self._thinking_temp_path = None

        duration = time.monotonic() - self._start_time if self._start_time else 0
        log.info(
            "Voice session ended in %s/%s (duration: %.0fs)",
            self.guild.name,
            self.channel.name,
            duration,
        )

    async def move_to(self, channel: discord.VoiceChannel) -> None:
        """Move to a different voice channel within the same guild."""
        if self.voice_client and self.voice_client.is_connected():
            await self.voice_client.move_to(channel)
            self.channel = channel
            log.info("Moved to %s/%s", self.guild.name, channel.name)

    async def _on_recording_stop(self, sink: discord.sinks.Sink, *args) -> None:
        """Called when recording stops (py-cord requires this to be async)."""
        log.debug("Recording stopped")

    async def _ensure_thinking_sound(self) -> str | None:
        """Generate thinking sound WAV and write to temp file, return path.

        The temp file is reused across calls (FFmpeg reads it each time).
        Uses ``-stream_loop -1`` so FFmpeg loops the short clip indefinitely.
        """
        if self._thinking_temp_path and os.path.exists(self._thinking_temp_path):
            return self._thinking_temp_path

        # Generate 2-second thinking sound on first use
        if self._thinking_sound is None:
            loop = asyncio.get_running_loop()
            self._thinking_sound = await loop.run_in_executor(
                None, generate_thinking_sound
            )

        # Write to temp file so FFmpeg can seek/loop it (pipe doesn't support seeking)
        fd, path = tempfile.mkstemp(suffix=".wav")
        try:
            os.write(fd, self._thinking_sound)
        finally:
            os.close(fd)
        self._thinking_temp_path = path
        return path

    async def _start_thinking_sound(self) -> None:
        """Play a subtle looping thinking sound while waiting for AI response."""
        if not self.voice_client or not self.voice_client.is_connected():
            return

        path = await self._ensure_thinking_sound()
        if not path:
            return

        try:
            source = discord.FFmpegPCMAudio(
                path, before_options="-stream_loop -1"
            )
            self.voice_client.play(source)
            log.debug("Thinking sound started")
        except Exception:
            log.debug("Failed to play thinking sound", exc_info=True)

    async def _stop_thinking_sound(self) -> None:
        """Stop the thinking sound if it's currently playing."""
        if not self.voice_client:
            return
        try:
            if self.voice_client.is_playing():
                self.voice_client.stop()
                # Brief pause for FFmpeg subprocess cleanup
                await asyncio.sleep(0.05)
                log.debug("Thinking sound stopped")
        except Exception:
            log.debug("Error stopping thinking sound", exc_info=True)

    async def _on_audio_chunk(
        self, user_id: int, audio_data: bytes, sample_rate: int
    ) -> None:
        """Process an audio chunk from a specific user.

        This is the core audio pipeline:
        1. Check if user is authorized (or wake word detected)
        2. Run wake word detection if needed
        3. Transcribe speech
        4. Send to OpenClaw
        5. Synthesize and play response
        """
        if not self.is_active:
            return

        is_authorized = self.bot.voice_manager.is_authorized(user_id)

        # For unauthorized users, require wake word
        if not is_authorized and self.config.auth.require_wake_word_for_unauthorized:
            if not self._wake_word:
                return
            if not self._wake_word.detect(audio_data, sample_rate):
                return

        # For authorized users, still check wake word if enabled and in multi-user channel
        if is_authorized and self._wake_word and len(self.channel.members) > 2:
            if not self._wake_word.detect(audio_data, sample_rate):
                return

        # Notify activity to reset inactivity timer
        self.bot.voice_manager.notify_activity(self.guild.id)

        # Transcribe
        async with self._processing_lock:
            text = await self._stt.transcribe(audio_data, sample_rate)
            if not text or len(text.strip()) < 2:
                return

            # Optional: verify voice identity
            member = self.guild.get_member(user_id)
            speaker_name = member.display_name if member else f"User#{user_id}"
            if self._voice_id and member:
                verified = await self._voice_id.verify(
                    user_id, audio_data, sample_rate
                )
                if verified is False:
                    log.warning(
                        "Voice verification failed for %s in %s",
                        speaker_name,
                        self.channel.name,
                    )

            log.info("[%s] %s", speaker_name, text)

            # Play thinking sound while waiting for AI response
            await self._start_thinking_sound()

            # Send to OpenClaw and get response
            try:
                response = await self._openclaw.send_message(
                    self._session_id,
                    text,
                    sender_name=speaker_name,
                    sender_id=str(user_id),
                )
            finally:
                await self._stop_thinking_sound()

            if response:
                log.info("[Assistant] %s", response)
                await self._speak(response)

    async def _speak(self, text: str) -> None:
        """Convert text to speech and play it in the voice channel."""
        if not self.voice_client or not self.voice_client.is_connected():
            log.warning("Cannot speak — voice client not connected")
            return

        audio_bytes = await self._tts.synthesize(text)
        if not audio_bytes:
            log.warning("TTS returned no audio for: %s", text[:80])
            return

        log.debug("TTS produced %d bytes of audio", len(audio_bytes))

        # Wait for any current playback to finish (with timeout to avoid hanging)
        try:
            wait_start = time.monotonic()
            while self.voice_client.is_playing():
                if time.monotonic() - wait_start > 120:
                    log.warning("Playback wait timed out after 120s, stopping current audio")
                    self.voice_client.stop()
                    break
                await asyncio.sleep(0.1)
        except Exception:
            log.debug("Error waiting for playback to finish")

        if not self.voice_client or not self.voice_client.is_connected():
            log.warning("Voice client disconnected while waiting for playback")
            return

        # Play the audio
        # before_options tells FFmpeg the INPUT is WAV format
        # (options would override the OUTPUT format that py-cord needs as s16le PCM)
        try:
            source = discord.FFmpegPCMAudio(
                io.BytesIO(audio_bytes), pipe=True, before_options="-f wav"
            )

            play_done = asyncio.Event()

            def after_play(error):
                if error:
                    log.error("Playback error: %s", error)
                play_done.set()

            self.voice_client.play(source, after=after_play)
            log.debug("Playback started")

            # Wait for playback to complete before releasing
            try:
                await asyncio.wait_for(play_done.wait(), timeout=120)
            except asyncio.TimeoutError:
                log.warning("Playback timed out after 120s")
                self.voice_client.stop()
        except Exception:
            log.exception("Failed to play audio")
