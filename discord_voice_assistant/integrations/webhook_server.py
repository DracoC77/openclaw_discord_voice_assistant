"""HTTP webhook server for receiving proactive voice messages.

Exposes a ``POST /speak`` endpoint that accepts messages from OpenClaw
(via plugin tools, cron webhook delivery, or external automations) and
routes them through the voice pipeline for live playback, voicemail
delivery, or user notification.

Start the server with :meth:`WebhookServer.start` after the bot is ready.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from discord_voice_assistant.bot import VoiceAssistantBot
    from discord_voice_assistant.config import Config

log = logging.getLogger(__name__)


class WebhookServer:
    """Lightweight aiohttp server that receives proactive voice messages."""

    def __init__(self, bot: VoiceAssistantBot, config: Config) -> None:
        self.bot = bot
        self.config = config
        self._app = web.Application(middlewares=[self._auth_middleware])
        self._app.router.add_post("/speak", self._handle_speak)
        self._app.router.add_get("/health", self._handle_health)
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    # -- Middleware ----------------------------------------------------------------

    @web.middleware
    async def _auth_middleware(
        self, request: web.Request, handler
    ) -> web.StreamResponse:
        if request.path == "/health":
            return await handler(request)

        token = self.config.webhook.token
        if token:
            auth = request.headers.get("Authorization", "")
            if not auth.startswith("Bearer ") or auth[7:] != token:
                log.warning(
                    "Webhook auth failure from %s", request.remote,
                )
                return web.json_response({"error": "unauthorized"}, status=401)

        return await handler(request)

    # -- Handlers -----------------------------------------------------------------

    async def _handle_speak(self, request: web.Request) -> web.Response:
        """Handle POST /speak — route a proactive message to the voice pipeline."""
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)

        text = self._extract_text(data)
        if not text:
            return web.json_response({"error": "no text provided"}, status=400)

        mode = data.get("mode", self.config.webhook.default_mode)
        if mode not in ("live", "voicemail", "notify", "auto"):
            return web.json_response(
                {"error": f"invalid mode: {mode}"}, status=400,
            )

        priority_str = data.get("priority", "normal")
        priority = 0 if priority_str == "urgent" else 1

        guild_id = int(data["guild_id"]) if data.get("guild_id") else None
        channel_id = int(data["channel_id"]) if data.get("channel_id") else None
        user_id = int(data["user_id"]) if data.get("user_id") else None

        log.info(
            "Webhook /speak: mode=%s priority=%s guild=%s user=%s text=%s",
            mode, priority_str, guild_id, user_id, text[:80],
        )

        result = await self.bot.voice_manager.handle_proactive_message(
            text=text,
            mode=mode,
            priority=priority,
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
        )

        status = 200 if result.get("status") == "ok" else 422
        return web.json_response(result, status=status)

    async def _handle_health(self, request: web.Request) -> web.Response:
        """Handle GET /health — basic liveness check."""
        sessions = self.bot.voice_manager.session_count
        return web.json_response({
            "status": "ok",
            "active_sessions": sessions,
            "webhook_enabled": self.config.webhook.enabled,
        })

    # -- Helpers ------------------------------------------------------------------

    @staticmethod
    def _extract_text(data: dict) -> str:
        """Extract the message text from various payload formats.

        Handles the direct format (``{"text": "..."}``), OpenClaw cron
        webhook delivery (``{"payload": {"summary": "..."}}``), and
        nested message formats.
        """
        # Direct format from plugin tool
        if "text" in data:
            return str(data["text"]).strip()

        # OpenClaw cron webhook delivery
        payload = data.get("payload", {})
        if isinstance(payload, dict):
            for key in ("summary", "text", "content", "message"):
                if key in payload and payload[key]:
                    return str(payload[key]).strip()

        # Nested message field
        if "message" in data:
            return str(data["message"]).strip()

        return ""

    # -- Lifecycle ----------------------------------------------------------------

    async def start(self) -> None:
        """Start the webhook HTTP server."""
        if not self.config.webhook.token:
            log.warning(
                "Webhook server starting WITHOUT authentication "
                "(set WEBHOOK_TOKEN for security)"
            )

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(
            self._runner, "0.0.0.0", self.config.webhook.port,
        )
        await self._site.start()
        log.info("Webhook server started on port %d", self.config.webhook.port)

    async def stop(self) -> None:
        """Stop the webhook HTTP server."""
        if self._site:
            await self._site.stop()
        if self._runner:
            await self._runner.cleanup()
        log.info("Webhook server stopped")
