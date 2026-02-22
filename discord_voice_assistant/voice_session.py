"""Represents a single voice conversation session in a channel."""

from __future__ import annotations

import asyncio
import base64
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
from discord_voice_assistant.integrations.openclaw import OpenClawClient

if TYPE_CHECKING:
    from discord_voice_assistant.bot import VoiceAssistantBot
    from discord_voice_assistant.config import Config
    from discord_voice_assistant.voice_bridge import VoiceBridgeClient

log = logging.getLogger(__name__)


class _BridgeVoiceProtocol(discord.VoiceProtocol):
    """Minimal VoiceProtocol that captures voice credentials from the gateway.

    When discord.py calls channel.connect(), it sends opcode 4 to the Discord
    gateway and receives voice_server_update and voice_state_update events.
    This protocol captures those credentials so we can forward them to the
    Node.js voice bridge.
    """

    def __init__(self, client: discord.Client, channel: discord.abc.Connectable) -> None:
        super().__init__(client, channel)
        self.voice_data: dict = {}
        self._connected = False

    async def on_voice_server_update(self, data: dict) -> None:
        self.voice_data["voice_server"] = data
        log.debug("Captured voice_server_update: endpoint=%s", data.get("endpoint"))
        self._connected = True

    async def on_voice_state_update(self, data: dict) -> None:
        self.voice_data["voice_state"] = data
        self.voice_data["session_id"] = data.get("session_id", "")
        log.debug("Captured voice_state_update: session=%s", data.get("session_id"))

    async def connect(self, *, timeout: float, reconnect: bool, **kwargs) -> None:
        for _ in range(int(timeout * 10)):
            if self.voice_data.get("voice_server") and self.voice_data.get("voice_state"):
                return
            await asyncio.sleep(0.1)
        if not self.voice_data.get("voice_server"):
            raise asyncio.TimeoutError("Timed out waiting for voice server data")

    def is_connected(self) -> bool:
        return self._connected

    async def disconnect(self, *, force: bool = False) -> None:
        self._connected = False
        await self.channel.guild.change_voice_state(channel=None)


