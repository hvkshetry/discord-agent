"""Whisper STT provider implementation"""
import asyncio
import numpy as np
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Optional
import logging
import io

logger = logging.getLogger(__name__)


class WhisperProvider:
    """Whisper Speech-to-Text provider"""

    def __init__(self, model: str = "small"):
        """
        Initialize Whisper provider.

        Args:
            model: Model size (tiny, base, small, medium, large)
        """
        self.model_obj = None  # Lazy load
        self.model_name = model
        self.executor = ThreadPoolExecutor(max_workers=1)
        self._load_lock = asyncio.Lock()
        self._load_task: Optional[asyncio.Task] = None
        logger.info(f"Initialized Whisper provider (model={model})")

    def _ensure_loaded(self):
        """Lazy load Whisper model (synchronous - runs in thread)"""
        if self.model_obj is None:
            try:
                from faster_whisper import WhisperModel
                logger.info(f"Loading Whisper model: {self.model_name}")

                model_name = self.model_name
                if not model_name.endswith('.en'):
                    model_name = f"{model_name}.en"

                self.model_obj = WhisperModel(
                    model_name,
                    device="cpu",
                    compute_type="int8",
                    cpu_threads=os.cpu_count()
                )
                logger.info(f"Whisper model loaded successfully: {model_name}")
            except Exception as e:
                logger.error(f"Failed to load Whisper: {e}")
                raise

    async def _ensure_loaded_async(self):
        """Async wrapper for model loading - offloads to thread pool"""
        async with self._load_lock:
            if self.model_obj is not None:
                return

            loop = asyncio.get_event_loop()
            await loop.run_in_executor(self.executor, self._ensure_loaded)

    async def preload(self):
        """Pre-load model at startup to avoid first-call latency"""
        await self._ensure_loaded_async()
        logger.info("Whisper model pre-loaded")

    async def transcribe(self, audio: bytes) -> str:
        """
        Transcribe audio bytes to text.

        Args:
            audio: Audio bytes (should be 16kHz PCM)

        Returns:
            Transcribed text
        """
        if not audio:
            logger.warning("Empty audio provided to Whisper")
            return ""

        await self._ensure_loaded_async()

        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(
                self.executor,
                self._transcribe_sync,
                audio
            )
        except Exception as e:
            logger.error(f"Whisper transcription failed: {e}")
            raise

    def _transcribe_sync(self, audio: bytes) -> str:
        """
        Synchronous transcription (runs in thread pool).

        Args:
            audio: Audio bytes

        Returns:
            Transcribed text
        """
        try:
            # Convert bytes to numpy array
            # Assuming 16-bit PCM
            audio_np = np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768.0

            # Transcribe (VAD already done by WebRTC at capture)
            segments, info = self.model_obj.transcribe(
                audio_np,
                language="en",
                beam_size=1,
                best_of=1,
                temperature=0.0,
                condition_on_previous_text=False
            )

            # Combine all segments
            text = " ".join([seg.text.strip() for seg in segments])

            logger.info(f"Whisper transcription: {text[:100]}...")
            return text.strip()

        except Exception as e:
            logger.error(f"Whisper transcription error: {e}")
            raise

    def __del__(self):
        """Cleanup executor on deletion"""
        if hasattr(self, 'executor'):
            self.executor.shutdown(wait=False)