"""Entry point for the Discord Voice Assistant."""

from __future__ import annotations

import asyncio
import logging
import sys

from discord_voice_assistant.bot import VoiceAssistantBot
from discord_voice_assistant.config import Config


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Silence noisy libraries
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.gateway").setLevel(logging.WARNING)


def main() -> None:
    config = Config()

    setup_logging(config.log_level)
    log = logging.getLogger("discord_voice_assistant")

    errors = config.validate()
    if errors:
        for err in errors:
            log.error("Config error: %s", err)
        sys.exit(1)

    log.info("Starting Discord Voice Assistant v%s", "0.1.0")

    bot = VoiceAssistantBot(config)

    try:
        bot.run(config.discord.token)
    except KeyboardInterrupt:
        log.info("Shutting down...")


if __name__ == "__main__":
    main()
