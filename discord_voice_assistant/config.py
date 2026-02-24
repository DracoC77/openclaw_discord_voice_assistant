"""Centralized configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _bool(val: str | None) -> bool:
    return str(val).lower() in ("true", "1", "yes")


def _int_list(val: str | None) -> list[int]:
    if not val:
        return []
    return [int(v.strip()) for v in val.split(",") if v.strip()]


@dataclass(frozen=True)
class DiscordConfig:
    token: str = os.getenv("DISCORD_BOT_TOKEN", "")
    bot_name: str = os.getenv("BOT_NAME", "OpenClaw")


@dataclass(frozen=True)
class OpenClawConfig:
    url: str = os.getenv("OPENCLAW_URL", "http://localhost:18789")
    api_key: str = os.getenv("OPENCLAW_API_KEY", "")
    agent_id: str = os.getenv("OPENCLAW_AGENT_ID", "voice")


@dataclass(frozen=True)
class TTSConfig:
    provider: str = os.getenv("TTS_PROVIDER", "local")
    elevenlabs_api_key: str = os.getenv("ELEVENLABS_API_KEY", "")
    elevenlabs_voice_id: str = os.getenv("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")
    local_model: str = os.getenv("LOCAL_TTS_MODEL", "en_US-hfc_male-medium")


@dataclass(frozen=True)
class STTConfig:
    model_size: str = os.getenv("STT_MODEL_SIZE", "base")
    device: str = os.getenv("STT_DEVICE", "auto")
    compute_type: str = os.getenv("STT_COMPUTE_TYPE", "int8")
    download_root: str = os.getenv(
        "STT_DOWNLOAD_ROOT",
        str(Path(os.getenv("MODELS_DIR", "models")) / "whisper"),
    )
    preload: bool = _bool(os.getenv("STT_PRELOAD", "true"))


@dataclass(frozen=True)
class WakeWordConfig:
    enabled: bool = _bool(os.getenv("WAKE_WORD_ENABLED", "false"))
    model_path: str = os.getenv("WAKE_WORD_MODEL_PATH", "")
    threshold: float = float(os.getenv("WAKE_WORD_THRESHOLD", "0.5"))


@dataclass(frozen=True)
class ThinkingSoundConfig:
    tone1_hz: float = float(os.getenv("THINKING_TONE1_HZ", "130"))
    tone2_hz: float = float(os.getenv("THINKING_TONE2_HZ", "130"))
    tone_mix: float = float(os.getenv("THINKING_TONE_MIX", "0.7"))
    pulse_hz: float = float(os.getenv("THINKING_PULSE_HZ", "0.3"))
    volume: float = float(os.getenv("THINKING_VOLUME", "0.4"))
    duration: float = float(os.getenv("THINKING_DURATION", "2.5"))


@dataclass(frozen=True)
class VoiceBridgeConfig:
    url: str = os.getenv("VOICE_BRIDGE_URL", "ws://localhost:9876")


@dataclass(frozen=True)
class VoiceConfig:
    auto_join: bool = _bool(os.getenv("AUTO_JOIN_ENABLED", "true"))
    inactivity_timeout: int = int(os.getenv("INACTIVITY_TIMEOUT", "300"))
    max_session_duration: int = int(os.getenv("MAX_SESSION_DURATION", "0"))


@dataclass(frozen=True)
class AuthConfig:
    authorized_user_ids: list[int] = field(
        default_factory=lambda: _int_list(os.getenv("AUTHORIZED_USER_IDS", ""))
    )
    admin_user_ids: list[int] = field(
        default_factory=lambda: _int_list(os.getenv("ADMIN_USER_IDS", ""))
    )
    require_wake_word_for_unauthorized: bool = _bool(
        os.getenv("REQUIRE_WAKE_WORD_FOR_UNAUTHORIZED", "true")
    )
    default_agent_id: str = os.getenv("DEFAULT_AGENT_ID", "")


@dataclass(frozen=True)
class Config:
    discord: DiscordConfig = field(default_factory=DiscordConfig)
    openclaw: OpenClawConfig = field(default_factory=OpenClawConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    stt: STTConfig = field(default_factory=STTConfig)
    wake_word: WakeWordConfig = field(default_factory=WakeWordConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    voice_bridge: VoiceBridgeConfig = field(default_factory=VoiceBridgeConfig)
    thinking_sound: ThinkingSoundConfig = field(default_factory=ThinkingSoundConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    debug_voice: bool = _bool(os.getenv("DEBUG_VOICE_PIPELINE", "false"))
    data_dir: Path = Path(os.getenv("DATA_DIR", "data"))
    models_dir: Path = Path(os.getenv("MODELS_DIR", "models"))

    def validate(self) -> list[str]:
        """Return a list of configuration errors (empty if valid)."""
        errors: list[str] = []
        if not self.discord.token:
            errors.append("DISCORD_BOT_TOKEN is required")
        if not self.openclaw.url:
            errors.append("OPENCLAW_URL is required")
        if self.tts.provider == "elevenlabs" and not self.tts.elevenlabs_api_key:
            errors.append("ELEVENLABS_API_KEY is required when TTS_PROVIDER=elevenlabs")
        return errors
