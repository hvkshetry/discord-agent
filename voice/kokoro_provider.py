"""Kokoro TTS provider implementation"""
import asyncio
import numpy as np
from concurrent.futures import ThreadPoolExecutor
from typing import Optional
import logging

logger = logging.getLogger(__name__)


class KokoroProvider:
    """Kokoro TTS provider with async interface"""

    def __init__(self, voice: str = "af_sky", lang_code: str = "a"):
        """
        Initialize Kokoro provider.

        Args:
            voice: Voice name (af_sky, af_sarah, af_heart, am_adam, etc.)
            lang_code: Language code ('a' for American English)
        """
        self.pipeline = None  # Lazy load on first use
        self.voice = voice
        self.lang_code = lang_code
        self.executor = ThreadPoolExecutor(max_workers=1)
        self._load_lock = asyncio.Lock()
        logger.info(f"Initialized Kokoro provider (voice={voice}, lang={lang_code})")

    def _ensure_loaded(self):
        """Lazy load Kokoro pipeline"""
        if self.pipeline is None:
            try:
                from kokoro_onnx import Kokoro
                import os
                logger.info(f"Loading Kokoro pipeline (CPU mode)...")

                # Look for model files in common locations
                model_paths = [
                    ("kokoro-v1.0.onnx", "voices-v1.0.bin"),  # Current directory
                    (os.path.expanduser("~/.cache/kokoro/kokoro-v1.0.onnx"),
                     os.path.expanduser("~/.cache/kokoro/voices-v1.0.bin")),
                    ("/tmp/kokoro/kokoro-v1.0.onnx", "/tmp/kokoro/voices-v1.0.bin")
                ]

                model_file = None
                voices_file = None
                for m, v in model_paths:
                    if os.path.exists(m) and os.path.exists(v):
                        model_file, voices_file = m, v
                        break

                if not model_file:
                    raise FileNotFoundError(
                        "Kokoro model files not found. Download them from:\n"
                        "  https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx\n"
                        "  https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin"
                    )

                # Initialize with CPU (default for kokoro-onnx)
                self.pipeline = Kokoro(model_file, voices_file)
                logger.info("Kokoro pipeline loaded successfully (CPU mode)")
            except Exception as e:
                logger.error(f"Failed to load Kokoro: {e}")
                raise

    async def _ensure_loaded_async(self):
        """Async wrapper for model loading - offloads to thread pool"""
        async with self._load_lock:
            if self.pipeline is not None:
                return

            loop = asyncio.get_event_loop()
            await loop.run_in_executor(self.executor, self._ensure_loaded)

    async def preload(self):
        """Pre-load model at startup to avoid first-call latency"""
        await self._ensure_loaded_async()
        logger.info("Kokoro pipeline pre-loaded")

    async def synthesize(self, text: str, voice: Optional[str] = None) -> bytes:
        """
        Convert text to speech audio.

        Args:
            text: Text to synthesize
            voice: Optional voice to use (overrides default)

        Returns:
            Audio bytes (16-bit PCM, 24kHz)
        """
        if not text.strip():
            logger.warning("Empty text provided to Kokoro")
            return b""

        await self._ensure_loaded_async()

        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(
                self.executor,
                self._synthesize_sync,
                text,
                voice
            )
        except Exception as e:
            logger.error(f"Kokoro synthesis failed: {e}")
            raise

    def _synthesize_sync(self, text: str, voice: Optional[str] = None) -> bytes:
        """
        Synchronous synthesis (runs in thread pool).

        Args:
            text: Text to synthesize
            voice: Optional voice to use (overrides default)

        Returns:
            Audio bytes (16-bit PCM, 24kHz)
        """
        try:
            # Kokoro-ONNX API: create(text, voice, speed, lang)
            # Map lang_code to full language code
            lang_map = {
                "a": "en-us",  # American English
                "b": "en-gb",  # British English
                "j": "ja",     # Japanese
                "k": "ko",     # Korean
                "z": "zh"      # Chinese
            }
            lang = lang_map.get(self.lang_code, "en-us")

            # Generate audio
            samples, sample_rate = self.pipeline.create(
                text,
                voice=voice or self.voice,  # Use provided voice or default
                speed=1.0,
                lang=lang
            )

            # samples is already numpy array, convert to 16-bit PCM
            if samples.dtype != np.int16:
                # If float32, convert to int16
                if samples.dtype == np.float32:
                    audio_int16 = (samples * 32767).astype(np.int16)
                else:
                    audio_int16 = samples.astype(np.int16)
            else:
                audio_int16 = samples

            return audio_int16.tobytes()

        except Exception as e:
            logger.error(f"Kokoro synthesis error: {e}")
            raise

    def __del__(self):
        """Cleanup executor on deletion"""
        if hasattr(self, 'executor'):
            self.executor.shutdown(wait=False)