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
        self._voice_server_event = asyncio.Event()
        self._voice_state_event = asyncio.Event()

    async def on_voice_server_update(self, data: dict) -> None:
        self.voice_data["voice_server"] = data
        log.debug("Captured voice_server_update: endpoint=%s", data.get("endpoint"))
        self._connected = True
        self._voice_server_event.set()

    async def on_voice_state_update(self, data: dict) -> None:
        self.voice_data["voice_state"] = data
        self.voice_data["session_id"] = data.get("session_id", "")
        log.debug("Captured voice_state_update: session=%s", data.get("session_id"))
        self._voice_state_event.set()

    async def connect(self, *, timeout: float, reconnect: bool, **kwargs) -> None:
        # Send Gateway OP 4 to tell Discord we want to join this voice channel.
        # This triggers VOICE_STATE_UPDATE and VOICE_SERVER_UPDATE events back
        # from the gateway, which discord.py routes to our on_voice_* methods.
        # Without this, Discord never knows we're joining and the events never arrive.
        await self.channel.guild.change_voice_state(
            channel=self.channel,
            self_deaf=kwargs.get('self_deaf', False),
            self_mute=kwargs.get('self_mute', False),
        )
        try:
            async with asyncio.timeout(timeout):
                await self._voice_server_event.wait()
                await self._voice_state_event.wait()
        except TimeoutError:
            raise asyncio.TimeoutError("Timed out waiting for voice server data")

    def is_connected(self) -> bool:
        return self._connected

    async def move_to(self, channel: discord.abc.Connectable) -> None:
        """Move to a different voice channel by sending a new OP 4."""
        self.channel = channel
        await channel.guild.change_voice_state(channel=channel)
        # Reset events so callers can await fresh voice data if needed
        self._voice_server_event.clear()
        self._voice_state_event.clear()

    async def disconnect(self, *, force: bool = False) -> None:
        self._connected = False
        try:
            await self.channel.guild.change_voice_state(channel=None)
        except Exception:
            pass
        # Tell discord.py to deregister this voice client from the guild.
        # Without this, guild.voice_client remains set and subsequent
        # channel.connect() calls fail with "Already connected".
        self.cleanup()


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
        shared_stt: SpeechToText | None = None,
    ) -> None:
        self.bot = bot
        self.config = config
        self.channel = channel
        self.guild = channel.guild
        self.bridge = bridge
        self._voice_client: _BridgeVoiceProtocol | None = None
        self.is_active = False

        # Use shared (preloaded) STT instance if provided, otherwise create per-session
        self._stt: SpeechToText | None = shared_stt
        self._owns_stt = shared_stt is None
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

    @property
    def session_id(self) -> str | None:
        """The OpenClaw session ID for this voice session."""
        return self._session_id

    @property
    def start_time(self) -> float:
        """Monotonic timestamp when the session started."""
        return self._start_time

    async def start(self) -> None:
        """Connect to the voice channel via the bridge and begin listening.

        The pipeline is warmed up BEFORE joining the voice channel so the bot
        only appears in the channel once it is ready to receive audio.  This
        avoids a confusing window where the bot is visible but deaf.
        """
        guild_id = self._guild_id_str
        channel_id = str(self.channel.id)
        user_id = str(self.bot.user.id)

        if not self.bridge.is_connected:
            raise RuntimeError(
                "Voice bridge is not connected. Cannot join voice channel."
            )

        # --- Phase 1: Initialize and warm up the pipeline (bot is NOT in channel yet) ---
        if self._stt is None:
            self._stt = SpeechToText(self.config.stt)
            self._owns_stt = True
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
        # Compact (summarize) the existing context rather than wiping it.
        # Users can explicitly /new or /compact via slash commands.
        await self._openclaw.compact_session(self._session_id)

        warmup_start = time.monotonic()
        warmup_tasks = [
            self._tts.warm_up(),
            self._ensure_thinking_sound(),
        ]
        # Only warm up STT if this session owns it (not preloaded)
        if self._owns_stt:
            warmup_tasks.append(self._stt.warm_up())
        if self._wake_word:
            loop = asyncio.get_running_loop()
            warmup_tasks.append(loop.run_in_executor(None, self._wake_word.warm_up))
        await asyncio.gather(*warmup_tasks, return_exceptions=True)
        log.info("Pipeline warm-up completed in %.3fs", time.monotonic() - warmup_start)

        # --- Phase 2: Join the voice channel (pipeline is ready) ---
        try:
            self._voice_client = await self.channel.connect(cls=_BridgeVoiceProtocol)
        except discord.ClientException:
            # Stale voice client from a previous session — disconnect it
            # and retry the connect.
            stale = self.guild.voice_client
            if stale:
                log.warning("Cleaning up stale voice client before reconnecting")
                try:
                    await stale.disconnect(force=True)
                except Exception:
                    # If disconnect fails, force-cleanup discord.py's reference
                    stale.cleanup()
                self._voice_client = await self.channel.connect(cls=_BridgeVoiceProtocol)
            else:
                raise

        voice_data = self._voice_client.voice_data

        await self.bridge.join(
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
            session_id=voice_data.get("session_id", ""),
        )

        if voice_data.get("voice_state"):
            await self.bridge.send_voice_state_update(voice_data["voice_state"])
        if voice_data.get("voice_server"):
            await self.bridge.send_voice_server_update(voice_data["voice_server"])

        ready = await self.bridge.wait_ready(guild_id, timeout=15.0)
        if not ready:
            log.error("Voice bridge failed to connect for guild %s", guild_id)
            raise RuntimeError("Voice bridge connection timeout")

        self.is_active = True
        self._start_time = time.monotonic()

        # Register audio callback immediately — pipeline is already warm
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
        """Called when the bridge sends decoded audio from a user.

        The bridge already segments audio by silence (EndBehaviorType.AfterSilence),
        so each message is a complete speech utterance. Use process_segment()
        to skip the sink's VAD and process immediately.
        """
        if not self.is_active or not self._sink:
            return
        self._sink.process_segment(user_id, pcm)

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
        except ConnectionError:
            log.debug("Bridge not connected during cleanup, skipping disconnect")
        except Exception:
            log.warning("Error telling bridge to disconnect", exc_info=True)

        if self._voice_client:
            try:
                if self._voice_client.is_connected():
                    await self._voice_client.disconnect(force=True)
            except Exception:
                log.debug("Error disconnecting voice client (expected during cleanup)")
            self._voice_client = None

        if self._openclaw:
            if self._session_id:
                await self._openclaw.end_session(self._session_id)
            await self._openclaw.close()

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
        if not self.is_active:
            return
        if self._thinking_sound is None:
            await self._ensure_thinking_sound()
        if self._thinking_sound:
            try:
                self._is_playing = True
                # Send directly rather than using bridge.play() since we don't
                # want to block waiting for play_done — the LLM response will
                # stop this sound and then play the actual TTS audio.
                await self.bridge.send({
                    "op": "play",
                    "guild_id": self._guild_id_str,
                    "audio": base64.b64encode(self._thinking_sound).decode("ascii"),
                    "format": "wav",
                })
                log.debug("Thinking sound started via bridge")
            except ConnectionError:
                log.debug("Bridge not connected, skipping thinking sound")
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
                log.warning("OpenClaw returned empty response for %r (%.3fs)", text[:200], llm_elapsed)
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
            log.warning("TTS returned no audio for: %s", text[:200])
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
