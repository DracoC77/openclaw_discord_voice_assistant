"""General slash commands for the voice assistant."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

if TYPE_CHECKING:
    from discord_voice_assistant.bot import VoiceAssistantBot

log = logging.getLogger(__name__)


class GeneralCommands(commands.Cog):
    """General bot commands."""

    def __init__(self, bot: VoiceAssistantBot) -> None:
        self.bot = bot

    @app_commands.command(name="ping", description="Check if the voice assistant is alive")
    async def ping(self, interaction: discord.Interaction) -> None:
        latency = round(self.bot.latency * 1000)
        await interaction.response.send_message(f"Pong! Latency: {latency}ms", ephemeral=True)

    @app_commands.command(name="status", description="Show the voice assistant's current status")
    async def status(self, interaction: discord.Interaction) -> None:
        vm = self.bot.voice_manager
        bridge = self.bot.bridge
        session_count = vm.session_count
        authorized = len(self.bot.config.auth.authorized_user_ids)
        name = self.bot.config.discord.bot_name

        embed = discord.Embed(
            title=f"{name} Status",
            color=discord.Color.green() if session_count > 0 else discord.Color.greyple(),
        )
        embed.add_field(name="Active Voice Sessions", value=str(session_count), inline=True)
        embed.add_field(
            name="Voice Bridge",
            value="Connected" if bridge.is_connected else "Disconnected",
            inline=True,
        )
        embed.add_field(
            name="Auto-Join", value="Enabled" if self.bot.config.voice.auto_join else "Disabled", inline=True
        )
        embed.add_field(
            name="Inactivity Timeout",
            value=f"{self.bot.config.voice.inactivity_timeout}s",
            inline=True,
        )
        embed.add_field(
            name="Wake Word",
            value="Enabled" if self.bot.config.wake_word.enabled else "Disabled",
            inline=True,
        )
        embed.add_field(
            name="TTS Provider", value=self.bot.config.tts.provider, inline=True
        )
        embed.add_field(
            name="STT Model", value=self.bot.config.stt.model_size, inline=True
        )
        embed.add_field(
            name="Authorized Users",
            value=f"{authorized} configured" if authorized else "All users",
            inline=True,
        )

        # Show current voice sessions
        active = vm.active_sessions
        if active:
            session_info = []
            for gid, session in active.items():
                ch_name = session.channel.name if session.channel else "Unknown"
                session_info.append(f"#{ch_name}")
            embed.add_field(
                name="Connected Channels",
                value=", ".join(session_info),
                inline=False,
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="authorize",
        description="Add a user to the authorized list (bot owner only)",
    )
    @app_commands.describe(user="User to authorize")
    async def authorize(
        self,
        interaction: discord.Interaction,
        user: discord.User,
    ) -> None:
        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message(
                "Only the bot owner can use this command.", ephemeral=True
            )
            return

        if user.id not in self.bot.config.auth.authorized_user_ids:
            # Note: This is runtime-only. For persistence, update .env
            self.bot.config.auth.authorized_user_ids.append(user.id)
            await interaction.response.send_message(
                f"Authorized {user.mention} for voice interactions. "
                f"Add their ID ({user.id}) to AUTHORIZED_USER_IDS in .env for persistence.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"{user.mention} is already authorized.", ephemeral=True
            )

    @app_commands.command(
        name="deauthorize",
        description="Remove a user from the authorized list (bot owner only)",
    )
    @app_commands.describe(user="User to deauthorize")
    async def deauthorize(
        self,
        interaction: discord.Interaction,
        user: discord.User,
    ) -> None:
        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message(
                "Only the bot owner can use this command.", ephemeral=True
            )
            return

        if user.id in self.bot.config.auth.authorized_user_ids:
            self.bot.config.auth.authorized_user_ids.remove(user.id)
            await interaction.response.send_message(
                f"Deauthorized {user.mention}. "
                f"Remove their ID from AUTHORIZED_USER_IDS in .env for persistence.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"{user.mention} is not in the authorized list.", ephemeral=True
            )

    @app_commands.command(name="help", description="Show available voice assistant commands")
    async def help_cmd(self, interaction: discord.Interaction) -> None:
        name = self.bot.config.discord.bot_name
        embed = discord.Embed(
            title=f"{name} - Voice Assistant Commands",
            description="Discord Voice Assistant for OpenClaw",
            color=discord.Color.blue(),
        )
        embed.add_field(
            name="General",
            value=(
                "`/ping` - Check bot latency\n"
                "`/status` - Show current bot status\n"
                "`/help` - Show this help message\n"
                "`/authorize @user` - Authorize a user (owner only)\n"
                "`/deauthorize @user` - Deauthorize a user (owner only)"
            ),
            inline=False,
        )
        embed.add_field(
            name="Voice",
            value=(
                f"`/join` - Summon {name} to your voice channel\n"
                f"`/leave` - Make {name} leave the voice channel\n"
                "`/rejoin` - Rejoin after inactivity disconnect\n"
                "`/voice-status` - Show voice session details\n"
                "`/timeout <seconds>` - Set inactivity timeout\n"
                "`/new` - Start a fresh conversation\n"
                "`/compact` - Summarize conversation to free context"
            ),
            inline=False,
        )
        embed.set_footer(text=f"Say '{name}' to activate in multi-user voice channels")
        await interaction.response.send_message(embed=embed, ephemeral=True)
