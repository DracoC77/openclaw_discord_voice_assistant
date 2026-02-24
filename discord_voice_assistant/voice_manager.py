"""Manages voice channel connections, sessions, and auto-join/leave behavior."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord

from discord_voice_assistant.audio.stt import SpeechToText
from discord_voice_assistant.voice_session import VoiceSession

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

        # User left a voice channel (or switched)
        if before.channel and (before.channel != after.channel):
            await self._check_should_leave(guild_id, before.channel)

    async def _try_join(
        self, member: discord.Member, channel: discord.VoiceChannel
    ) -> None:
        """Attempt to join a voice channel where an authorized user is."""
        guild_id = member.guild.id

        # Already in a session in this guild (or join in progress)
        if guild_id in self._sessions:
            session = self._sessions[guild_id]
            # If we're in a different channel, move to the authorized user's channel
            if session.voice_client and session.voice_client.channel != channel:
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
        elif not authorized_members and self.config.auth.authorized_user_ids:
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

    def notify_activity(self, guild_id: int) -> None:
        """Reset inactivity timer when there is voice activity."""
        if guild_id in self._sessions:
            self._reset_inactivity_timer(guild_id)
