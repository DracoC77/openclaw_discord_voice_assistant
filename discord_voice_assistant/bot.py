"""Core Discord bot with voice channel awareness."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from discord_voice_assistant.auth_store import AuthStore
from discord_voice_assistant.commands.admin import AdminCommands
from discord_voice_assistant.commands.general import GeneralCommands
from discord_voice_assistant.commands.voice import VoiceCommands
from discord_voice_assistant.voice_bridge import VoiceBridgeClient
from discord_voice_assistant.voice_manager import VoiceManager

if TYPE_CHECKING:
    from discord_voice_assistant.config import Config

log = logging.getLogger(__name__)

# How often to check if the bridge is still healthy (seconds)
_BRIDGE_HEALTH_INTERVAL = 30


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
        self.bridge = VoiceBridgeClient(config.voice_bridge.url)

        # Persistent auth store — bootstraps from env vars on first run
        default_agent = config.auth.default_agent_id or config.openclaw.agent_id
        self.auth_store = AuthStore(
            data_dir=config.data_dir,
            bootstrap_user_ids=config.auth.authorized_user_ids,
            bootstrap_admin_ids=config.auth.admin_user_ids,
            default_agent_id=default_agent,
        )

        self.voice_manager = VoiceManager(self, config, self.bridge)
        self._bridge_health_task: asyncio.Task | None = None

    async def on_ready(self) -> None:
        log.info("Logged in as %s (ID: %s)", self.user, self.user.id)
        log.info("Connected to %d guild(s)", len(self.guilds))

        # Auto-bootstrap: if the auth store is empty, add the application owner
        # as an admin so the bot is immediately usable (solves the chicken-and-egg
        # problem where no one is authorized and no one can authorize themselves).
        if self.auth_store.user_count == 0:
            try:
                app_info = await self.application_info()
                owner_id = app_info.owner.id
                self.auth_store.add_user(owner_id, role="admin", added_by="auto_owner")
                log.info(
                    "Auto-added application owner %s (%s) as admin "
                    "(auth store was empty)",
                    app_info.owner, owner_id,
                )
            except Exception:
                log.warning(
                    "Could not auto-add application owner — auth store remains "
                    "empty (all users rejected). Use /voice-add as bot owner "
                    "to bootstrap.",
                    exc_info=True,
                )

        # Connect to the Node.js voice bridge
        await self.bridge.start()
        try:
            await self.bridge.wait_connected(timeout=15.0)
            log.info("Voice bridge connected at %s", self.config.voice_bridge.url)
        except Exception:
            log.error(
                "Failed to connect to voice bridge at %s — voice will not work "
                "until the bridge comes online (reconnection is automatic)",
                self.config.voice_bridge.url,
            )

        await self.voice_manager.initialize()

        # Register cogs (only on first ready — reconnects also trigger on_ready)
        if not self.cogs:
            await self.add_cog(GeneralCommands(self))
            await self.add_cog(VoiceCommands(self))
            await self.add_cog(AdminCommands(self))

        # Sync slash commands
        try:
            synced = await self.tree.sync()
            log.info("Synced %d slash command(s)", len(synced))
        except Exception:
            log.exception("Failed to sync slash commands")

        # Start bridge health monitor
        if self._bridge_health_task is None or self._bridge_health_task.done():
            self._bridge_health_task = asyncio.create_task(
                self._monitor_bridge_health(), name="bridge-health-monitor"
            )

    async def _monitor_bridge_health(self) -> None:
        """Periodically log bridge connection status for observability."""
        while True:
            try:
                await asyncio.sleep(_BRIDGE_HEALTH_INTERVAL)
                if not self.bridge.is_connected:
                    log.warning(
                        "Voice bridge is disconnected (reconnect attempt %d)",
                        self.bridge.reconnect_attempts,
                    )
            except asyncio.CancelledError:
                return

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
        if self._bridge_health_task:
            self._bridge_health_task.cancel()
            self._bridge_health_task = None
        await self.voice_manager.cleanup()
        await self.bridge.stop()
        await super().close()
