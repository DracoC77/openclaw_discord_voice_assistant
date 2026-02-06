"""General slash commands for Clippy."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

if TYPE_CHECKING:
    from clippy.bot import ClippyBot

log = logging.getLogger(__name__)


class GeneralCommands(commands.Cog):
    """General bot commands."""

    def __init__(self, bot: ClippyBot) -> None:
        self.bot = bot

    @discord.slash_command(name="ping", description="Check if Clippy is alive")
    async def ping(self, ctx: discord.ApplicationContext) -> None:
        latency = round(self.bot.latency * 1000)
        await ctx.respond(f"Pong! Latency: {latency}ms", ephemeral=True)

    @discord.slash_command(name="status", description="Show Clippy's current status")
    async def status(self, ctx: discord.ApplicationContext) -> None:
        vm = self.bot.voice_manager
        sessions = len(vm._sessions)
        authorized = len(self.bot.config.auth.authorized_user_ids)

        embed = discord.Embed(
            title="Clippy Status",
            color=discord.Color.green() if sessions > 0 else discord.Color.greyple(),
        )
        embed.add_field(name="Active Voice Sessions", value=str(sessions), inline=True)
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
        if vm._sessions:
            session_info = []
            for gid, session in vm._sessions.items():
                ch_name = session.channel.name if session.channel else "Unknown"
                session_info.append(f"#{ch_name}")
            embed.add_field(
                name="Connected Channels",
                value=", ".join(session_info),
                inline=False,
            )

        await ctx.respond(embed=embed, ephemeral=True)

    @discord.slash_command(
        name="authorize",
        description="Add a user to the authorized list (bot owner only)",
    )
    @commands.is_owner()
    async def authorize(
        self,
        ctx: discord.ApplicationContext,
        user: discord.Option(discord.User, "User to authorize"),
    ) -> None:
        if user.id not in self.bot.config.auth.authorized_user_ids:
            # Note: This is runtime-only. For persistence, update .env
            self.bot.config.auth.authorized_user_ids.append(user.id)
            await ctx.respond(
                f"Authorized {user.mention} for voice interactions. "
                f"Add their ID ({user.id}) to AUTHORIZED_USER_IDS in .env for persistence.",
                ephemeral=True,
            )
        else:
            await ctx.respond(f"{user.mention} is already authorized.", ephemeral=True)

    @discord.slash_command(
        name="deauthorize",
        description="Remove a user from the authorized list (bot owner only)",
    )
    @commands.is_owner()
    async def deauthorize(
        self,
        ctx: discord.ApplicationContext,
        user: discord.Option(discord.User, "User to deauthorize"),
    ) -> None:
        if user.id in self.bot.config.auth.authorized_user_ids:
            self.bot.config.auth.authorized_user_ids.remove(user.id)
            await ctx.respond(
                f"Deauthorized {user.mention}. "
                f"Remove their ID from AUTHORIZED_USER_IDS in .env for persistence.",
                ephemeral=True,
            )
        else:
            await ctx.respond(f"{user.mention} is not in the authorized list.", ephemeral=True)

    @discord.slash_command(name="help", description="Show Clippy's available commands")
    async def help_cmd(self, ctx: discord.ApplicationContext) -> None:
        embed = discord.Embed(
            title="Clippy - Voice Assistant Commands",
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
                "`/join` - Summon Clippy to your voice channel\n"
                "`/leave` - Make Clippy leave the voice channel\n"
                "`/rejoin` - Rejoin after inactivity disconnect\n"
                "`/enroll` - Enroll your voice profile for identification\n"
                "`/voice-status` - Show voice session details\n"
                "`/timeout <seconds>` - Set inactivity timeout"
            ),
            inline=False,
        )
        embed.set_footer(text="Say 'Clippy' to activate in multi-user voice channels")
        await ctx.respond(embed=embed, ephemeral=True)
