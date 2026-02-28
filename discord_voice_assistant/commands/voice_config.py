"""Slash commands for per-user voice customization and TTS provider management."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

if TYPE_CHECKING:
    from discord_voice_assistant.bot import VoiceAssistantBot

log = logging.getLogger(__name__)

# -----------------------------------------------------------------------
# Curated voice lists for autocomplete
# -----------------------------------------------------------------------

# ElevenLabs built-in voices: (display_name, voice_id)
ELEVENLABS_VOICES: list[tuple[str, str]] = [
    ("Rachel - calm, young female", "21m00Tcm4TlvDq8ikWAM"),
    ("Domi - strong, young female", "AZnzlk1XvdvUeBnXmlld"),
    ("Bella - soft, young female", "EXAVITQu4vr4xnSDxMaL"),
    ("Antoni - well-rounded, young male", "ErXwobaYiN019PkySvjV"),
    ("Elli - emotional, young female", "MF3mGyEYCl7XYWbV9V6O"),
    ("Josh - deep, young male", "TxGEqnHWrfWFTfGW9XjX"),
    ("Arnold - crisp, older male", "VR6AewLTigWG4xSOukaG"),
    ("Adam - deep, middle-aged male", "pNInz6obpgDQGcFmaJgB"),
    ("Sam - raspy, young male", "yoZ06aMxZJJ28mfd3POQ"),
]

# Piper voices: (display_name, model_name)
PIPER_VOICES: list[tuple[str, str]] = [
    ("Lessac (US, medium) - default quality", "en_US-lessac-medium"),
    ("Lessac (US, high) - higher quality", "en_US-lessac-high"),
    ("HFC Male (US, medium)", "en_US-hfc_male-medium"),
    ("HFC Female (US, medium)", "en_US-hfc_female-medium"),
    ("Amy (US, medium)", "en_US-amy-medium"),
    ("Joe (US, medium)", "en_US-joe-medium"),
    ("Kristin (US, medium)", "en_US-kristin-medium"),
    ("Kusal (US, medium)", "en_US-kusal-medium"),
    ("Ryan (US, medium)", "en_US-ryan-medium"),
    ("Ryan (US, high)", "en_US-ryan-high"),
    ("Danny (US, low)", "en_US-danny-low"),
    ("Arctic (US, medium)", "en_US-arctic-medium"),
    ("LibriTTS (US, high)", "en_US-libritts-high"),
    ("LibriTTS-R (US, medium)", "en_US-libritts_r-medium"),
    ("Alan (GB, medium)", "en_GB-alan-medium"),
    ("Alba (GB, medium)", "en_GB-alba-medium"),
    ("Aru (GB, medium)", "en_GB-aru-medium"),
    ("Cori (GB, medium)", "en_GB-cori-medium"),
    ("Jenny Dioco (GB, medium)", "en_GB-jenny_dioco-medium"),
    ("Northern English Male (GB)", "en_GB-northern_english_male-medium"),
    ("Semaine (GB, medium)", "en_GB-semaine-medium"),
    ("Southern English Female (GB)", "en_GB-southern_english_female-low"),
    ("VCTK (GB, medium) - multi-speaker", "en_GB-vctk-medium"),
]


def _get_voice_list_for_provider(provider: str) -> list[tuple[str, str]]:
    """Return (display_name, value) pairs for the given provider."""
    if provider == "elevenlabs":
        return ELEVENLABS_VOICES
    return PIPER_VOICES


def _voice_display_name(provider: str, value: str) -> str:
    """Look up the display name for a voice value, or return the raw value."""
    for name, vid in _get_voice_list_for_provider(provider):
        if vid == value:
            return name
    return value


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


class VoiceConfigCommands(commands.Cog):
    """Commands for configuring TTS provider and per-user voice preferences."""

    def __init__(self, bot: VoiceAssistantBot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------
    # /voice-provider — set global TTS provider (admin only)
    # ------------------------------------------------------------------

    @app_commands.command(
        name="voice-provider",
        description="Set the global TTS provider (admin only)",
    )
    @app_commands.describe(
        provider="TTS provider to use globally",
    )
    @app_commands.choices(
        provider=[
            app_commands.Choice(name="local (Piper - free, runs locally)", value="local"),
            app_commands.Choice(name="elevenlabs (cloud - requires API key)", value="elevenlabs"),
            app_commands.Choice(name="reset to env default", value="__reset__"),
        ]
    )
    async def voice_provider(
        self,
        interaction: discord.Interaction,
        provider: app_commands.Choice[str],
    ) -> None:
        if not await _check_admin(self.bot, interaction):
            return

        store = self.bot.auth_store

        if provider.value == "__reset__":
            store.clear_global_tts_provider()
            env_default = self.bot.config.tts.provider
            await interaction.response.send_message(
                f"Global TTS provider reset to env default: **{env_default}**.\n"
                "Takes effect on the next speech synthesis.",
                ephemeral=True,
            )
            return

        # Validate elevenlabs has API key
        if provider.value == "elevenlabs" and not self.bot.config.tts.elevenlabs_api_key:
            await interaction.response.send_message(
                "Cannot switch to ElevenLabs: `ELEVENLABS_API_KEY` is not configured.\n"
                "Set the env var and restart, or use `local` provider.",
                ephemeral=True,
            )
            return

        store.set_global_tts_provider(provider.value)
        await interaction.response.send_message(
            f"Global TTS provider set to **{provider.value}**.\n"
            "Takes effect on the next speech synthesis (no restart needed).",
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /voice-set — set your own voice (self-service)
    # ------------------------------------------------------------------

    @app_commands.command(
        name="voice-set",
        description="Set your personal TTS voice",
    )
    @app_commands.describe(
        voice="Voice to use (type to search, or paste a custom voice ID/model name)",
    )
    async def voice_set(
        self,
        interaction: discord.Interaction,
        voice: str,
    ) -> None:
        store = self.bot.auth_store

        if not store.is_authorized(interaction.user.id):
            await interaction.response.send_message(
                "You need to be an authorized voice user to set your voice.",
                ephemeral=True,
            )
            return

        if voice == "__reset__":
            if store.clear_user_voice(interaction.user.id):
                await interaction.response.send_message(
                    "Your voice preference has been cleared. Using the default voice.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    "You don't have a custom voice set.", ephemeral=True
                )
            return

        # Determine which provider field to set based on current provider
        provider = store.get_effective_tts_provider(self.bot.config.tts.provider)
        display = _voice_display_name(provider, voice)

        warning = ""
        if provider == "elevenlabs":
            store.set_user_voice(interaction.user.id, elevenlabs_voice_id=voice)
        else:
            # Validate the Piper model exists or is a known model name
            known_models = {m for _, m in PIPER_VOICES}
            piper_model_dir = os.getenv("PIPER_MODEL_DIR", "/opt/piper")
            model_file = os.path.join(piper_model_dir, f"{voice}.onnx")
            if voice not in known_models and not os.path.isfile(model_file):
                warning = (
                    f"\n\n**Warning:** Model `{voice}` is not pre-installed "
                    "and not in the known voice list. It will be auto-downloaded "
                    "from HuggingFace on first use — if the name is wrong, TTS "
                    "will fall back to espeak-ng (robotic voice). "
                    "Use `/voice-voices` to see known models."
                )
            store.set_user_voice(interaction.user.id, local_tts_model=voice)

        await interaction.response.send_message(
            f"Your voice set to **{display}** (provider: {provider}).\n"
            f"Takes effect on your next speech.{warning}",
            ephemeral=True,
        )

    @voice_set.autocomplete("voice")
    async def _voice_set_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        store = self.bot.auth_store
        provider = store.get_effective_tts_provider(self.bot.config.tts.provider)
        voices = _get_voice_list_for_provider(provider)

        # Always include reset option
        choices = [app_commands.Choice(name="Reset to default", value="__reset__")]

        query = current.lower()
        for name, value in voices:
            if query and query not in name.lower() and query not in value.lower():
                continue
            choices.append(app_commands.Choice(name=name, value=value))
            if len(choices) >= 25:  # Discord limit
                break

        return choices

    # ------------------------------------------------------------------
    # /voice-voices — list available voices
    # ------------------------------------------------------------------

    @app_commands.command(
        name="voice-voices",
        description="List available TTS voices for the current provider",
    )
    @app_commands.describe(
        provider="Provider to list voices for (defaults to current global provider)",
    )
    @app_commands.choices(
        provider=[
            app_commands.Choice(name="local (Piper)", value="local"),
            app_commands.Choice(name="elevenlabs", value="elevenlabs"),
        ]
    )
    async def voice_voices(
        self,
        interaction: discord.Interaction,
        provider: app_commands.Choice[str] | None = None,
    ) -> None:
        store = self.bot.auth_store
        effective_provider = (
            provider.value
            if provider
            else store.get_effective_tts_provider(self.bot.config.tts.provider)
        )
        voices = _get_voice_list_for_provider(effective_provider)

        embed = discord.Embed(
            title=f"Available Voices ({effective_provider})",
            color=discord.Color.purple(),
        )

        if effective_provider == "elevenlabs":
            embed.description = (
                "Built-in ElevenLabs voices. You can also use any custom "
                "voice ID from your ElevenLabs account."
            )
            lines = []
            for name, vid in voices:
                lines.append(f"**{name}**\n`{vid}`")
            embed.add_field(name="Voices", value="\n".join(lines), inline=False)
        else:
            embed.description = (
                "Piper voices (auto-downloaded from HuggingFace on first use). "
                "You can also use any valid Piper model name."
            )
            # Split into US and GB columns
            us_lines = []
            gb_lines = []
            for name, model in voices:
                line = f"**{name}**\n`{model}`"
                if model.startswith("en_GB"):
                    gb_lines.append(line)
                else:
                    us_lines.append(line)
            if us_lines:
                embed.add_field(
                    name="US English", value="\n".join(us_lines), inline=True
                )
            if gb_lines:
                embed.add_field(
                    name="GB English", value="\n".join(gb_lines), inline=True
                )

        embed.set_footer(text="Use /voice-set <voice> to set your voice")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # /voice-config — show current voice configuration
    # ------------------------------------------------------------------

    @app_commands.command(
        name="voice-config",
        description="Show the current TTS voice configuration",
    )
    async def voice_config(self, interaction: discord.Interaction) -> None:
        store = self.bot.auth_store
        tts_cfg = self.bot.config.tts

        env_provider = tts_cfg.provider
        global_override = store.get_global_tts_provider()
        effective_provider = global_override or env_provider

        embed = discord.Embed(
            title="Voice Configuration",
            color=discord.Color.teal(),
        )

        # Global section
        global_lines = [
            f"**Env default provider:** `{env_provider}`",
        ]
        if global_override:
            global_lines.append(f"**Global override:** `{global_override}`")
        global_lines.append(f"**Active provider:** `{effective_provider}`")

        if effective_provider == "elevenlabs":
            default_voice = _voice_display_name("elevenlabs", tts_cfg.elevenlabs_voice_id)
            global_lines.append(f"**Default voice:** {default_voice}")
            global_lines.append(f"**Default voice ID:** `{tts_cfg.elevenlabs_voice_id}`")
        else:
            default_model = _voice_display_name("local", tts_cfg.local_model)
            global_lines.append(f"**Default model:** {default_model}")
            global_lines.append(f"**Default model name:** `{tts_cfg.local_model}`")

        embed.add_field(
            name="Global Settings",
            value="\n".join(global_lines),
            inline=False,
        )

        # User's personal settings
        user_prefs = store.get_user_voice(interaction.user.id)
        if user_prefs:
            user_lines = []
            if user_prefs.get("elevenlabs_voice_id"):
                vid = user_prefs["elevenlabs_voice_id"]
                name = _voice_display_name("elevenlabs", vid)
                user_lines.append(f"**ElevenLabs voice:** {name} (`{vid}`)")
            if user_prefs.get("local_tts_model"):
                model = user_prefs["local_tts_model"]
                name = _voice_display_name("local", model)
                user_lines.append(f"**Piper model:** {name} (`{model}`)")
            embed.add_field(
                name="Your Voice Settings",
                value="\n".join(user_lines),
                inline=False,
            )
        else:
            embed.add_field(
                name="Your Voice Settings",
                value="Using defaults. Use `/voice-set` to customize.",
                inline=False,
            )

        embed.set_footer(
            text="Use /voice-set to change your voice, "
            "/voice-provider to change the global provider (admin)"
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
