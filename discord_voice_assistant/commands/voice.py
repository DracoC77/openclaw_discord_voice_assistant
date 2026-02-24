"""Voice-specific slash commands for the voice assistant."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

if TYPE_CHECKING:
    from discord_voice_assistant.bot import VoiceAssistantBot

log = logging.getLogger(__name__)


class VoiceCommands(commands.Cog):
    """Commands for controlling voice channel behavior."""

    def __init__(self, bot: VoiceAssistantBot) -> None:
        self.bot = bot

    @app_commands.command(name="join", description="Summon the voice assistant to your channel")
    async def join(self, interaction: discord.Interaction) -> None:
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message(
                "You need to be in a voice channel first!", ephemeral=True
            )
            return

        vm = self.bot.voice_manager
        if not vm.is_authorized(interaction.user.id):
            await interaction.response.send_message(
                "You're not authorized to summon the voice assistant. "
                "Ask the bot owner to use `/authorize` for you.",
                ephemeral=True,
            )
            return

        if not self.bot.bridge.is_connected:
            await interaction.response.send_message(
                "Voice bridge is not connected. Voice features are temporarily unavailable.",
                ephemeral=True,
            )
            return

        channel = interaction.user.voice.channel
        await interaction.response.send_message(f"Joining **{channel.name}**...", ephemeral=True)

        try:
            await vm.join_channel(channel)
        except Exception as e:
            await interaction.edit_original_response(content=f"Failed to join: {e}")

    @app_commands.command(name="leave", description="Make the voice assistant leave the channel")
    async def leave(self, interaction: discord.Interaction) -> None:
        vm = self.bot.voice_manager
        session = vm.get_session(interaction.guild.id)

        if not session or not session.is_active:
            await interaction.response.send_message("I'm not in a voice channel!", ephemeral=True)
            return

        await interaction.response.send_message("Leaving voice channel. Goodbye!", ephemeral=True)
        await vm.leave_channel(interaction.guild.id)

    @app_commands.command(
        name="rejoin",
        description="Rejoin the voice channel after inactivity disconnect",
    )
    async def rejoin(self, interaction: discord.Interaction) -> None:
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message(
                "You need to be in a voice channel first!", ephemeral=True
            )
            return

        vm = self.bot.voice_manager
        if not vm.is_authorized(interaction.user.id):
            await interaction.response.send_message("You're not authorized.", ephemeral=True)
            return

        channel = interaction.user.voice.channel
        await interaction.response.send_message(f"Rejoining **{channel.name}**...", ephemeral=True)
        await vm.join_channel(channel)

    @app_commands.command(
        name="voice-status",
        description="Show details about the current voice session",
    )
    async def voice_status(self, interaction: discord.Interaction) -> None:
        vm = self.bot.voice_manager
        session = vm.get_session(interaction.guild.id)

        if not session or not session.is_active:
            await interaction.response.send_message(
                "No active voice session in this server.", ephemeral=True
            )
            return

        import time

        duration = time.monotonic() - session.start_time
        minutes, seconds = divmod(int(duration), 60)
        hours, minutes = divmod(minutes, 60)

        bridge = self.bot.bridge
        guild_id_str = str(interaction.guild.id)

        embed = discord.Embed(
            title="Voice Session Status",
            color=discord.Color.green(),
        )
        embed.add_field(
            name="Channel", value=session.channel.name, inline=True
        )
        embed.add_field(
            name="Duration", value=f"{hours}h {minutes}m {seconds}s", inline=True
        )
        embed.add_field(
            name="Session ID",
            value=f"`{session.session_id[:8]}...`" if session.session_id else "N/A",
            inline=True,
        )
        embed.add_field(
            name="Bridge",
            value="Connected" if bridge.is_connected else "Disconnected",
            inline=True,
        )
        embed.add_field(
            name="DAVE E2EE",
            value="Active" if bridge.is_dave_active(guild_id_str) else "Inactive",
            inline=True,
        )

        members = [m for m in session.channel.members if not m.bot]
        embed.add_field(
            name="Users in Channel",
            value=", ".join(m.display_name for m in members) or "None",
            inline=False,
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="timeout",
        description="Set the inactivity timeout (in seconds)",
    )
    @app_commands.describe(seconds="Timeout in seconds (0 to disable)")
    async def timeout(
        self,
        interaction: discord.Interaction,
        seconds: app_commands.Range[int, 0, 3600],
    ) -> None:
        vm = self.bot.voice_manager
        if not vm.is_authorized(interaction.user.id):
            await interaction.response.send_message("You're not authorized.", ephemeral=True)
            return

        # Runtime-only change
        # We need a mutable config for this; use object.__setattr__ since frozen
        object.__setattr__(self.bot.config.voice, "inactivity_timeout", seconds)

        if seconds == 0:
            await interaction.response.send_message(
                "Inactivity timeout disabled.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"Inactivity timeout set to {seconds} seconds.", ephemeral=True
            )

        # Reset timer on current session if active
        session = vm.get_session(interaction.guild.id)
        if session and session.is_active:
            vm.reset_inactivity(interaction.guild.id)

    @app_commands.command(
        name="new",
        description="Start a fresh conversation (clears your context)",
    )
    async def new_session(self, interaction: discord.Interaction) -> None:
        vm = self.bot.voice_manager
        session = vm.get_session(interaction.guild.id)

        if not session or not session.is_active:
            await interaction.response.send_message(
                "No active voice session in this server.", ephemeral=True
            )
            return

        if not vm.is_authorized(interaction.user.id):
            await interaction.response.send_message("You're not authorized.", ephemeral=True)
            return

        # Reset the caller's per-user session
        user_session_id = session._get_or_create_user_session(interaction.user.id)
        user_agent_id = self.bot.auth_store.get_agent_id(interaction.user.id)

        await interaction.response.defer(ephemeral=True)
        success = await session._openclaw.reset_session(
            user_session_id, agent_id=user_agent_id
        )
        if success:
            await interaction.followup.send(
                "Your conversation cleared. Starting fresh!", ephemeral=True
            )
        else:
            await interaction.followup.send(
                "Failed to reset conversation. Check logs for details.", ephemeral=True
            )

    @app_commands.command(
        name="compact",
        description="Summarize your conversation history to free up context space",
    )
    async def compact_session(self, interaction: discord.Interaction) -> None:
        vm = self.bot.voice_manager
        session = vm.get_session(interaction.guild.id)

        if not session or not session.is_active:
            await interaction.response.send_message(
                "No active voice session in this server.", ephemeral=True
            )
            return

        if not vm.is_authorized(interaction.user.id):
            await interaction.response.send_message("You're not authorized.", ephemeral=True)
            return

        # Compact the caller's per-user session
        user_session_id = session._get_or_create_user_session(interaction.user.id)
        user_agent_id = self.bot.auth_store.get_agent_id(interaction.user.id)

        await interaction.response.defer(ephemeral=True)
        success = await session._openclaw.compact_session(
            user_session_id, agent_id=user_agent_id
        )
        if success:
            await interaction.followup.send(
                "Your conversation compacted. Context has been summarized.", ephemeral=True
            )
        else:
            await interaction.followup.send(
                "Failed to compact conversation. Check logs for details.", ephemeral=True
            )
