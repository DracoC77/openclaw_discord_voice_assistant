"""Voice identification using speaker embeddings (Resemblyzer)."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

log = logging.getLogger(__name__)

# Cosine similarity threshold for voice verification
VERIFICATION_THRESHOLD = 0.75


class VoiceIdentifier:
    """Identifies and verifies speakers using voice embeddings.

    Uses Resemblyzer to compute speaker embeddings from audio, then
    compares them against stored voice profiles for authorized users.
    """

    def __init__(self, profiles_dir: Path) -> None:
        self.profiles_dir = profiles_dir
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        self._encoder = None
        self._profiles: dict[int, np.ndarray] = {}
        self._initialized = False

    def _ensure_encoder(self) -> bool:
        """Lazy-load the Resemblyzer encoder."""
        if self._initialized:
            return self._encoder is not None

        self._initialized = True
        try:
            from resemblyzer import VoiceEncoder

            self._encoder = VoiceEncoder()
            self._load_profiles()
            log.info(
                "Voice encoder loaded, %d profile(s) available",
                len(self._profiles),
            )
            return True
        except ImportError:
            log.warning(
                "resemblyzer not installed. Voice identification disabled. "
                "Install with: pip install resemblyzer"
            )
            return False
        except Exception:
            log.exception("Failed to load voice encoder")
            return False

    def _load_profiles(self) -> None:
        """Load saved voice profiles from disk."""
        for profile_path in self.profiles_dir.glob("*.npy"):
            try:
                user_id = int(profile_path.stem)
                self._profiles[user_id] = np.load(profile_path, allow_pickle=False)
            except (ValueError, Exception) as e:
                log.warning("Failed to load voice profile %s: %s", profile_path, e)

    async def warm_up(self) -> None:
        """Pre-load the voice encoder so the first verification isn't delayed."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._ensure_encoder)

    async def enroll(self, user_id: int, audio_data: bytes, sample_rate: int = 16000) -> bool:
        """Enroll a user's voice profile from an audio sample.

        Args:
            user_id: Discord user ID
            audio_data: Raw 16-bit PCM audio (16kHz mono)
            sample_rate: Sample rate

        Returns:
            True if enrollment succeeded.
        """
        if not self._ensure_encoder():
            return False

        loop = asyncio.get_running_loop()
        embedding = await loop.run_in_executor(
            None, self._compute_embedding, audio_data, sample_rate
        )

        if embedding is None:
            return False

        # Average with existing profile if one exists (incremental enrollment)
        if user_id in self._profiles:
            old = self._profiles[user_id]
            embedding = (old + embedding) / 2
            embedding = embedding / np.linalg.norm(embedding)

        self._profiles[user_id] = embedding

        # Save to disk
        profile_path = self.profiles_dir / f"{user_id}.npy"
        np.save(profile_path, embedding)
        log.info("Enrolled voice profile for user %d", user_id)
        return True

    async def verify(
        self, user_id: int, audio_data: bytes, sample_rate: int = 16000
    ) -> bool | None:
        """Verify if an audio sample matches a user's voice profile.

        Args:
            user_id: Discord user ID to verify against
            audio_data: Raw 16-bit PCM audio (16kHz mono)
            sample_rate: Sample rate

        Returns:
            True if verified, False if rejected, None if no profile exists.
        """
        if not self._ensure_encoder():
            return None

        if user_id not in self._profiles:
            return None  # No profile to compare against

        loop = asyncio.get_running_loop()
        embedding = await loop.run_in_executor(
            None, self._compute_embedding, audio_data, sample_rate
        )

        if embedding is None:
            return None

        # Cosine similarity
        similarity = float(np.dot(self._profiles[user_id], embedding))
        log.debug("Voice verification for user %d: similarity=%.3f", user_id, similarity)

        return similarity >= VERIFICATION_THRESHOLD

    def _compute_embedding(
        self, audio_data: bytes, sample_rate: int
    ) -> np.ndarray | None:
        """Compute a speaker embedding from audio data."""
        from resemblyzer import preprocess_wav

        audio_np = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0

        if len(audio_np) < sample_rate:  # Need at least 1 second
            return None

        # Resemblyzer expects float32 in [-1, 1]
        processed = preprocess_wav(audio_np, source_sr=sample_rate)
        if len(processed) == 0:
            return None

        embedding = self._encoder.embed_utterance(processed)
        return embedding

    def has_profile(self, user_id: int) -> bool:
        """Check if a user has a voice profile."""
        return user_id in self._profiles

    def delete_profile(self, user_id: int) -> bool:
        """Delete a user's voice profile."""
        if user_id in self._profiles:
            del self._profiles[user_id]
            profile_path = self.profiles_dir / f"{user_id}.npy"
            profile_path.unlink(missing_ok=True)
            return True
        return False
