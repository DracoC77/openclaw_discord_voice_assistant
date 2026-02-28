"""Admin slash commands for user management and agent routing."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from discord_voice_assistant.commands.voice_config import (
    _get_voice_list_for_provider,
    _voice_display_name,
)

if TYPE_CHECKING:
    from discord_voice_assistant.bot import VoiceAssistantBot

log = logging.getLogger(__name__)


async def _check_admin(bot: VoiceAssistantBot, interaction: discord.Interaction) -> bool:
    """Return True if the caller is a bot owner or auth-store admin."""
    if await bot.is_owner(interaction.user):
        return True
    if bot.auth_store.is_admin(interaction.user.id):
        return True
    await interaction.response.send_message(
        "You need admin privileges to use this command.", ephemeral=True
    )
    return False


class AdminCommands(commands.Cog):
    """Admin commands for managing authorized users and agent routing."""

    def __init__(self, bot: VoiceAssistantBot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------
    # /voice-users — list all authorized users
    # ------------------------------------------------------------------

    @app_commands.command(
        name="voice-users",
        description="List all authorized voice users and their roles",
    )
    async def voice_users(self, interaction: discord.Interaction) -> None:
        if not await _check_admin(self.bot, interaction):
            return

        store = self.bot.auth_store
        users = store.get_all_users()
        routes = store.get_all_routes()

        if not users:
            await interaction.response.send_message(
                "No authorized users configured. Use `/voice-add` to add users.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="Authorized Voice Users",
            color=discord.Color.blue(),
            description=f"{len(users)} user(s) configured",
        )

        effective_provider = store.get_effective_tts_provider(
            self.bot.config.tts.provider
        )

        for uid_str, info in sorted(users.items()):
            uid = int(uid_str)
            role = info.get("role", "user")
            role_badge = "\U0001f6e1\ufe0f" if role == "admin" else "\U0001f464"

            # Try to resolve the user name from the guild
            member = interaction.guild.get_member(uid) if interaction.guild else None
            name = member.display_name if member else f"User {uid}"

            # Check for agent route
            route = routes.get(uid_str)
            agent_line = ""
            if route and route.get("agent_id"):
                agent_line = f"\nAgent: `{route['agent_id']}`"
            else:
                agent_line = f"\nAgent: `{store.default_agent_id}` (default)"

            # Check for voice override
            voice_prefs = store.get_user_voice(uid)
            voice_line = ""
            if effective_provider == "elevenlabs" and voice_prefs.get("elevenlabs_voice_id"):
                vid = voice_prefs["elevenlabs_voice_id"]
                vname = _voice_display_name("elevenlabs", vid)
                voice_line = f"\nVoice: {vname}"
            elif effective_provider == "local" and voice_prefs.get("local_tts_model"):
                model = voice_prefs["local_tts_model"]
                vname = _voice_display_name("local", model)
                voice_line = f"\nVoice: {vname}"

            added_by = info.get("added_by", "unknown")
            embed.add_field(
                name=f"{role_badge} {name}",
                value=f"ID: `{uid}`\nRole: {role}{agent_line}{voice_line}\nAdded by: {added_by}",
                inline=True,
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # /voice-add — add an authorized user
    # ------------------------------------------------------------------

    @app_commands.command(
        name="voice-add",
        description="Add a user to the authorized voice users list",
    )
    @app_commands.describe(
        user="User to authorize",
        role="Role to assign (default: user)",
        agent_id="OpenClaw agent ID for this user (optional, uses default if empty)",
    )
    @app_commands.choices(
        role=[
            app_commands.Choice(name="user", value="user"),
            app_commands.Choice(name="admin", value="admin"),
        ]
    )
    async def voice_add(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        role: app_commands.Choice[str] | None = None,
        agent_id: str | None = None,
    ) -> None:
        if not await _check_admin(self.bot, interaction):
            return

        store = self.bot.auth_store
        chosen_role = role.value if role else "user"

        if not store.add_user(user.id, role=chosen_role, added_by=interaction.user.id):
            await interaction.response.send_message(
                f"{user.mention} is already authorized. Use `/voice-remove` first to re-add with different settings.",
                ephemeral=True,
            )
            return

        # Set agent route if specified
        if agent_id:
            store.set_agent_id(user.id, agent_id)

        agent_info = f" with agent `{agent_id}`" if agent_id else ""
        await interaction.response.send_message(
            f"Added {user.mention} as **{chosen_role}**{agent_info}.",
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /voice-remove — remove an authorized user
    # ------------------------------------------------------------------

    @app_commands.command(
        name="voice-remove",
        description="Remove a user from the authorized voice users list",
    )
    @app_commands.describe(user="User to remove")
    async def voice_remove(
        self,
        interaction: discord.Interaction,
        user: discord.User,
    ) -> None:
        if not await _check_admin(self.bot, interaction):
            return

        store = self.bot.auth_store

        # Lockout protection: can't remove the last admin
        if store.is_last_admin(user.id):
            await interaction.response.send_message(
                f"Cannot remove {user.mention} — they are the last admin. "
                "Promote another user to admin first.",
                ephemeral=True,
            )
            return

        if not store.remove_user(user.id):
            await interaction.response.send_message(
                f"{user.mention} is not in the authorized list.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"Removed {user.mention} from authorized users.",
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /voice-promote — promote a user to admin
    # ------------------------------------------------------------------

    @app_commands.command(
        name="voice-promote",
        description="Promote an authorized user to admin role",
    )
    @app_commands.describe(user="User to promote")
    async def voice_promote(
        self,
        interaction: discord.Interaction,
        user: discord.User,
    ) -> None:
        if not await _check_admin(self.bot, interaction):
            return

        store = self.bot.auth_store

        if not store.is_authorized(user.id):
            await interaction.response.send_message(
                f"{user.mention} is not authorized. Add them first with `/voice-add`.",
                ephemeral=True,
            )
            return

        if not store.promote_user(user.id):
            await interaction.response.send_message(
                f"{user.mention} is already an admin.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"Promoted {user.mention} to **admin**.",
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /voice-demote — demote an admin to user
    # ------------------------------------------------------------------

    @app_commands.command(
        name="voice-demote",
        description="Demote an admin to regular user role",
    )
    @app_commands.describe(user="Admin to demote")
    async def voice_demote(
        self,
        interaction: discord.Interaction,
        user: discord.User,
    ) -> None:
        if not await _check_admin(self.bot, interaction):
            return

        store = self.bot.auth_store

        # Lockout protection
        if store.is_last_admin(user.id):
            await interaction.response.send_message(
                f"Cannot demote {user.mention} — they are the last admin. "
                "Promote another user first.",
                ephemeral=True,
            )
            return

        if not store.demote_user(user.id):
            await interaction.response.send_message(
                f"{user.mention} is not an admin (or not authorized).",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"Demoted {user.mention} to **user**.",
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /voice-agent — set or clear a per-user agent ID
    # ------------------------------------------------------------------

    @app_commands.command(
        name="voice-agent",
        description="Set or clear the OpenClaw agent ID for a user",
    )
    @app_commands.describe(
        user="User to configure",
        agent_id="Agent ID to assign (leave empty to reset to default)",
    )
    async def voice_agent(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        agent_id: str | None = None,
    ) -> None:
        if not await _check_admin(self.bot, interaction):
            return

        store = self.bot.auth_store

        if not store.is_authorized(user.id):
            await interaction.response.send_message(
                f"{user.mention} is not authorized. Add them first with `/voice-add`.",
                ephemeral=True,
            )
            return

        if agent_id:
            store.set_agent_id(user.id, agent_id)
            await interaction.response.send_message(
                f"Set agent for {user.mention} to `{agent_id}`.",
                ephemeral=True,
            )
        else:
            store.clear_agent_id(user.id)
            await interaction.response.send_message(
                f"Reset agent for {user.mention} to default (`{store.default_agent_id}`).",
                ephemeral=True,
            )

    # ------------------------------------------------------------------
    # /voice-set-user — admin sets voice for another user
    # ------------------------------------------------------------------

    @app_commands.command(
        name="voice-set-user",
        description="Set the TTS voice for a specific user (admin only)",
    )
    @app_commands.describe(
        user="User to configure",
        voice="Voice to assign (type to search, or paste a custom voice ID/model name). "
              "Leave empty to clear.",
    )
    async def voice_set_user(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        voice: str | None = None,
    ) -> None:
        if not await _check_admin(self.bot, interaction):
            return

        store = self.bot.auth_store

        if not store.is_authorized(user.id):
            await interaction.response.send_message(
                f"{user.mention} is not authorized. Add them first with `/voice-add`.",
                ephemeral=True,
            )
            return

        if not voice:
            if store.clear_user_voice(user.id):
                await interaction.response.send_message(
                    f"Cleared voice override for {user.mention}. Using default voice.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    f"{user.mention} has no custom voice set.",
                    ephemeral=True,
                )
            return

        provider = store.get_effective_tts_provider(self.bot.config.tts.provider)
        display = _voice_display_name(provider, voice)

        if provider == "elevenlabs":
            store.set_user_voice(user.id, elevenlabs_voice_id=voice)
        else:
            store.set_user_voice(user.id, local_tts_model=voice)

        await interaction.response.send_message(
            f"Voice for {user.mention} set to **{display}** (provider: {provider}).",
            ephemeral=True,
        )

    @voice_set_user.autocomplete("voice")
    async def _voice_set_user_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        store = self.bot.auth_store
        provider = store.get_effective_tts_provider(self.bot.config.tts.provider)
        voices = _get_voice_list_for_provider(provider)

        choices: list[app_commands.Choice[str]] = []
        query = current.lower()
        for name, value in voices:
            if query and query not in name.lower() and query not in value.lower():
                continue
            choices.append(app_commands.Choice(name=name, value=value))
            if len(choices) >= 25:
                break
        return choices

    # ------------------------------------------------------------------
    # /voice-channels — list allowed channels for this guild
    # ------------------------------------------------------------------

    @app_commands.command(
        name="voice-channels",
        description="List voice channels the bot is allowed to join in this server",
    )
    async def voice_channels(self, interaction: discord.Interaction) -> None:
        if not await _check_admin(self.bot, interaction):
            return

        store = self.bot.auth_store
        guild = interaction.guild
        allowed = store.get_allowed_channels(guild.id)

        if not allowed:
            await interaction.response.send_message(
                "No channel restrictions — the bot can join **any** voice channel in this server.\n"
                "Use `/voice-channel-add` to restrict it to specific channels.",
                ephemeral=True,
            )
            return

        lines = []
        for cid in allowed:
            channel = guild.get_channel(cid)
            if channel:
                lines.append(f"- {channel.mention} (`{cid}`)")
            else:
                lines.append(f"- *Unknown channel* (`{cid}`)")

        embed = discord.Embed(
            title="Allowed Voice Channels",
            description="\n".join(lines),
            color=discord.Color.blue(),
        )
        embed.set_footer(
            text="The bot will only auto-join and accept /join for these channels. "
            "Use /voice-channel-clear to remove all restrictions."
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # /voice-channel-add — add a channel to the allowlist
    # ------------------------------------------------------------------

    @app_commands.command(
        name="voice-channel-add",
        description="Add a voice channel the bot is allowed to join",
    )
    @app_commands.describe(channel="Voice channel to allow")
    async def voice_channel_add(
        self,
        interaction: discord.Interaction,
        channel: discord.VoiceChannel,
    ) -> None:
        if not await _check_admin(self.bot, interaction):
            return

        store = self.bot.auth_store

        if not store.add_allowed_channel(interaction.guild.id, channel.id):
            await interaction.response.send_message(
                f"{channel.mention} is already in the allowlist.",
                ephemeral=True,
            )
            return

        count = len(store.get_allowed_channels(interaction.guild.id))
        await interaction.response.send_message(
            f"Added {channel.mention} to the allowlist ({count} channel(s) configured).\n"
            "The bot will now **only** join allowed channels.",
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /voice-channel-remove — remove a channel from the allowlist
    # ------------------------------------------------------------------

    @app_commands.command(
        name="voice-channel-remove",
        description="Remove a voice channel from the bot's allowlist",
    )
    @app_commands.describe(channel="Voice channel to remove")
    async def voice_channel_remove(
        self,
        interaction: discord.Interaction,
        channel: discord.VoiceChannel,
    ) -> None:
        if not await _check_admin(self.bot, interaction):
            return

        store = self.bot.auth_store

        if not store.remove_allowed_channel(interaction.guild.id, channel.id):
            await interaction.response.send_message(
                f"{channel.mention} is not in the allowlist.",
                ephemeral=True,
            )
            return

        remaining = store.get_allowed_channels(interaction.guild.id)
        if remaining:
            await interaction.response.send_message(
                f"Removed {channel.mention} from the allowlist ({len(remaining)} channel(s) remaining).",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"Removed {channel.mention}. Allowlist is now empty — bot can join **any** channel.",
                ephemeral=True,
            )

    # ------------------------------------------------------------------
    # /voice-channel-clear — remove all channel restrictions
    # ------------------------------------------------------------------

    @app_commands.command(
        name="voice-channel-clear",
        description="Remove all channel restrictions (bot can join any voice channel)",
    )
    async def voice_channel_clear(self, interaction: discord.Interaction) -> None:
        if not await _check_admin(self.bot, interaction):
            return

        store = self.bot.auth_store

        if not store.clear_allowed_channels(interaction.guild.id):
            await interaction.response.send_message(
                "No channel restrictions to clear — bot can already join any channel.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "Channel restrictions cleared. The bot can now join **any** voice channel.",
            ephemeral=True,
        )
