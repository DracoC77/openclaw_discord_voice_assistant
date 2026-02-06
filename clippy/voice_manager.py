"""Manages voice channel connections, sessions, and auto-join/leave behavior."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord

from clippy.voice_session import VoiceSession

if TYPE_CHECKING:
    from clippy.bot import ClippyBot
    from clippy.config import Config

log = logging.getLogger(__name__)


class VoiceManager:
    """Coordinates voice channel presence and session lifecycle."""

    def __init__(self, bot: ClippyBot, config: Config) -> None:
        self.bot = bot
        self.config = config
        # guild_id -> VoiceSession
        self._sessions: dict[int, VoiceSession] = {}
        self._inactivity_tasks: dict[int, asyncio.Task] = {}

    async def initialize(self) -> None:
        """Initialize audio subsystems (models loaded lazily on first use)."""
        log.info("Voice manager initialized")

    def is_authorized(self, user_id: int) -> bool:
        """Check if a user is authorized to interact with the bot."""
        ids = self.config.auth.authorized_user_ids
        # If no authorized users configured, allow all
        return not ids or user_id in ids

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

        # Already in a session in this guild
        if guild_id in self._sessions and self._sessions[guild_id].is_active:
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
        await self.join_channel(channel)

    async def join_channel(self, channel: discord.VoiceChannel) -> VoiceSession:
        """Join a voice channel and start a new session."""
        guild_id = channel.guild.id

        # Clean up existing session if any
        if guild_id in self._sessions:
            await self._sessions[guild_id].stop()

        session = VoiceSession(self.bot, self.config, channel)
        self._sessions[guild_id] = session
        await session.start()

        self._reset_inactivity_timer(guild_id)
        return session

    async def leave_channel(self, guild_id: int) -> None:
        """Leave the voice channel in a guild and clean up the session."""
        self._cancel_inactivity_timer(guild_id)

        if guild_id in self._sessions:
            session = self._sessions.pop(guild_id)
            await session.stop()
            log.info("Left voice channel in guild %d", guild_id)

    async def _check_should_leave(
        self, guild_id: int, channel: discord.VoiceChannel
    ) -> None:
        """Check if we should leave because no authorized users remain."""
        if guild_id not in self._sessions:
            return

        session = self._sessions[guild_id]
        if not session.voice_client or session.voice_client.channel != channel:
            return

        # Count non-bot members in the channel
        human_members = [m for m in channel.members if not m.bot]
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

    async def cleanup(self) -> None:
        """Disconnect from all voice channels."""
        for guild_id in list(self._sessions):
            await self.leave_channel(guild_id)

    def notify_activity(self, guild_id: int) -> None:
        """Reset inactivity timer when there is voice activity."""
        if guild_id in self._sessions:
            self._reset_inactivity_timer(guild_id)
