"""Manages voice channel connections, sessions, and auto-join/leave behavior."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import aiohttp
import discord

from discord_voice_assistant.audio.stt import SpeechToText
from discord_voice_assistant.audio.tts import TextToSpeech
from discord_voice_assistant.audio.voicemail import (
    calculate_waveform,
    create_dm_channel,
    get_wav_duration,
    send_voice_message,
    wav_to_ogg_opus,
)
from discord_voice_assistant.voice_session import PRIORITY_NORMAL, VoiceSession

if TYPE_CHECKING:
    from discord_voice_assistant.bot import VoiceAssistantBot
    from discord_voice_assistant.config import Config
    from discord_voice_assistant.voice_bridge import VoiceBridgeClient

log = logging.getLogger(__name__)


class VoiceManager:
    """Coordinates voice channel presence and session lifecycle."""

    def __init__(self, bot: VoiceAssistantBot, config: Config, bridge: VoiceBridgeClient) -> None:
        self.bot = bot
        self.config = config
        self.bridge = bridge
        # guild_id -> VoiceSession
        self._sessions: dict[int, VoiceSession] = {}
        self._inactivity_tasks: dict[int, asyncio.Task] = {}
        # Serialize join/leave operations per guild to prevent race conditions
        self._guild_locks: dict[int, asyncio.Lock] = {}
        # Shared STT instance that persists across sessions (when STT_PRELOAD=true)
        self._shared_stt: SpeechToText | None = None
        # Pending notify messages: user_id -> [(text, priority)]
        self._pending_notify: dict[int, list[tuple[str, int]]] = {}
        # Shared TTS instance for voicemail (no active session needed)
        self._shared_tts: TextToSpeech | None = None
        # Shared HTTP session for Discord REST API calls (voicemail)
        self._http: aiohttp.ClientSession | None = None

    def _get_guild_lock(self, guild_id: int) -> asyncio.Lock:
        """Get or create a per-guild lock for serializing join/leave operations."""
        if guild_id not in self._guild_locks:
            self._guild_locks[guild_id] = asyncio.Lock()
        return self._guild_locks[guild_id]

    async def initialize(self) -> None:
        """Initialize audio subsystems.

        When STT_PRELOAD is enabled, the Whisper model is loaded once here and
        shared across all voice sessions so it survives leave/rejoin cycles.
        """
        if self.config.stt.preload:
            log.info("STT preload enabled — loading Whisper model at startup")
            self._shared_stt = SpeechToText(self.config.stt)
            await self._shared_stt.warm_up()
            log.info("Whisper model preloaded and ready")
        log.info("Voice manager initialized")

    def is_authorized(self, user_id: int) -> bool:
        """Check if a user is authorized to interact with the bot.

        Fail-closed: if no users are configured, all are rejected.
        """
        return self.bot.auth_store.is_authorized(user_id)

    async def handle_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """React to users joining/leaving voice channels."""
        guild_id = member.guild.id

        # User joined a voice channel
        if after.channel and (before.channel != after.channel):
            if self.config.voice.auto_join and self.is_authorized(member.id):
                await self._try_join(member, after.channel)

            # Deliver any pending notify messages for this user
            await self._deliver_pending_notify(member)

        # User left a voice channel (or switched)
        if before.channel and (before.channel != after.channel):
            await self._check_should_leave(guild_id, before.channel)

    def is_channel_allowed(self, guild_id: int, channel_id: int) -> bool:
        """Check if a channel is in the guild's allowlist (empty = all allowed)."""
        return self.bot.auth_store.is_channel_allowed(guild_id, channel_id)

    async def _try_join(
        self, member: discord.Member, channel: discord.VoiceChannel
    ) -> None:
        """Attempt to join a voice channel where an authorized user is."""
        guild_id = member.guild.id

        # Check channel allowlist before joining
        if not self.is_channel_allowed(guild_id, channel.id):
            log.debug(
                "Ignoring auto-join for %s in %s — channel not in allowlist",
                channel.name, member.guild.name,
            )
            return

        # Already in a session in this guild (or join in progress)
        if guild_id in self._sessions:
            session = self._sessions[guild_id]
            # If we're in a different channel, move to the authorized user's channel
            # (but only if the new channel is in the allowlist)
            if session.voice_client and session.voice_client.channel != channel:
                if not self.is_channel_allowed(guild_id, channel.id):
                    log.debug(
                        "Not following %s to %s — channel not in allowlist",
                        member.display_name, channel.name,
                    )
                    return
                log.info(
                    "Moving to %s in %s (following %s)",
                    channel.name,
                    member.guild.name,
                    member.display_name,
                )
                await session.move_to(channel)
            # Cancel any pending inactivity disconnect
            self._cancel_inactivity_timer(guild_id)
            return

        log.info(
            "Auto-joining %s in %s (triggered by %s)",
            channel.name,
            member.guild.name,
            member.display_name,
        )
        try:
            await self.join_channel(channel)
        except Exception:
            log.warning(
                "Failed to auto-join %s in %s",
                channel.name, member.guild.name, exc_info=True,
            )

    async def join_channel(self, channel: discord.VoiceChannel) -> VoiceSession:
        """Join a voice channel and start a new session.

        Uses a per-guild lock to prevent concurrent join/leave races.
        """
        guild_id = channel.guild.id
        async with self._get_guild_lock(guild_id):
            # Clean up existing session if any
            if guild_id in self._sessions:
                try:
                    await self._sessions[guild_id].stop()
                except Exception:
                    log.warning("Error stopping existing session in guild %d", guild_id, exc_info=True)
                finally:
                    self._sessions.pop(guild_id, None)

            session = VoiceSession(
                self.bot, self.config, channel, self.bridge,
                shared_stt=self._shared_stt,
            )
            self._sessions[guild_id] = session
            try:
                await session.start()
            except Exception:
                self._sessions.pop(guild_id, None)
                raise

            self._reset_inactivity_timer(guild_id)
            return session

    async def leave_channel(self, guild_id: int) -> None:
        """Leave the voice channel in a guild and clean up the session.

        Uses a per-guild lock to prevent concurrent join/leave races.
        """
        async with self._get_guild_lock(guild_id):
            self._cancel_inactivity_timer(guild_id)

            if guild_id in self._sessions:
                session = self._sessions.pop(guild_id)
                try:
                    await session.stop()
                except Exception:
                    log.warning("Error stopping session in guild %d", guild_id, exc_info=True)
                log.info("Left voice channel in guild %d", guild_id)

    async def _check_should_leave(
        self, guild_id: int, channel: discord.VoiceChannel
    ) -> None:
        """Check if we should leave because no authorized users remain."""
        # Count non-bot members in the channel
        human_members = [m for m in channel.members if not m.bot]

        if guild_id not in self._sessions:
            # No session but bot might be stuck in the channel (orphaned connection)
            if not human_members:
                guild = channel.guild
                if guild.voice_client and guild.voice_client.is_connected():
                    log.info("Cleaning up orphaned voice connection in %s", channel.name)
                    await guild.voice_client.disconnect(force=True)
            return

        session = self._sessions[guild_id]
        if not session.voice_client or session.voice_client.channel != channel:
            return

        authorized_members = [m for m in human_members if self.is_authorized(m.id)]

        if not human_members:
            # No humans left, leave immediately
            log.info("No users remaining in %s, leaving", channel.name)
            await self.leave_channel(guild_id)
        elif not authorized_members and self.bot.auth_store.user_count > 0:
            # No authorized users left, start short timer
            log.info("No authorized users in %s, starting leave timer", channel.name)
            self._reset_inactivity_timer(guild_id, timeout=30)

    def _reset_inactivity_timer(
        self, guild_id: int, timeout: int | None = None
    ) -> None:
        """Reset the inactivity timer for a guild session."""
        self._cancel_inactivity_timer(guild_id)

        if timeout is None:
            timeout = self.config.voice.inactivity_timeout
        if timeout <= 0:
            return

        async def _inactivity_disconnect() -> None:
            await asyncio.sleep(timeout)
            log.info("Inactivity timeout reached for guild %d", guild_id)
            await self.leave_channel(guild_id)

        self._inactivity_tasks[guild_id] = asyncio.create_task(
            _inactivity_disconnect()
        )

    def _cancel_inactivity_timer(self, guild_id: int) -> None:
        task = self._inactivity_tasks.pop(guild_id, None)
        if task and not task.done():
            task.cancel()

    def get_session(self, guild_id: int) -> VoiceSession | None:
        return self._sessions.get(guild_id)

    @property
    def session_count(self) -> int:
        """Number of active voice sessions."""
        return len(self._sessions)

    @property
    def active_sessions(self) -> dict[int, VoiceSession]:
        """Read-only view of active sessions (guild_id -> VoiceSession)."""
        return dict(self._sessions)

    def reset_inactivity(self, guild_id: int, timeout: int | None = None) -> None:
        """Public API to reset the inactivity timer for a guild."""
        self._reset_inactivity_timer(guild_id, timeout=timeout)

    async def cleanup(self) -> None:
        """Disconnect from all voice channels."""
        for guild_id in list(self._sessions):
            await self.leave_channel(guild_id)
        if self._http and not self._http.closed:
            await self._http.close()

    def notify_activity(self, guild_id: int) -> None:
        """Reset inactivity timer when there is voice activity."""
        if guild_id in self._sessions:
            self._reset_inactivity_timer(guild_id)

    # -- Proactive message routing ------------------------------------------------

    async def handle_proactive_message(
        self,
        text: str,
        mode: str = "auto",
        priority: int = PRIORITY_NORMAL,
        guild_id: int | None = None,
        channel_id: int | None = None,
        user_id: int | None = None,
    ) -> dict:
        """Route a proactive message to the appropriate delivery method.

        Returns a dict with ``status`` ("ok" or "error") and ``delivery``
        indicating which method was used.
        """
        if mode == "auto":
            return await self._deliver_auto(
                text, priority, guild_id, channel_id, user_id,
            )
        elif mode == "live":
            return await self._deliver_live(text, priority, guild_id)
        elif mode == "voicemail":
            return await self._deliver_voicemail(text, user_id)
        elif mode == "notify":
            return await self._deliver_notify(text, priority, user_id, guild_id)
        else:
            return {"status": "error", "error": f"unknown mode: {mode}"}

    async def _deliver_auto(
        self,
        text: str,
        priority: int,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int | None,
    ) -> dict:
        """Auto-mode: try live, fall back to notify, fall back to voicemail."""
        # Try live delivery first
        session = self._find_session_with_listeners(guild_id)
        if session:
            await session.enqueue_proactive(text, priority)
            return {"status": "ok", "delivery": "live"}

        # Resolve user_id from config if not provided
        user_id = self._resolve_user_id(user_id)
        if not user_id:
            return {
                "status": "error",
                "error": (
                    "no active voice session with listeners, and no user_id "
                    "provided for fallback (set WEBHOOK_NOTIFY_USER_IDS or "
                    "pass user_id in the request)"
                ),
            }

        # Try notify (DM + queue for when user joins)
        notify_result = await self._deliver_notify(
            text, priority, user_id, guild_id,
        )
        if notify_result.get("status") == "ok":
            return notify_result

        # Fall back to voicemail
        return await self._deliver_voicemail(text, user_id)

    async def _deliver_live(
        self, text: str, priority: int, guild_id: int | None,
    ) -> dict:
        """Deliver a message via the active voice session."""
        session = self._find_session_with_listeners(guild_id)
        if not session:
            return {
                "status": "error",
                "error": "no active voice session with listeners",
            }
        await session.enqueue_proactive(text, priority)
        return {"status": "ok", "delivery": "live"}

    async def _deliver_voicemail(
        self, text: str, user_id: int | None,
    ) -> dict:
        """Generate TTS and send as a Discord voice message to the user's DM."""
        user_id = self._resolve_user_id(user_id)
        if not user_id:
            return {
                "status": "error",
                "error": "user_id required for voicemail delivery",
            }

        bot_token = self.config.discord.token
        http = await self._get_http()

        # Create DM channel
        dm_channel_id = await create_dm_channel(http, bot_token, user_id)
        if not dm_channel_id:
            return {
                "status": "error",
                "error": f"could not create DM channel with user {user_id}",
            }

        # Generate TTS audio
        tts = await self._get_shared_tts()
        wav_bytes = await tts.synthesize(text)
        if not wav_bytes:
            return {"status": "error", "error": "TTS synthesis failed"}

        # Convert to OGG Opus
        ogg_bytes = await wav_to_ogg_opus(wav_bytes)
        if not ogg_bytes:
            return {"status": "error", "error": "WAV to OGG conversion failed"}

        # Calculate waveform and duration
        duration = get_wav_duration(wav_bytes)
        waveform = calculate_waveform(wav_bytes)

        # Send voice message
        success = await send_voice_message(
            http, bot_token, dm_channel_id, ogg_bytes, duration, waveform,
        )
        if success:
            log.info("Voicemail delivered to user %d (%.1fs)", user_id, duration)
            return {"status": "ok", "delivery": "voicemail"}
        return {"status": "error", "error": "failed to send voice message"}

    async def _deliver_notify(
        self,
        text: str,
        priority: int,
        user_id: int | None,
        guild_id: int | None,
    ) -> dict:
        """Send a DM notification and queue the message for when the user joins."""
        user_id = self._resolve_user_id(user_id)
        if not user_id:
            return {
                "status": "error",
                "error": "user_id required for notify delivery",
            }

        # Queue the message for when the user joins a voice channel
        if user_id not in self._pending_notify:
            self._pending_notify[user_id] = []
        self._pending_notify[user_id].append((text, priority))
        log.info(
            "Queued notify message for user %d (%d pending)",
            user_id, len(self._pending_notify[user_id]),
        )

        # Send a DM telling the user to join voice
        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            # Find a voice channel to suggest
            channel_name = self._suggest_voice_channel(guild_id)
            dm_text = (
                "I have something to tell you! "
                f"Join **{channel_name}** to hear it."
                if channel_name
                else "I have something to tell you! Join a voice channel to hear it."
            )
            await user.send(dm_text)
            log.info("Notify DM sent to user %d", user_id)
        except discord.Forbidden:
            log.warning(
                "Cannot DM user %d (DMs disabled) — message queued for "
                "when they join voice",
                user_id,
            )
        except Exception:
            log.exception("Failed to send notify DM to user %d", user_id)

        return {"status": "ok", "delivery": "notify"}

    async def _deliver_pending_notify(self, member: discord.Member) -> None:
        """Deliver any pending notify messages when a user joins a voice channel."""
        pending = self._pending_notify.pop(member.id, [])
        if not pending:
            return

        guild_id = member.guild.id
        session = self._sessions.get(guild_id)
        if not session or not session.is_active:
            # Session might not be ready yet — wait briefly for auto-join to complete
            await asyncio.sleep(2.0)
            session = self._sessions.get(guild_id)

        if not session or not session.is_active:
            # Put messages back — they'll be delivered next time
            self._pending_notify[member.id] = pending
            log.warning(
                "No active session to deliver %d pending notify messages for user %d",
                len(pending), member.id,
            )
            return

        log.info(
            "Delivering %d pending notify messages to user %d",
            len(pending), member.id,
        )
        for text, priority in pending:
            await session.enqueue_proactive(text, priority)

    # -- Helpers ------------------------------------------------------------------

    def _find_session_with_listeners(
        self, guild_id: int | None,
    ) -> VoiceSession | None:
        """Find an active session with human listeners."""
        if guild_id and guild_id in self._sessions:
            session = self._sessions[guild_id]
            if session.is_active and session.has_listeners():
                return session

        # No guild specified or specified guild has no listeners — try any session
        for session in self._sessions.values():
            if session.is_active and session.has_listeners():
                return session

        return None

    def _resolve_user_id(self, user_id: int | None) -> int | None:
        """Resolve a user ID from the request, webhook config, or auth store."""
        if user_id:
            return user_id
        notify_ids = self.config.webhook.notify_user_ids
        if notify_ids:
            return notify_ids[0]
        # Fall back to first authorized user from the auth store
        all_users = self.bot.auth_store.get_all_users()
        if all_users:
            return int(next(iter(all_users)))
        return None

    def _suggest_voice_channel(self, guild_id: int | None) -> str | None:
        """Suggest a voice channel name for the notify DM."""
        if guild_id:
            guild = self.bot.get_guild(guild_id)
            if guild and guild.voice_channels:
                return guild.voice_channels[0].name

        for guild in self.bot.guilds:
            if guild.voice_channels:
                return guild.voice_channels[0].name
        return None

    async def _get_shared_tts(self) -> TextToSpeech:
        """Get or create a shared TTS instance for voicemail."""
        if self._shared_tts is None:
            self._shared_tts = TextToSpeech(self.config.tts)
            await self._shared_tts.warm_up()
        return self._shared_tts

    async def _get_http(self) -> aiohttp.ClientSession:
        """Get or create a shared HTTP session for Discord API calls."""
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession()
        return self._http
