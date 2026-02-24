"""Admin slash commands for user management and agent routing."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

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

            added_by = info.get("added_by", "unknown")
            embed.add_field(
                name=f"{role_badge} {name}",
                value=f"ID: `{uid}`\nRole: {role}{agent_line}\nAdded by: {added_by}",
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
