"""Core Discord bot with voice channel awareness."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from discord_voice_assistant.commands.general import GeneralCommands
from discord_voice_assistant.commands.voice import VoiceCommands
from discord_voice_assistant.voice_manager import VoiceManager

if TYPE_CHECKING:
    from discord_voice_assistant.config import Config

log = logging.getLogger(__name__)


class VoiceAssistantBot(commands.Bot):
    """Discord bot that manages voice sessions with OpenClaw integration."""

    def __init__(self, config: Config) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        intents.members = True

        super().__init__(
            command_prefix="!",
            intents=intents,
            activity=discord.Activity(
                type=discord.ActivityType.listening,
                name="for voice commands",
            ),
        )

        self.config = config
        self.voice_manager = VoiceManager(self, config)

    async def on_ready(self) -> None:
        log.info("Logged in as %s (ID: %s)", self.user, self.user.id)
        log.info("Connected to %d guild(s)", len(self.guilds))

        await self.voice_manager.initialize()

        # Register cogs
        await self.add_cog(GeneralCommands(self))
        await self.add_cog(VoiceCommands(self))

        # Sync slash commands
        try:
            synced = await self.tree.sync()
            log.info("Synced %d slash command(s)", len(synced))
        except Exception:
            log.exception("Failed to sync slash commands")

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """Track voice state changes to auto-join/leave channels."""
        # Ignore our own voice state changes
        if member.id == self.user.id:
            return

        await self.voice_manager.handle_voice_state_update(member, before, after)

    async def close(self) -> None:
        log.info("Shutting down voice assistant...")
        await self.voice_manager.cleanup()
        await super().close()
