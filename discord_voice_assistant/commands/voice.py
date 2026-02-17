"""Voice-specific slash commands for the voice assistant."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

if TYPE_CHECKING:
    from discord_voice_assistant.bot import VoiceAssistantBot

log = logging.getLogger(__name__)


class VoiceCommands(commands.Cog):
    """Commands for controlling voice channel behavior."""

    def __init__(self, bot: VoiceAssistantBot) -> None:
        self.bot = bot

    @discord.slash_command(name="join", description="Summon the voice assistant to your channel")
    async def join(self, ctx: discord.ApplicationContext) -> None:
        if not ctx.author.voice or not ctx.author.voice.channel:
            await ctx.respond(
                "You need to be in a voice channel first!", ephemeral=True
            )
            return

        vm = self.bot.voice_manager
        if not vm.is_authorized(ctx.author.id):
            await ctx.respond(
                "You're not authorized to summon the voice assistant. "
                "Ask the bot owner to use `/authorize` for you.",
                ephemeral=True,
            )
            return

        channel = ctx.author.voice.channel
        await ctx.respond(f"Joining **{channel.name}**...", ephemeral=True)

        try:
            await vm.join_channel(channel)
        except Exception as e:
            await ctx.edit(content=f"Failed to join: {e}")

    @discord.slash_command(name="leave", description="Make the voice assistant leave the channel")
    async def leave(self, ctx: discord.ApplicationContext) -> None:
        vm = self.bot.voice_manager
        session = vm.get_session(ctx.guild.id)

        if not session or not session.is_active:
            await ctx.respond("I'm not in a voice channel!", ephemeral=True)
            return

        await ctx.respond("Leaving voice channel. Goodbye!", ephemeral=True)
        await vm.leave_channel(ctx.guild.id)

    @discord.slash_command(
        name="rejoin",
        description="Rejoin the voice channel after inactivity disconnect",
    )
    async def rejoin(self, ctx: discord.ApplicationContext) -> None:
        if not ctx.author.voice or not ctx.author.voice.channel:
            await ctx.respond(
                "You need to be in a voice channel first!", ephemeral=True
            )
            return

        vm = self.bot.voice_manager
        if not vm.is_authorized(ctx.author.id):
            await ctx.respond("You're not authorized.", ephemeral=True)
            return

        channel = ctx.author.voice.channel
        await ctx.respond(f"Rejoining **{channel.name}**...", ephemeral=True)
        await vm.join_channel(channel)

    @discord.slash_command(
        name="voice-status",
        description="Show details about the current voice session",
    )
    async def voice_status(self, ctx: discord.ApplicationContext) -> None:
        vm = self.bot.voice_manager
        session = vm.get_session(ctx.guild.id)

        if not session or not session.is_active:
            await ctx.respond("No active voice session in this server.", ephemeral=True)
            return

        import time

        duration = time.monotonic() - session._start_time
        minutes, seconds = divmod(int(duration), 60)
        hours, minutes = divmod(minutes, 60)

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
            value=f"`{session._session_id[:8]}...`" if session._session_id else "N/A",
            inline=True,
        )

        members = [m for m in session.channel.members if not m.bot]
        embed.add_field(
            name="Users in Channel",
            value=", ".join(m.display_name for m in members) or "None",
            inline=False,
        )

        await ctx.respond(embed=embed, ephemeral=True)

    @discord.slash_command(
        name="timeout",
        description="Set the inactivity timeout (in seconds)",
    )
    async def timeout(
        self,
        ctx: discord.ApplicationContext,
        seconds: discord.Option(
            int,
            "Timeout in seconds (0 to disable)",
            min_value=0,
            max_value=3600,
        ),
    ) -> None:
        vm = self.bot.voice_manager
        if not vm.is_authorized(ctx.author.id):
            await ctx.respond("You're not authorized.", ephemeral=True)
            return

        # Runtime-only change
        # We need a mutable config for this; use object.__setattr__ since frozen
        object.__setattr__(self.bot.config.voice, "inactivity_timeout", seconds)

        if seconds == 0:
            await ctx.respond("Inactivity timeout disabled.", ephemeral=True)
        else:
            await ctx.respond(
                f"Inactivity timeout set to {seconds} seconds.", ephemeral=True
            )

        # Reset timer on current session if active
        session = vm.get_session(ctx.guild.id)
        if session and session.is_active:
            vm._reset_inactivity_timer(ctx.guild.id)
