"""WebSocket client for the Node.js voice bridge.

The bridge handles Discord voice I/O with DAVE E2EE support.
This module provides:
  - VoiceBridgeClient: manages the WebSocket connection and message routing
  - BridgeVoiceClient: a discord.VoiceProtocol that forwards voice credentials
    to the Node bridge instead of establishing its own voice connection
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import TYPE_CHECKING, Callable, Awaitable, Any

import websockets
from websockets.asyncio.client import ClientConnection

if TYPE_CHECKING:
    import discord

log = logging.getLogger(__name__)

# Type for the audio callback: (user_id, pcm_bytes_48k_stereo, guild_id)
AudioCallback = Callable[[int, bytes, str], Awaitable[None]]


class VoiceBridgeClient:
    """Manages the WebSocket connection to the Node.js voice bridge."""

    # Reconnection backoff parameters
    _RECONNECT_BASE = 2.0
    _RECONNECT_MAX = 60.0

    def __init__(self, url: str) -> None:
        self.url = url
        self._ws: ClientConnection | None = None
        self._connected = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._reconnect_attempts = 0
        # guild_id -> callback for incoming audio
        self._audio_callbacks: dict[str, AudioCallback] = {}
        # guild_id -> asyncio.Event for ready signal
        self._ready_events: dict[str, asyncio.Event] = {}
        # guild_id -> asyncio.Event for play_done signal
        self._play_done_events: dict[str, asyncio.Event] = {}
        # guild_id -> bool for DAVE status
        self._dave_status: dict[str, bool] = {}
        # guild_id -> asyncio.Event for disconnect signal
        self._disconnect_events: dict[str, asyncio.Event] = {}

    async def start(self) -> None:
        """Connect to the bridge and start the message loop."""
        self._task = asyncio.create_task(self._run(), name="voice-bridge-ws")

    async def stop(self) -> None:
        """Disconnect from the bridge."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._ws:
            await self._ws.close()
            self._ws = None
        self._connected.clear()

    async def wait_connected(self, timeout: float = 10.0) -> None:
        """Wait for the bridge WebSocket to be connected."""
        await asyncio.wait_for(self._connected.wait(), timeout=timeout)

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    async def _run(self) -> None:
        """Connection loop with exponential backoff reconnection."""
        while True:
            try:
                log.info("Connecting to voice bridge at %s", self.url)
                async with websockets.connect(self.url) as ws:
                    self._ws = ws
                    self._connected.set()
                    self._reconnect_attempts = 0
                    log.info("Connected to voice bridge")
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                            await self._handle_message(msg)
                        except json.JSONDecodeError:
                            log.warning("Invalid JSON from bridge: %s", raw[:200])
            except asyncio.CancelledError:
                raise
            except Exception:
                self._connected.clear()
                self._ws = None
                delay = min(
                    self._RECONNECT_BASE * (2 ** self._reconnect_attempts),
                    self._RECONNECT_MAX,
                )
                self._reconnect_attempts += 1
                log.warning(
                    "Voice bridge connection lost, reconnecting in %.0fs (attempt %d)...",
                    delay, self._reconnect_attempts, exc_info=True,
                )
                await asyncio.sleep(delay)

    async def _handle_message(self, msg: dict) -> None:
        """Route incoming messages from the bridge."""
        op = msg.get("op")
        guild_id = msg.get("guild_id", "")

        if op == "ready":
            self._dave_status[guild_id] = msg.get("dave", False)
            evt = self._ready_events.get(guild_id)
            if evt:
                evt.set()
            log.info(
                "Voice bridge ready for guild %s (DAVE=%s)",
                guild_id, self._dave_status.get(guild_id),
            )

        elif op == "audio":
            user_id = msg.get("user_id")
            pcm_b64 = msg.get("pcm", "")
            if pcm_b64 and guild_id in self._audio_callbacks:
                pcm = base64.b64decode(pcm_b64)
                try:
                    await self._audio_callbacks[guild_id](int(user_id), pcm, guild_id)
                except Exception:
                    log.exception("Error in audio callback for user %s", user_id)

        elif op == "play_done":
            evt = self._play_done_events.get(guild_id)
            if evt:
                evt.set()

        elif op == "disconnected":
            log.warning("Bridge reports voice disconnected for guild %s", guild_id)
            evt = self._disconnect_events.get(guild_id)
            if evt:
                evt.set()

        elif op == "error":
            log.error("Bridge error for guild %s: %s", guild_id, msg.get("message"))

    async def send(self, msg: dict) -> None:
        """Send a JSON message to the bridge.

        Raises ConnectionError if the bridge is not connected.
        """
        if not self._ws:
            raise ConnectionError("Voice bridge is not connected")
        await self._ws.send(json.dumps(msg))

    def register_audio_callback(self, guild_id: str, callback: AudioCallback) -> None:
        """Register a callback for incoming audio for a guild."""
        self._audio_callbacks[guild_id] = callback

    def unregister_audio_callback(self, guild_id: str) -> None:
        """Remove the audio callback for a guild."""
        self._audio_callbacks.pop(guild_id, None)

    async def join(
        self,
        guild_id: str,
        channel_id: str,
        user_id: str,
        session_id: str,
        timeout: float = 15.0,
    ) -> bool:
        """Request the bridge to join a voice channel. Returns True if ready."""
        evt = asyncio.Event()
        self._ready_events[guild_id] = evt

        await self.send({
            "op": "join",
            "guild_id": guild_id,
            "channel_id": channel_id,
            "user_id": user_id,
            "session_id": session_id,
        })

        # Don't wait for ready here -- we need to send voice credentials first
        return True

    async def wait_ready(self, guild_id: str, timeout: float = 15.0) -> bool:
        """Wait for the bridge to signal ready for a guild."""
        evt = self._ready_events.get(guild_id)
        if not evt:
            return False
        try:
            await asyncio.wait_for(evt.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            log.warning("Timed out waiting for bridge ready (guild %s)", guild_id)
            return False
        finally:
            self._ready_events.pop(guild_id, None)

    async def send_voice_state_update(self, data: dict) -> None:
        """Forward a voice_state_update event to the bridge."""
        await self.send({"op": "voice_state_update", "d": data})

    async def send_voice_server_update(self, data: dict) -> None:
        """Forward a voice_server_update event to the bridge."""
        await self.send({"op": "voice_server_update", "d": data})

    async def play(
        self,
        guild_id: str,
        audio_bytes: bytes,
        fmt: str = "wav",
        timeout: float = 120.0,
    ) -> None:
        """Play audio in the voice channel via the bridge."""
        evt = asyncio.Event()
        self._play_done_events[guild_id] = evt

        await self.send({
            "op": "play",
            "guild_id": guild_id,
            "audio": base64.b64encode(audio_bytes).decode("ascii"),
            "format": fmt,
        })

        try:
            await asyncio.wait_for(evt.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            log.warning("Playback timed out for guild %s", guild_id)
        finally:
            self._play_done_events.pop(guild_id, None)

    async def stop_playing(self, guild_id: str) -> None:
        """Stop current playback in a guild."""
        await self.send({"op": "stop", "guild_id": guild_id})

    async def disconnect(self, guild_id: str) -> None:
        """Disconnect from voice in a guild and clean up all state."""
        self._audio_callbacks.pop(guild_id, None)
        self._ready_events.pop(guild_id, None)
        self._play_done_events.pop(guild_id, None)
        self._disconnect_events.pop(guild_id, None)
        self._dave_status.pop(guild_id, None)
        try:
            await self.send({"op": "disconnect", "guild_id": guild_id})
        except ConnectionError:
            log.debug("Bridge not connected, skipping disconnect message")

    def is_dave_active(self, guild_id: str) -> bool:
        """Check if DAVE E2EE is active for a guild."""
        return self._dave_status.get(guild_id, False)

    @property
    def reconnect_attempts(self) -> int:
        """Number of consecutive reconnection attempts (0 when connected)."""
        return self._reconnect_attempts
