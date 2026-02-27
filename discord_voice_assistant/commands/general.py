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
        store = self.bot.auth_store
        authorized = store.user_count
        admins = store.admin_count
        name = self.bot.config.discord.bot_name

        effective_provider = store.get_effective_tts_provider(
            self.bot.config.tts.provider
        )

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
            name="TTS Provider", value=effective_provider, inline=True
        )
        embed.add_field(
            name="STT Model", value=self.bot.config.stt.model_size, inline=True
        )
        embed.add_field(
            name="Authorized Users",
            value=f"{authorized} ({admins} admin)" if authorized else "None (fail-closed)",
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
                "`/help` - Show this help message"
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
        embed.add_field(
            name="Voice Customization",
            value=(
                "`/voice-set <voice>` - Set your personal TTS voice\n"
                "`/voice-voices` - Browse available voices\n"
                "`/voice-config` - Show your voice configuration"
            ),
            inline=False,
        )
        embed.add_field(
            name="Admin",
            value=(
                "`/voice-users` - List authorized users and roles\n"
                "`/voice-add @user [role] [agent]` - Add user\n"
                "`/voice-remove @user` - Remove user\n"
                "`/voice-promote @user` - Promote to admin\n"
                "`/voice-demote @user` - Demote to user\n"
                "`/voice-agent @user [agent_id]` - Set/clear agent\n"
                "`/voice-set-user @user [voice]` - Set/clear user voice\n"
                "`/voice-provider <provider>` - Switch TTS provider\n"
                "`/voice-channels` - List allowed channels\n"
                "`/voice-channel-add #ch` - Restrict to a channel\n"
                "`/voice-channel-remove #ch` - Un-restrict a channel\n"
                "`/voice-channel-clear` - Allow all channels"
            ),
            inline=False,
        )
        embed.set_footer(text=f"Say '{name}' to activate in multi-user voice channels")
        await interaction.response.send_message(embed=embed, ephemeral=True)
