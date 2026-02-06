"""OpenClaw integration for managing AI conversation sessions."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import TYPE_CHECKING

import aiohttp

if TYPE_CHECKING:
    from clippy.config import OpenClawConfig

log = logging.getLogger(__name__)


class OpenClawClient:
    """Client for communicating with an OpenClaw instance.

    OpenClaw exposes a gateway API that manages agent sessions. This client
    creates sessions, sends messages, and receives responses.

    The API supports multiple connection modes:
    - REST API (default): POST messages, GET responses
    - WebSocket: Real-time bidirectional streaming (for lower latency)
    """

    def __init__(self, config: OpenClawConfig) -> None:
        self.config = config
        self.base_url = config.url.rstrip("/")
        self._session: aiohttp.ClientSession | None = None
        self._headers: dict[str, str] = {"Content-Type": "application/json"}
        if config.api_key:
            self._headers["Authorization"] = f"Bearer {config.api_key}"

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers=self._headers)
        return self._session

    async def create_session(self, context: str = "") -> str:
        """Create a new conversation session with the OpenClaw agent.

        Args:
            context: Session context identifier (e.g., "discord:voice:guild:channel")

        Returns:
            Session ID string.
        """
        session_id = str(uuid.uuid4())

        try:
            http = await self._get_session()
            async with http.post(
                f"{self.base_url}/api/v1/sessions",
                json={
                    "agentId": self.config.agent_id,
                    "sessionId": session_id,
                    "context": context,
                    "metadata": {
                        "source": "discord-voice",
                        "interface": "voice",
                    },
                },
            ) as resp:
                if resp.status in (200, 201):
                    data = await resp.json()
                    session_id = data.get("sessionId", session_id)
                    log.info("Created OpenClaw session: %s", session_id)
                elif resp.status == 404:
                    # API endpoint not found - OpenClaw may use a different API format
                    log.warning(
                        "OpenClaw session API not found (404). "
                        "Using local session management. "
                        "Ensure your OpenClaw instance supports the gateway API."
                    )
                else:
                    text = await resp.text()
                    log.warning(
                        "OpenClaw session creation returned %d: %s",
                        resp.status,
                        text[:200],
                    )
        except aiohttp.ClientError as e:
            log.warning("Could not connect to OpenClaw at %s: %s", self.base_url, e)
            log.info("Using local session ID: %s", session_id)

        return session_id

    async def send_message(
        self,
        session_id: str,
        text: str,
        sender_name: str = "User",
        sender_id: str = "",
    ) -> str:
        """Send a message to the OpenClaw agent and get a response.

        Args:
            session_id: The session ID from create_session()
            text: The user's message text
            sender_name: Display name of the sender
            sender_id: Unique ID of the sender

        Returns:
            The agent's response text, or empty string on failure.
        """
        try:
            http = await self._get_session()
            payload = {
                "sessionId": session_id,
                "message": text,
                "sender": {
                    "name": sender_name,
                    "id": sender_id,
                },
            }

            async with http.post(
                f"{self.base_url}/api/v1/chat",
                json=payload,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("response", data.get("message", ""))
                else:
                    text_resp = await resp.text()
                    log.warning(
                        "OpenClaw chat returned %d: %s",
                        resp.status,
                        text_resp[:200],
                    )
                    return ""
        except aiohttp.ClientError as e:
            log.error("Failed to communicate with OpenClaw: %s", e)
            return ""

    async def send_message_stream(
        self,
        session_id: str,
        text: str,
        sender_name: str = "User",
        sender_id: str = "",
    ):
        """Send a message and stream back the response token by token.

        Yields:
            Response text chunks as they arrive.
        """
        try:
            http = await self._get_session()
            payload = {
                "sessionId": session_id,
                "message": text,
                "stream": True,
                "sender": {
                    "name": sender_name,
                    "id": sender_id,
                },
            }

            async with http.post(
                f"{self.base_url}/api/v1/chat",
                json=payload,
            ) as resp:
                if resp.status == 200:
                    async for line in resp.content:
                        line = line.decode("utf-8").strip()
                        if line.startswith("data: "):
                            data = json.loads(line[6:])
                            chunk = data.get("text", "")
                            if chunk:
                                yield chunk
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
        """End a conversation session."""
        try:
            http = await self._get_session()
            async with http.delete(
                f"{self.base_url}/api/v1/sessions/{session_id}",
            ) as resp:
                if resp.status in (200, 204, 404):
                    log.info("Ended OpenClaw session: %s", session_id)
                else:
                    log.warning("Failed to end session %s: %d", session_id, resp.status)
        except aiohttp.ClientError as e:
            log.warning("Error ending session: %s", e)

    async def close(self) -> None:
        """Clean up HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
