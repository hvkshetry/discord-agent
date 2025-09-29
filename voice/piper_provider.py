"""Piper TTS provider implementation (fallback)"""
import asyncio
import numpy as np
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional
import logging

logger = logging.getLogger(__name__)


class PiperProvider:
    """Piper TTS provider (fallback for Kokoro)"""

    def __init__(self, voice_path: str):
        """
        Initialize Piper provider.

        Args:
            voice_path: Path to Piper voice model (.onnx file)
        """
        self.voice = None  # Lazy load
        self.voice_path = Path(voice_path)
        self.executor = ThreadPoolExecutor(max_workers=1)
        logger.info(f"Initialized Piper provider (voice={voice_path})")

    def _ensure_loaded(self):
        """Lazy load Piper voice"""
        if self.voice is None:
            try:
                from piper import PiperVoice
                logger.info(f"Loading Piper voice: {self.voice_path}")

                if not self.voice_path.exists():
                    raise FileNotFoundError(f"Piper voice not found: {self.voice_path}")

                self.voice = PiperVoice.load(str(self.voice_path))
                logger.info("Piper voice loaded successfully")
            except Exception as e:
                logger.error(f"Failed to load Piper: {e}")
                raise

    async def synthesize(self, text: str, voice: Optional[str] = None) -> bytes:
        """
        Convert text to speech audio.

        Args:
            text: Text to synthesize
            voice: Optional voice to use (ignored, Piper uses fixed voice)

        Returns:
            Audio bytes (16-bit PCM, 22050Hz)
        """
        if not text.strip():
            logger.warning("Empty text provided to Piper")
            return b""

        self._ensure_loaded()

        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(
                self.executor,
                self._synthesize_sync,
                text,
                voice
            )
        except Exception as e:
            logger.error(f"Piper synthesis failed: {e}")
            raise

    def _synthesize_sync(self, text: str, voice: Optional[str] = None) -> bytes:
        """
        Synchronous synthesis (runs in thread pool).

        Args:
            text: Text to synthesize
            voice: Optional voice to use (ignored, Piper uses fixed voice)

        Returns:
            Audio bytes (16-bit PCM)
        """
        try:
            # Piper synthesize returns audio array
            audio_data = self.voice.synthesize(text, rate=1.0)

            # Ensure it's in the right format
            if isinstance(audio_data, np.ndarray):
                # Convert to 16-bit PCM if needed
                if audio_data.dtype != np.int16:
                    audio_data = (audio_data * 32767).astype(np.int16)
                return audio_data.tobytes()
            else:
                # Already bytes
                return audio_data

        except Exception as e:
            logger.error(f"Piper synthesis error: {e}")
            raise

    def __del__(self):
        """Cleanup executor on deletion"""
        if hasattr(self, 'executor'):
            self.executor.shutdown(wait=False)