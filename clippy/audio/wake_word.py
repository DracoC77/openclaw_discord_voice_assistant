"""Wake word detection using openWakeWord."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from clippy.config import WakeWordConfig

log = logging.getLogger(__name__)

# openWakeWord expects 16kHz 16-bit mono, in 80ms frames (1280 samples)
FRAME_SIZE = 1280


class WakeWordDetector:
    """Detects a wake word (e.g., 'clippy') in audio streams."""

    def __init__(self, config: WakeWordConfig) -> None:
        self.config = config
        self._model = None
        self._initialized = False

    def _ensure_model(self) -> bool:
        """Lazy-load the openWakeWord model."""
        if self._initialized:
            return self._model is not None

        self._initialized = True
        try:
            import openwakeword
            from openwakeword.model import Model

            # Download default models if needed
            openwakeword.utils.download_models()

            model_kwargs = {}
            if self.config.model_path:
                model_kwargs["wakeword_models"] = [self.config.model_path]
            # else: use all default models (includes "hey jarvis", etc.)
            # For custom "clippy" wake word, user provides a trained .tflite model

            self._model = Model(**model_kwargs)
            log.info(
                "Wake word detector loaded (threshold=%.2f, models=%s)",
                self.config.threshold,
                list(self._model.models.keys()) if self._model else "none",
            )
            return True
        except ImportError:
            log.warning(
                "openwakeword not installed. Wake word detection disabled. "
                "Install with: pip install openwakeword"
            )
            return False
        except Exception:
            log.exception("Failed to load wake word model")
            return False

    def detect(self, audio_data: bytes, sample_rate: int = 16000) -> bool:
        """Check if the wake word is present in an audio chunk.

        Args:
            audio_data: Raw 16-bit PCM audio bytes (16kHz mono)
            sample_rate: Sample rate (should be 16000)

        Returns:
            True if wake word detected above threshold.
        """
        if not self._ensure_model():
            # If wake word detection is unavailable, allow all audio through
            return True

        audio_np = np.frombuffer(audio_data, dtype=np.int16)

        # Process in 80ms frames as required by openWakeWord
        detected = False
        for i in range(0, len(audio_np) - FRAME_SIZE + 1, FRAME_SIZE):
            frame = audio_np[i : i + FRAME_SIZE]
            prediction = self._model.predict(frame)

            for model_name, score in prediction.items():
                if score >= self.config.threshold:
                    log.info(
                        "Wake word detected: %s (score=%.3f)", model_name, score
                    )
                    detected = True
                    self._model.reset()
                    return True

        return detected

    def reset(self) -> None:
        """Reset the detector state (call after wake word is consumed)."""
        if self._model:
            self._model.reset()
