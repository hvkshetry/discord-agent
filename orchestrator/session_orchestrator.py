"""Session orchestrator - routes events to agents and manages lifecycles"""
import asyncio
import logging
import time
import numpy as np
from typing import Dict, Optional, List
from pathlib import Path

from core.events import (
    Event, TextInputEvent, VoiceInputEvent, CommandEvent,
    AgentResponseEvent, ErrorEvent, StatusEvent, VoiceOutputEvent,
    VoiceConnectionEvent, Attachment
)
from core.bus import EventBus
from core.profiles import ProfileLoader, ChannelProfile
from engines.protocol import AgentEngine
from engines.codex_engine import CodexEngine
from engines.claude_engine import ClaudeEngine
from state.persistence import SessionDB
from state.transcript import TranscriptStore
from voice.providers import ProviderRegistry

logger = logging.getLogger(__name__)


class SessionOrchestrator:
    """
    Orchestrates agent sessions for channels.

    Responsibilities:
    - Route events to correct agent based on channel profile
    - Manage CLI engine lifecycle (spawn, reuse, cleanup)
    - Handle voice-to-text conversion
    - Store session state and transcripts
    - Maintain per-channel locks for engine access
    """

    def __init__(
        self,
        event_bus: EventBus,
        profile_loader: ProfileLoader,
        session_db: SessionDB,
        transcript_store: TranscriptStore,
        provider_registry: ProviderRegistry
    ):
        self.bus = event_bus
        self.profiles = profile_loader
        self.session_db = session_db
        self.transcripts = transcript_store
        self.voice_providers = provider_registry

        # Per-channel engine instances
        self.engines: Dict[str, AgentEngine] = {}

        # Per-channel locks to prevent concurrent CLI access
        self.channel_locks: Dict[str, asyncio.Lock] = {}

        # Codex conversation IDs for multi-turn context
        self.conversation_ids: Dict[str, str] = {}

        # Voice streaming state
        self.voice_buffers: Dict[str, str] = {}
        self.voice_spoken: Dict[str, str] = {}
        self.last_voice_time: Dict[str, float] = {}
        self.flush_tasks: Dict[str, asyncio.Task] = {}

        # Voice channel connection state
        self.voice_active_channels: Dict[str, float] = {}

        # Track which channels have received voice optimization prompt
        self.voice_prompted_sessions: set = set()

    def subscribe_to_events(self):
        """Register event handlers"""
        self.bus.subscribe("text_input", self._handle_text_input)
        self.bus.subscribe("voice_input", self._handle_voice_input)
        self.bus.subscribe("command", self._handle_command)
        self.bus.subscribe("voice_connection", self._handle_voice_connection)
        logger.info("Session orchestrator subscribed to events")

    async def _handle_text_input(self, event: TextInputEvent):
        """Handle text message from user"""
        try:
            profile = self.profiles.get_profile(event.channel_id)
            if not profile:
                await self.bus.publish(ErrorEvent(
                    channel_id=event.channel_id,
                    message=f"No profile configured for channel: {event.channel_id}",
                    error_type="ConfigurationError"
                ))
                return

            # Record to transcript if enabled
            if profile.session.transcript_store:
                await self.transcripts.append(event.channel_id, event)

            # Update session activity
            await self.session_db.update_activity(event.channel_id)

            # Process with agent
            await self._process_with_agent(
                event.channel_id,
                event.content,
                profile,
                attachments=event.attachments
            )

        except Exception as e:
            logger.error(f"Error handling text input: {e}", exc_info=True)
            await self.bus.publish(ErrorEvent(
                channel_id=event.channel_id,
                message=f"Failed to process input: {str(e)}",
                error_type=type(e).__name__
            ))

    async def _handle_voice_input(self, event: VoiceInputEvent):
        """Handle voice audio from user"""
        try:
            profile = self.profiles.get_profile(event.channel_id)
            if not profile or not profile.voice or not profile.voice.enabled:
                await self.bus.publish(ErrorEvent(
                    channel_id=event.channel_id,
                    message="Voice not configured for this channel",
                    error_type="ConfigurationError"
                ))
                return

            # Transcribe audio
            await self.bus.publish(StatusEvent(
                channel_id=event.channel_id,
                message="Transcribing audio..."
            ))

            text = await self.voice_providers.transcribe(
                event.audio_data,
                preferred=profile.voice.stt_provider
            )

            if not text.strip():
                logger.debug(f"Empty transcription for channel {event.channel_id}")
                return

            logger.info(f"Transcribed: {text[:100]}...")

            # Record to transcript (exclude binary audio_data)
            if profile.session.transcript_store:
                event_dict = event.model_dump(exclude={'audio_data'})
                await self.transcripts.append_dict(event.channel_id, event_dict)

            # Update session activity
            await self.session_db.update_activity(event.channel_id)

            # Process with agent
            await self._process_with_agent(event.channel_id, text, profile)

        except Exception as e:
            logger.error(f"Error handling voice input: {e}", exc_info=True)
            await self.bus.publish(ErrorEvent(
                channel_id=event.channel_id,
                message=f"Voice processing failed: {str(e)}",
                error_type=type(e).__name__
            ))

    async def _handle_command(self, event: CommandEvent):
        """Handle slash commands"""
        try:
            if event.command == "reset":
                await self._reset_session(event.channel_id)
                await self.bus.publish(StatusEvent(
                    channel_id=event.channel_id,
                    message="Session reset complete"
                ))

            elif event.command == "reload":
                self.profiles.reload()
                await self.bus.publish(StatusEvent(
                    channel_id=event.channel_id,
                    message="Profiles reloaded"
                ))

            else:
                await self.bus.publish(ErrorEvent(
                    channel_id=event.channel_id,
                    message=f"Unknown command: {event.command}",
                    error_type="CommandError"
                ))

        except Exception as e:
            logger.error(f"Error handling command: {e}", exc_info=True)
            await self.bus.publish(ErrorEvent(
                channel_id=event.channel_id,
                message=f"Command failed: {str(e)}",
                error_type=type(e).__name__
            ))

    async def _handle_voice_connection(self, event: VoiceConnectionEvent):
        """Handle voice connection state changes"""
        try:
            if event.state == "joined":
                self.voice_active_channels[event.channel_id] = event.timestamp
                logger.info(f"Voice channel joined for {event.channel_id}")
            elif event.state == "left":
                if event.channel_id in self.voice_active_channels:
                    del self.voice_active_channels[event.channel_id]

                # Clear voice prompt tracking when leaving voice
                if event.channel_id in self.voice_prompted_sessions:
                    self.voice_prompted_sessions.remove(event.channel_id)

                # Clear conversation ID to avoid voice instructions lingering in history
                if event.channel_id in self.conversation_ids:
                    del self.conversation_ids[event.channel_id]
                    logger.info(f"Cleared conversation ID on voice disconnect: {event.channel_id}")

                logger.info(f"Voice channel left for {event.channel_id}")

        except Exception as e:
            logger.error(f"Error handling voice connection event: {e}", exc_info=True)

    async def _process_with_agent(
        self,
        channel_id: str,
        text: str,
        profile: ChannelProfile,
        attachments: Optional[List[Attachment]] = None
    ):
        """Process input with appropriate agent engine"""

        # Get or create lock for this channel
        if channel_id not in self.channel_locks:
            self.channel_locks[channel_id] = asyncio.Lock()

        async with self.channel_locks[channel_id]:
            try:
                # Get or create engine instance
                engine = await self._get_or_create_engine(channel_id, profile)

                # Get conversation ID for multi-turn (Codex only)
                conversation_id = self.conversation_ids.get(channel_id)

                # Inject voice optimization prompt if voice is active (once per session)
                is_voice_active = (
                    profile.voice and profile.voice.enabled and
                    channel_id in self.voice_active_channels
                )

                if is_voice_active and channel_id not in self.voice_prompted_sessions:
                    # Compact voice instruction (~40 tokens)
                    voice_prefix = "You're speaking aloud. Use spoken-friendly phrasing for times, dates, numbers. Skip URLs and markdown. Be conversational. "
                    text = voice_prefix + text
                    self.voice_prompted_sessions.add(channel_id)
                    logger.info(f"Injected voice prompt for channel {channel_id}")

                # Stream responses from engine
                async for event in engine.send_input(text, channel_id, conversation_id, attachments):
                    # Record to transcript
                    if profile.session.transcript_store:
                        await self.transcripts.append(channel_id, event)

                    # Publish to bus
                    await self.bus.publish(event)

                    # Yield control to event loop (prevents Discord heartbeat starvation)
                    await asyncio.sleep(0)

                    # Extract conversation ID if Codex response
                    if isinstance(event, AgentResponseEvent):
                        metadata = event.metadata or {}
                        if "conversationId" in metadata:
                            self.conversation_ids[channel_id] = metadata["conversationId"]
                            logger.info(f"Stored conversation ID for {channel_id}")

                        # Handle voice streaming
                        if profile.voice and profile.voice.enabled and channel_id in self.voice_active_channels:
                            await self._handle_voice_streaming(channel_id, event, profile)

            except Exception as e:
                logger.error(f"Error processing with agent: {e}", exc_info=True)
                await self.bus.publish(ErrorEvent(
                    channel_id=channel_id,
                    message=f"Agent error: {str(e)}",
                    error_type=type(e).__name__
                ))

    async def _get_or_create_engine(
        self,
        channel_id: str,
        profile: ChannelProfile
    ) -> AgentEngine:
        """Get existing or create new engine instance"""

        # Return existing if available
        if channel_id in self.engines:
            return self.engines[channel_id]

        # Create new engine
        engine_type = profile.engine.type
        logger.info(f"Creating {engine_type} engine for channel {channel_id}")

        if engine_type == "codex":
            engine = CodexEngine()
        elif engine_type == "claude-code":
            engine = ClaudeEngine()
        else:
            raise ValueError(f"Unknown engine type: {engine_type}")

        # Start engine
        await engine.start(profile.engine.config_home)

        # Store instance
        self.engines[channel_id] = engine

        # Save session
        await self.session_db.save_session(
            channel_id=channel_id,
            agent_type=engine_type
        )

        return engine

    async def _reset_session(self, channel_id: str):
        """Reset session - close engine and clear state"""

        # Close engine if exists
        if channel_id in self.engines:
            engine = self.engines[channel_id]
            await engine.close()
            del self.engines[channel_id]
            logger.info(f"Closed engine for channel {channel_id}")

        # Clear conversation ID
        if channel_id in self.conversation_ids:
            del self.conversation_ids[channel_id]

        # Clear database records
        await self.session_db.clear_session(channel_id)
        await self.transcripts.clear_channel(channel_id)

        logger.info(f"Reset session for channel {channel_id}")

    async def _handle_voice_streaming(
        self,
        channel_id: str,
        event: AgentResponseEvent,
        profile: ChannelProfile
    ):
        """
        Intelligently buffer and stream voice output.

        Engine-specific flush conditions:
        - Claude Code: Flush on any complete sentence (sends full messages)
        - Codex: Flush on sentence + ≥30 chars/6 words (sends deltas)
        - Fallback: 180 chars or 1.5s timeout
        """
        import re

        # Check if streaming is enabled for this profile
        stream_chunks = profile.voice.stream_chunks

        # Initialize buffer
        if channel_id not in self.voice_buffers:
            self.voice_buffers[channel_id] = ""
            self.voice_spoken[channel_id] = ""
            self.last_voice_time[channel_id] = time.time()

        # Accumulate content
        self.voice_buffers[channel_id] += event.content
        buffer = self.voice_buffers[channel_id]

        # Compute new content early (before flush checks)
        spoken_so_far = self.voice_spoken[channel_id]
        new_text = buffer[len(spoken_so_far):]

        # Check if we should flush
        should_flush = False

        if event.is_final:
            # Final chunk - flush everything not yet spoken
            should_flush = True
        elif stream_chunks and new_text.strip():
            # Engine-specific flush logic
            engine_type = profile.engine.type

            if engine_type == "claude-code":
                # Claude Code sends complete messages - flush immediately on sentence end
                # Regex handles punctuation with quotes/brackets: "Hello.", "Done!", list:\n\n
                if re.search(r'[.!?]["\')\]]?\s*$', new_text):
                    should_flush = True

            elif engine_type == "codex":
                # Codex sends deltas - require minimum length + sentence terminator
                word_count = len(new_text.split())
                char_count = len(new_text)
                has_terminator = re.search(r'[.!?]["\')\]]?\s*$', new_text)

                if has_terminator and (char_count >= 30 or word_count >= 6):
                    should_flush = True

            # Fallback conditions (all engines)
            buffer_length = len(buffer)
            time_since_last = time.time() - self.last_voice_time[channel_id]

            if buffer_length >= 180:
                should_flush = True
            elif time_since_last >= 1.5:
                should_flush = True

        if should_flush and new_text.strip():
            await self._synthesize_and_emit(channel_id, new_text.strip(), profile)
            self.voice_spoken[channel_id] = buffer
            self.last_voice_time[channel_id] = time.time()

            # Clear buffer on final
            if event.is_final:
                del self.voice_buffers[channel_id]
                del self.voice_spoken[channel_id]
                del self.last_voice_time[channel_id]

    def _split_text_for_tts(self, text: str, max_chars: int = 200) -> list[str]:
        """Split long text into TTS-safe chunks to avoid Kokoro 510 phoneme limit"""
        if len(text) <= max_chars:
            return [text]

        sentences = text.replace('\n\n', '. ').replace('\n', '. ').split('. ')
        chunks = []
        current = ""

        for sentence in sentences:
            if len(current) + len(sentence) < max_chars:
                current += sentence + ". "
            else:
                if current:
                    chunks.append(current.strip())
                current = sentence + ". "

        if current:
            chunks.append(current.strip())

        return chunks

    def _normalize_for_speech(self, text: str) -> str:
        """Normalize text for natural TTS output using battle-tested libraries"""
        import re
        from datetime import datetime
        from num2words import num2words
        import inflect

        p = inflect.engine()

        # Time normalization: "16:00" → "four PM", "14:30" → "two thirty PM"
        def replace_time(match):
            hour, minute = int(match.group(1)), int(match.group(2))

            # Convert to 12-hour format
            period = "AM" if hour < 12 else "PM"
            hour_12 = hour % 12
            if hour_12 == 0:
                hour_12 = 12

            # Use num2words instead of hardcoded arrays
            hour_words = num2words(hour_12)

            if minute == 0:
                return f"{hour_words} {period}"
            else:
                # Speak minutes naturally: "14:05" → "two oh five PM"
                if minute < 10:
                    minute_words = f"oh {num2words(minute)}"
                else:
                    minute_words = num2words(minute)
                return f"{hour_words} {minute_words} {period}"

        text = re.sub(r'\b(\d{1,2}):(\d{2})\b', replace_time, text)

        # Date normalization: "2025-10-03" → "October third, twenty twenty-five"
        def replace_iso_date(match):
            try:
                date = datetime.strptime(match.group(0), '%Y-%m-%d')
                today = datetime.now().date()

                # Handle relative dates
                if date.date() == today:
                    return "today"
                elif date.date() == today.replace(day=today.day + 1):
                    return "tomorrow"
                else:
                    # Use inflect for ordinal day: "3rd", "21st", etc.
                    month_name = date.strftime("%B")
                    day_ordinal = p.ordinal(date.day)  # "3rd" → spoken as "third"
                    year = date.year

                    # Split year for natural speech: "2025" → "twenty twenty-five"
                    if year >= 2000 and year < 2100:
                        century = year // 100
                        decade = year % 100
                        if decade == 0:
                            year_words = f"{num2words(century)} hundred"
                        else:
                            year_words = f"{num2words(century)} {num2words(decade)}"
                    else:
                        year_words = num2words(year)

                    return f"{month_name} {day_ordinal}, {year_words}"
            except:
                return match.group(0)

        text = re.sub(r'\b\d{4}-\d{2}-\d{2}\b', replace_iso_date, text)

        # Convert standalone numbers: "42 tasks" → "forty-two tasks"
        # But avoid converting times/dates that were already handled
        def replace_number(match):
            num = int(match.group(0))
            # Skip very large numbers (likely IDs/codes)
            if num > 10000:
                return match.group(0)
            return num2words(num)

        text = re.sub(r'(?<!:)\b\d{1,4}\b(?!:)', replace_number, text)

        # Remove URLs (awkward when spoken)
        text = re.sub(r'https?://\S+', '', text)

        # Remove markdown code blocks
        text = re.sub(r'```.*?```', '', text, flags=re.DOTALL)
        text = re.sub(r'`[^`]+`', '', text)

        # Remove markdown formatting (keep text)
        text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)  # Bold
        text = re.sub(r'\*([^*]+)\*', r'\1', text)      # Italic
        text = re.sub(r'__([^_]+)__', r'\1', text)      # Underline

        # Clean up extra whitespace
        text = re.sub(r'\s+', ' ', text).strip()

        return text

    async def _synthesize_and_emit(
        self,
        channel_id: str,
        text: str,
        profile: ChannelProfile
    ):
        """Synthesize text and emit VoiceOutputEvent for each chunk"""
        try:
            tts_provider = profile.voice.tts_provider
            tts_voice = profile.voice.tts_voice
            fallback = profile.voice.tts_fallback

            # Normalize text for natural speech BEFORE TTS
            normalized_text = self._normalize_for_speech(text)
            logger.info(f"Synthesizing voice for channel {channel_id}: {normalized_text[:100]}...")

            # Split text into chunks to avoid Kokoro 510 phoneme limit
            text_chunks = self._split_text_for_tts(normalized_text)

            # Process and emit each chunk immediately
            for i, chunk in enumerate(text_chunks):
                # Synthesize this chunk
                chunk_audio = await self.voice_providers.synthesize(
                    chunk,
                    preferred=tts_provider,
                    fallback=fallback,
                    voice=tts_voice  # Pass the voice from channel profile
                )

                # Add padding between chunks (not after last)
                if i < len(text_chunks) - 1:
                    padding = np.zeros(int(0.05 * 24000), dtype=np.int16)
                    audio_array = np.concatenate([
                        np.frombuffer(chunk_audio, dtype=np.int16),
                        padding
                    ])
                else:
                    # Final chunk gets end padding
                    padding = np.zeros(int(0.1 * 24000), dtype=np.int16)
                    audio_array = np.concatenate([
                        np.frombuffer(chunk_audio, dtype=np.int16),
                        padding
                    ])

                padded_audio = audio_array.tobytes()

                # Emit this chunk immediately
                await self.bus.publish(VoiceOutputEvent(
                    channel_id=channel_id,
                    audio_bytes=padded_audio,
                    sample_rate=24000,
                    voice_name=tts_voice,
                    original_text=chunk  # Log individual chunk
                ))

                logger.info(f"Voice chunk {i+1}/{len(text_chunks)} emitted: {len(chunk)} chars, {len(padded_audio)} bytes")

        except Exception as e:
            logger.error(f"Voice synthesis failed: {e}", exc_info=True)

    async def cleanup_old_sessions(self, max_age_hours: int = 24):
        """Cleanup old inactive sessions"""
        try:
            # Get old sessions from database
            await self.session_db.cleanup_old_sessions(max_age_hours)

            # Close any stale engines (safety net)
            # In production, add logic to track last activity per engine

            logger.info("Session cleanup completed")

        except Exception as e:
            logger.error(f"Error during cleanup: {e}", exc_info=True)

    async def shutdown(self):
        """Graceful shutdown - close all engines"""
        logger.info("Shutting down session orchestrator")

        for channel_id, engine in list(self.engines.items()):
            try:
                await engine.close()
                logger.info(f"Closed engine for {channel_id}")
            except Exception as e:
                logger.error(f"Error closing engine for {channel_id}: {e}")

        self.engines.clear()
        self.conversation_ids.clear()
        logger.info("Session orchestrator shutdown complete")