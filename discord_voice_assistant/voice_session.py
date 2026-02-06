"""Represents a single voice conversation session in a channel."""

from __future__ import annotations

import asyncio
import io
import logging
import time
from typing import TYPE_CHECKING

import discord

from discord_voice_assistant.audio.sink import StreamingSink
from discord_voice_assistant.audio.stt import SpeechToText
from discord_voice_assistant.audio.tts import TextToSpeech
from discord_voice_assistant.audio.wake_word import WakeWordDetector
from discord_voice_assistant.audio.voice_id import VoiceIdentifier
from discord_voice_assistant.integrations.openclaw import OpenClawClient

if TYPE_CHECKING:
    from discord_voice_assistant.bot import ClippyBot
    from discord_voice_assistant.config import Config

log = logging.getLogger(__name__)


class VoiceSession:
    """A single voice conversation in a channel, processing audio in real-time."""

    def __init__(
        self, bot: ClippyBot, config: Config, channel: discord.VoiceChannel
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
        self._sink = StreamingSink(self._on_audio_chunk)
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
            if self.voice_client.recording:
                self.voice_client.stop_recording()
            if self.voice_client.is_connected():
                await self.voice_client.disconnect()
            self.voice_client = None

        if self._openclaw and self._session_id:
            await self._openclaw.end_session(self._session_id)

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

    def _on_recording_stop(self, sink: discord.sinks.Sink, *args) -> None:
        """Called when recording stops."""
        log.debug("Recording stopped")

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

            # Send to OpenClaw and get response
            response = await self._openclaw.send_message(
                self._session_id,
                text,
                sender_name=speaker_name,
                sender_id=str(user_id),
            )

            if response:
                log.info("[Clippy] %s", response[:100])
                await self._speak(response)

    async def _speak(self, text: str) -> None:
        """Convert text to speech and play it in the voice channel."""
        if not self.voice_client or not self.voice_client.is_connected():
            return

        audio_bytes = await self._tts.synthesize(text)
        if not audio_bytes:
            return

        # Wait for any current playback to finish
        while self.voice_client.is_playing():
            await asyncio.sleep(0.1)

        # Play the audio
        source = discord.FFmpegPCMAudio(
            io.BytesIO(audio_bytes), pipe=True, options="-f wav"
        )
        self.voice_client.play(source)