class VoiceSession:
    """A single voice conversation in a channel, processing audio in real-time.

    Uses the Node.js voice bridge for Discord voice I/O with DAVE E2EE.
    The bridge handles voice connections, Opus encode/decode, and DAVE
    encryption/decryption. This class manages the audio pipeline
    (STT -> LLM -> TTS) and communicates with the bridge over WebSocket.
    """

    def __init__(
        self,
        bot: VoiceAssistantBot,
        config: Config,
        channel: discord.VoiceChannel,
        bridge: VoiceBridgeClient,
    ) -> None:
        self.bot = bot
        self.config = config
        self.channel = channel
        self.guild = channel.guild
        self.bridge = bridge
        self._voice_client: _BridgeVoiceProtocol | None = None
        self.is_active = False

        self._stt: SpeechToText | None = None
        self._tts: TextToSpeech | None = None
        self._wake_word: WakeWordDetector | None = None
        self._openclaw: OpenClawClient | None = None
        self._sink: StreamingSink | None = None
        self._processing_lock = asyncio.Lock()
        self._session_id: str | None = None
        self._start_time: float = 0
        self._thinking_sound: bytes | None = None
        self._thinking_temp_path: str | None = None
        self._is_playing: bool = False
        self._guild_id_str: str = str(channel.guild.id)

    @property
    def voice_client(self):
        """Compatibility property for voice_manager checks."""
        return self._voice_client

    async def start(self) -> None:
        """Connect to the voice channel via the bridge and begin listening."""
        guild_id = self._guild_id_str
        channel_id = str(self.channel.id)
        user_id = str(self.bot.user.id)

        try:
            # Connect via discord.py to get voice credentials from the gateway
            self._voice_client = await self.channel.connect(cls=_BridgeVoiceProtocol)
            voice_data = self._voice_client.voice_data

            # Tell the bridge to set up the voice connection
            await self.bridge.join(
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
                session_id=voice_data.get("session_id", ""),
            )

            # Forward the voice credentials to the bridge
            if voice_data.get("voice_state"):
                await self.bridge.send_voice_state_update(voice_data["voice_state"])
            if voice_data.get("voice_server"):
                await self.bridge.send_voice_server_update(voice_data["voice_server"])

            # Wait for the bridge to establish the voice connection
            ready = await self.bridge.wait_ready(guild_id, timeout=15.0)
            if not ready:
                log.error("Voice bridge failed to connect for guild %s", guild_id)
                raise RuntimeError("Voice bridge connection timeout")

        except discord.ClientException:
            if self.guild.voice_client:
                await self.guild.voice_client.move_to(self.channel)
                self._voice_client = self.guild.voice_client
            else:
                raise

        self.is_active = True
        self._start_time = time.monotonic()

        # Initialize pipeline components
        self._stt = SpeechToText(self.config.stt)
        self._tts = TextToSpeech(self.config.tts)
        if self.config.wake_word.enabled:
            self._wake_word = WakeWordDetector(self.config.wake_word)
            log.info("Wake word detection ENABLED")
        else:
            log.info("Wake word detection DISABLED")
        self._openclaw = OpenClawClient(self.config.openclaw)

        self._session_id = await self._openclaw.create_session(
            context=f"discord:voice:{self.guild.id}:{self.channel.id}"
        )
        await self._openclaw.reset_session(self._session_id)

        # Pre-warm models concurrently
        warmup_start = time.monotonic()
        warmup_tasks = [
            self._stt.warm_up(),
            self._tts.warm_up(),
            self._ensure_thinking_sound(),
        ]
        if self._wake_word:
            loop = asyncio.get_running_loop()
            warmup_tasks.append(loop.run_in_executor(None, self._wake_word.warm_up))
        await asyncio.gather(*warmup_tasks, return_exceptions=True)
        log.info("Pipeline warm-up completed in %.3fs", time.monotonic() - warmup_start)

        # Register audio callback with the bridge
        self._sink = StreamingSink(self._on_audio_chunk, asyncio.get_running_loop())
        self.bridge.register_audio_callback(guild_id, self._on_bridge_audio)

        log.info(
            "Voice session started in %s/%s (session: %s, DAVE: %s)",
            self.guild.name,
            self.channel.name,
            self._session_id,
            self.bridge.is_dave_active(guild_id),
        )

    async def _on_bridge_audio(self, user_id: int, pcm: bytes, guild_id: str) -> None:
        """Called when the bridge sends decoded audio from a user."""
        if not self.is_active or not self._sink:
            return
        self._sink.write(user_id, pcm)

    async def stop(self) -> None:
        """Disconnect and clean up the session."""
        self.is_active = False
        guild_id = self._guild_id_str

        self.bridge.unregister_audio_callback(guild_id)

        if self._sink and self._sink._pipeline_tasks:
            pending = [t for t in self._sink._pipeline_tasks if not t.done()]
            if pending:
                log.debug(
                    "Waiting up to 2s for %d in-progress pipeline task(s)", len(pending),
                )
                await asyncio.wait(pending, timeout=2.0)

        try:
            await self.bridge.disconnect(guild_id)
        except Exception:
            log.debug("Error telling bridge to disconnect (expected during cleanup)")

        if self._voice_client:
            try:
                if self._voice_client.is_connected():
                    await self._voice_client.disconnect(force=True)
            except Exception:
                log.debug("Error disconnecting voice client (expected during cleanup)")
            self._voice_client = None

        if self._openclaw and self._session_id:
            await self._openclaw.end_session(self._session_id)

        if self._sink:
            self._sink.cleanup()

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
        if self._voice_client and self._voice_client.is_connected():
            await self._voice_client.move_to(channel)
            self.channel = channel
            log.info("Moved to %s/%s", self.guild.name, channel.name)

    async def _ensure_thinking_sound(self) -> str | None:
        """Generate thinking sound WAV and write to temp file, return path."""
        if self._thinking_temp_path and os.path.exists(self._thinking_temp_path):
            return self._thinking_temp_path

        if self._thinking_sound is None:
            ts = self.config.thinking_sound
            loop = asyncio.get_running_loop()
            self._thinking_sound = await loop.run_in_executor(
                None,
                lambda: generate_thinking_sound(
                    tone1_hz=ts.tone1_hz,
                    tone2_hz=ts.tone2_hz,
                    tone_mix=ts.tone_mix,
                    pulse_hz=ts.pulse_hz,
                    volume=ts.volume,
                    duration=ts.duration,
                ),
            )

        fd, path = tempfile.mkstemp(suffix=".wav")
        try:
            os.write(fd, self._thinking_sound)
        finally:
            os.close(fd)
        self._thinking_temp_path = path
        return path

    async def _start_thinking_sound(self) -> None:
        """Play a subtle thinking sound while waiting for AI response."""
        if self._thinking_sound is None:
            await self._ensure_thinking_sound()
        if self._thinking_sound:
            try:
                self._is_playing = True
                await self.bridge.send({
                    "op": "play",
                    "guild_id": self._guild_id_str,
                    "audio": base64.b64encode(self._thinking_sound).decode("ascii"),
                    "format": "wav",
                })
                log.debug("Thinking sound started via bridge")
            except Exception:
                log.debug("Failed to play thinking sound", exc_info=True)

    async def _stop_thinking_sound(self) -> None:
        """Stop the thinking sound if it's currently playing."""
        if self._is_playing:
            try:
                await self.bridge.stop_playing(self._guild_id_str)
                self._is_playing = False
                await asyncio.sleep(0.1)
                log.debug("Thinking sound stopped")
            except Exception:
                log.debug("Error stopping thinking sound", exc_info=True)

    async def _on_audio_chunk(
        self, user_id: int, audio_data: bytes, sample_rate: int
    ) -> None:
        """Process an audio chunk from a specific user.

        Core audio pipeline: authorization -> wake word -> STT -> LLM -> TTS
        """
        pipeline_start = time.monotonic()
        audio_duration = len(audio_data) / (sample_rate * 2)

        if not self.is_active:
            return

        log.debug(
            "Pipeline START for user %d: %d bytes (%.2fs audio at %dHz)",
            user_id, len(audio_data), audio_duration, sample_rate,
        )

        is_authorized = self.bot.voice_manager.is_authorized(user_id)

        if not is_authorized and self.config.auth.require_wake_word_for_unauthorized:
            if not self._wake_word:
                return
            if not self._wake_word.detect(audio_data, sample_rate):
                return

        if is_authorized and self._wake_word and len(self.channel.members) > 2:
            if not self._wake_word.detect(audio_data, sample_rate):
                return

        self.bot.voice_manager.notify_activity(self.guild.id)

        async with self._processing_lock:
            stt_start = time.monotonic()
            text = await self._stt.transcribe(audio_data, sample_rate)
            stt_elapsed = time.monotonic() - stt_start

            if not text or len(text.strip()) < 2:
                return

            log.debug("STT transcribed in %.3fs: %r", stt_elapsed, text)

            member = self.guild.get_member(user_id)
            speaker_name = member.display_name if member else f"User#{user_id}"
            log.info("[%s] %s", speaker_name, text)

            await self._start_thinking_sound()

            llm_start = time.monotonic()
            try:
                response = await self._openclaw.send_message(
                    self._session_id,
                    text,
                    sender_name=speaker_name,
                    sender_id=str(user_id),
                )
            finally:
                await self._stop_thinking_sound()
            llm_elapsed = time.monotonic() - llm_start

            if not response:
                log.warning("OpenClaw returned empty response for %r (%.3fs)", text[:80], llm_elapsed)
                return

            log.info("[Assistant] %s", response)

            if not self.is_active:
                return

            tts_start = time.monotonic()
            await self._speak(response)
            tts_elapsed = time.monotonic() - tts_start

            total_elapsed = time.monotonic() - pipeline_start
            log.debug(
                "Pipeline END for user %d: stt=%.3fs llm=%.3fs tts=%.3fs total=%.3fs",
                user_id, stt_elapsed, llm_elapsed, tts_elapsed, total_elapsed,
            )

    async def _speak(self, text: str) -> None:
        """Convert text to speech and play it via the voice bridge."""
        if not self.is_active:
            return

        synth_start = time.monotonic()
        audio_bytes = await self._tts.synthesize(text)
        if not audio_bytes:
            log.warning("TTS returned no audio for: %s", text[:80])
            return

        log.debug(
            "TTS produced %d bytes in %.3fs", len(audio_bytes), time.monotonic() - synth_start,
        )

        if not self.is_active:
            return

        try:
            await self.bridge.play(
                guild_id=self._guild_id_str,
                audio_bytes=audio_bytes,
                fmt="wav",
                timeout=120.0,
            )
        except Exception:
            log.exception("Failed to play audio via bridge")
