"""Provider registry with automatic failover"""
from typing import Dict, Protocol, Optional
import logging

logger = logging.getLogger(__name__)


class STTProvider(Protocol):
    """Speech-to-Text provider protocol"""

    async def transcribe(self, audio: bytes) -> str:
        """Transcribe audio bytes to text"""
        ...


class TTSProvider(Protocol):
    """Text-to-Speech provider protocol"""

    async def synthesize(self, text: str, voice: Optional[str] = None) -> bytes:
        """Convert text to speech audio bytes with optional voice override"""
        ...


class ProviderRegistry:
    """Registry for STT/TTS providers with automatic failover"""

    def __init__(self):
        self.stt_providers: Dict[str, STTProvider] = {}
        self.tts_providers: Dict[str, TTSProvider] = {}

    def register_stt(self, name: str, provider: STTProvider):
        """Register Speech-to-Text provider"""
        self.stt_providers[name] = provider
        logger.info(f"Registered STT provider: {name}")

    def register_tts(self, name: str, provider: TTSProvider):
        """Register Text-to-Speech provider"""
        self.tts_providers[name] = provider
        logger.info(f"Registered TTS provider: {name}")

    async def transcribe(
        self,
        audio: bytes,
        preferred: str = "whisper"
    ) -> str:
        """
        Transcribe audio with automatic failover.

        Args:
            audio: Audio bytes to transcribe
            preferred: Preferred provider name

        Returns:
            Transcribed text

        Raises:
            RuntimeError: If all providers fail
        """
        # Try preferred first, then others
        providers_to_try = [preferred] + [
            k for k in self.stt_providers.keys() if k != preferred
        ]

        for name in providers_to_try:
            if name not in self.stt_providers:
                continue

            try:
                logger.debug(f"Trying STT provider: {name}")
                result = await self.stt_providers[name].transcribe(audio)
                logger.info(f"STT success with {name}: {result[:50]}...")
                return result
            except Exception as e:
                logger.warning(f"STT {name} failed: {e}")
                continue

        raise RuntimeError("All STT providers failed")

    async def synthesize(
        self,
        text: str,
        preferred: str = "kokoro",
        fallback: str = "piper",
        voice: Optional[str] = None
    ) -> bytes:
        """
        Synthesize speech with automatic failover.

        Args:
            text: Text to synthesize
            preferred: Preferred provider name
            fallback: Fallback provider name
            voice: Optional voice to use (provider-specific)

        Returns:
            Audio bytes

        Raises:
            RuntimeError: If all providers fail
        """
        providers_to_try = [preferred]
        if fallback and fallback != preferred:
            providers_to_try.append(fallback)

        for name in providers_to_try:
            if name not in self.tts_providers:
                continue

            try:
                logger.debug(f"Trying TTS provider: {name}")
                result = await self.tts_providers[name].synthesize(text, voice)
                logger.info(f"TTS success with {name} ({len(result)} bytes)")
                return result
            except Exception as e:
                logger.warning(f"TTS {name} failed: {e}, trying next")
                continue

        raise RuntimeError(f"All TTS providers failed for text: {text[:50]}...")