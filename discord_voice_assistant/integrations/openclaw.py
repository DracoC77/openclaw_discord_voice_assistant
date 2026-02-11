"""OpenClaw integration using the OpenAI-compatible Chat Completions API.

OpenClaw's Gateway exposes an OpenAI-compatible endpoint at /v1/chat/completions.
This is disabled by default and must be enabled in openclaw.json:

    { "gateway": { "bind": "lan" } }

Authentication is required when bind != "loopback":

    { "gateway": { "auth": { "token": "your-secret-token" } } }

The default gateway port is 18789 (not 3000).
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import TYPE_CHECKING

import aiohttp

if TYPE_CHECKING:
    from discord_voice_assistant.config import OpenClawConfig

log = logging.getLogger(__name__)


class OpenClawClient:
    """Client for communicating with an OpenClaw instance.

    Uses the OpenAI-compatible /v1/chat/completions endpoint. Session
    persistence is achieved via the `user` field, which OpenClaw uses
    to derive a stable session key for the same agent+user pair.
    """

    def __init__(self, config: OpenClawConfig) -> None:
        self.config = config
        self.base_url = config.url.rstrip("/")
        self._http: aiohttp.ClientSession | None = None

        self._headers: dict[str, str] = {"Content-Type": "application/json"}
        if config.api_key:
            self._headers["Authorization"] = f"Bearer {config.api_key}"

    async def _get_http(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(headers=self._headers)
        return self._http

    async def create_session(self, context: str = "") -> str:
        """Create a session identifier.

        OpenClaw doesn't have an explicit session creation endpoint.
        Instead, the `user` field in /v1/chat/completions determines
        the session key. We generate a unique ID here and pass it as
        `user` on every request to maintain conversation continuity.
        """
        session_id = f"voice:{context}:{uuid.uuid4().hex[:12]}"
        log.info("Created session ID: %s", session_id)
        return session_id

    async def send_message(
        self,
        session_id: str,
        text: str,
        sender_name: str = "User",
        sender_id: str = "",
    ) -> str:
        """Send a message to the OpenClaw agent and get a response.

        Uses the OpenAI-compatible /v1/chat/completions endpoint.
        The `user` field provides session continuity, and the
        `x-openclaw-agent-id` header routes to the correct agent.

        Args:
            session_id: Session ID from create_session() (used as `user` field)
            text: The user's message text
            sender_name: Display name of the sender
            sender_id: Unique ID of the sender

        Returns:
            The agent's response text, or empty string on failure.
        """
        try:
            http = await self._get_http()

            # Prefix the message with the speaker's name for multi-user context
            content = f"[{sender_name}]: {text}" if sender_name else text

            # Voice instruction is embedded in the user message because OpenClaw's
            # agent has its own system prompt that overrides any system message we send.
            voice_instruction = (
                "(You are responding via voice in a Discord voice channel. "
                "Keep your reply to 1-3 short spoken sentences. "
                "Do NOT use markdown, bullet points, numbered lists, or emoji. "
                "Reply in plain, natural, conversational speech.) "
            )

            payload = {
                "model": "openclaw",
                "messages": [
                    {"role": "user", "content": voice_instruction + content},
                ],
                "user": session_id,
            }

            headers = {}
            if self.config.agent_id and self.config.agent_id != "default":
                headers["x-openclaw-agent-id"] = self.config.agent_id

            async with http.post(
                f"{self.base_url}/v1/chat/completions",
                json=payload,
                headers=headers,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    # OpenAI format: choices[0].message.content
                    choices = data.get("choices", [])
                    if choices:
                        return choices[0].get("message", {}).get("content", "")
                    return ""
                elif resp.status == 401:
                    log.error(
                        "OpenClaw authentication failed (401). "
                        "Set OPENCLAW_API_KEY to your gateway token "
                        "(OPENCLAW_GATEWAY_TOKEN on the OpenClaw side)."
                    )
                    return ""
                elif resp.status == 404:
                    log.error(
                        "OpenClaw /v1/chat/completions not found (404). "
                        "Ensure the HTTP API is enabled: set gateway.bind to 'lan' "
                        "in openclaw.json or OPENCLAW_GATEWAY_BIND=lan."
                    )
                    return ""
                else:
                    text_resp = await resp.text()
                    log.warning(
                        "OpenClaw returned %d: %s", resp.status, text_resp[:200]
                    )
                    return ""
        except aiohttp.ClientError as e:
            log.error("Failed to communicate with OpenClaw at %s: %s", self.base_url, e)
            return ""

    async def send_message_stream(
        self,
        session_id: str,
        text: str,
        sender_name: str = "User",
        sender_id: str = "",
    ):
        """Send a message and stream the response via SSE.

        Yields:
            Response text chunks as they arrive.
        """
        try:
            http = await self._get_http()

            content = f"[{sender_name}]: {text}" if sender_name else text

            voice_instruction = (
                "(You are responding via voice in a Discord voice channel. "
                "Keep your reply to 1-3 short spoken sentences. "
                "Do NOT use markdown, bullet points, numbered lists, or emoji. "
                "Reply in plain, natural, conversational speech.) "
            )

            payload = {
                "model": "openclaw",
                "messages": [
                    {"role": "user", "content": voice_instruction + content},
                ],
                "user": session_id,
                "stream": True,
            }

            headers = {}
            if self.config.agent_id and self.config.agent_id != "default":
                headers["x-openclaw-agent-id"] = self.config.agent_id

            async with http.post(
                f"{self.base_url}/v1/chat/completions",
                json=payload,
                headers=headers,
            ) as resp:
                if resp.status == 200:
                    async for line in resp.content:
                        line = line.decode("utf-8").strip()
                        if not line or not line.startswith("data: "):
                            continue
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            break
                        try:
                            data = json.loads(data_str)
                            delta = (
                                data.get("choices", [{}])[0]
                                .get("delta", {})
                                .get("content", "")
                            )
                            if delta:
                                yield delta
                        except (json.JSONDecodeError, IndexError):
                            continue
                else:
                    text_resp = await resp.text()
                    log.warning(
                        "OpenClaw stream returned %d: %s",
                        resp.status,
                        text_resp[:200],
                    )
        except aiohttp.ClientError as e:
            log.error("Failed to stream from OpenClaw: %s", e)

    async def end_session(self, session_id: str) -> None:
        """End a conversation session.

        OpenClaw doesn't have an explicit session teardown endpoint.
        Sessions are managed internally by the gateway. This is a no-op
        but kept for interface consistency.
        """
        log.info("Session ended: %s", session_id)

    async def close(self) -> None:
        """Clean up HTTP session."""
        if self._http and not self._http.closed:
            await self._http.close()
