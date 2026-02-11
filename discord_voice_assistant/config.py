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
    bot_name: str = os.getenv("BOT_NAME", "Clippy")


@dataclass(frozen=True)
class OpenClawConfig:
    url: str = os.getenv("OPENCLAW_URL", "http://localhost:18789")
    api_key: str = os.getenv("OPENCLAW_API_KEY", "")
    agent_id: str = os.getenv("OPENCLAW_AGENT_ID", "default")


@dataclass(frozen=True)
class TTSConfig:
    provider: str = os.getenv("TTS_PROVIDER", "local")
    elevenlabs_api_key: str = os.getenv("ELEVENLABS_API_KEY", "")
    elevenlabs_voice_id: str = os.getenv("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")
    local_model: str = os.getenv("LOCAL_TTS_MODEL", "/opt/piper/en_US-lessac-medium.onnx")


@dataclass(frozen=True)
class STTConfig:
    model_size: str = os.getenv("STT_MODEL_SIZE", "base")
    device: str = os.getenv("STT_DEVICE", "auto")
    compute_type: str = os.getenv("STT_COMPUTE_TYPE", "int8")


@dataclass(frozen=True)
class WakeWordConfig:
    enabled: bool = _bool(os.getenv("WAKE_WORD_ENABLED", "true"))
    model_path: str = os.getenv("WAKE_WORD_MODEL_PATH", "")
    threshold: float = float(os.getenv("WAKE_WORD_THRESHOLD", "0.5"))


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
    require_wake_word_for_unauthorized: bool = _bool(
        os.getenv("REQUIRE_WAKE_WORD_FOR_UNAUTHORIZED", "true")
    )


@dataclass(frozen=True)
class Config:
    discord: DiscordConfig = field(default_factory=DiscordConfig)
    openclaw: OpenClawConfig = field(default_factory=OpenClawConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    stt: STTConfig = field(default_factory=STTConfig)
    wake_word: WakeWordConfig = field(default_factory=WakeWordConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
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
