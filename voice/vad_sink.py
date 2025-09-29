import asyncio
import audioop
import logging
import time
from collections import deque
from typing import Callable, Dict, Optional
import discord

try:
    import webrtcvad
    VAD_AVAILABLE = True
except ImportError:
    VAD_AVAILABLE = False
    webrtcvad = None

logger = logging.getLogger(__name__)


class UserStream:
    def __init__(
        self,
        user_id: int,
        vad: Optional[any],
        frame_ms: int = 30,
        silence_frames: int = 33,
        on_utterance: Optional[Callable] = None,
        no_vad_timeout_ms: int = 3000
    ):
        self.user_id = user_id
        self.vad = vad
        self.frame_ms = frame_ms
        self.silence_frames = silence_frames
        self.on_utterance = on_utterance
        self.no_vad_timeout_ms = no_vad_timeout_ms

        self.resample_state = None
        self.buffer_16k = bytearray()
        self.utterance_buffer = bytearray()

        self.frame_size_16k = (16000 * frame_ms // 1000) * 2
        self.speech_window = deque(maxlen=silence_frames)
        self.has_speech = False
        self.utterance_start_time = None

        self.no_vad_frame_count = 0
        self.no_vad_flush_threshold = no_vad_timeout_ms // frame_ms

        self.preroll_buffer = deque(maxlen=13)
        self.last_frame_time = None
        self.last_speech_time = None

        # Async timer support
        self.loop = None  # Will be set by VADSink
        self.max_duration_task = None
        self.max_duration_seconds = 8.0

        logger.info(f"UserStream initialized for user {user_id} (frame={frame_ms}ms, silence={silence_frames} frames, vad={vad is not None})")

    def feed(self, data_48k: bytes):
        self.last_frame_time = time.time()
        try:
            data_16k_stereo, self.resample_state = audioop.ratecv(
                data_48k,
                2,
                2,
                48000,
                16000,
                self.resample_state
            )

            data_16k = audioop.tomono(data_16k_stereo, 2, 0.5, 0.5)

            self.buffer_16k.extend(data_16k)

            while len(self.buffer_16k) >= self.frame_size_16k:
                frame = bytes(self.buffer_16k[:self.frame_size_16k])
                self.buffer_16k = self.buffer_16k[self.frame_size_16k:]

                self._process_frame(frame)

        except Exception as e:
            logger.error(f"Error in UserStream.feed: {e}", exc_info=True)

    def _process_frame(self, frame_16k: bytes):
        if not self.vad:
            self.utterance_buffer.extend(frame_16k)
            self.no_vad_frame_count += 1

            if not self.has_speech:
                self.has_speech = True
                self.utterance_start_time = time.time()

                # Start max duration timer for no-VAD mode
                if self.loop:
                    self.max_duration_task = self.loop.call_later(
                        self.max_duration_seconds,
                        lambda: self.loop.call_soon_threadsafe(self._force_finalize_from_timer)
                    )
                    logger.debug(f"Started {self.max_duration_seconds}s max duration timer for user {self.user_id} (no-VAD mode)")

            if self.no_vad_frame_count >= self.no_vad_flush_threshold:
                logger.debug(f"No-VAD timeout flush for user {self.user_id} after {self.no_vad_timeout_ms}ms")
                self._finalize_utterance()
            return

        try:
            is_speech = self.vad.is_speech(frame_16k, 16000)
        except Exception as e:
            logger.warning(f"VAD error: {e}, assuming speech")
            is_speech = True

        if not self.has_speech:
            self.preroll_buffer.append(frame_16k)

        self.speech_window.append(is_speech)

        if is_speech:
            self.last_speech_time = time.time()

            if not self.has_speech:
                self.has_speech = True
                self.utterance_start_time = time.time()

                for buffered_frame in self.preroll_buffer:
                    self.utterance_buffer.extend(buffered_frame)

                self.preroll_buffer.clear()
                logger.debug(f"Speech detected for user {self.user_id} (added {len(self.utterance_buffer)} bytes from pre-roll)")

                # Start max duration timer
                if self.loop:
                    self.max_duration_task = self.loop.call_later(
                        self.max_duration_seconds,
                        lambda: self.loop.call_soon_threadsafe(self._force_finalize_from_timer)
                    )
                    logger.debug(f"Started {self.max_duration_seconds}s max duration timer for user {self.user_id}")

            self.utterance_buffer.extend(frame_16k)

        elif self.has_speech:
            self.utterance_buffer.extend(frame_16k)

            silence_count = sum(1 for x in self.speech_window if not x)

            if silence_count >= self.silence_frames:
                self._finalize_utterance()

        if self.has_speech:
            now = time.time()

            if self.last_frame_time and (now - self.last_frame_time) > 0.6:
                logger.info(f"Force finalize: no frames for {now - self.last_frame_time:.1f}s")
                self._finalize_utterance()
                return

            if self.last_speech_time and (now - self.last_speech_time) > 0.4:
                logger.info(f"Force finalize: no speech for {now - self.last_speech_time:.1f}s")
                self._finalize_utterance()
                return

            utterance_duration = now - self.utterance_start_time
            if utterance_duration > 8.0:
                logger.warning(f"Force finalize: max duration reached ({utterance_duration:.1f}s)")
                self._finalize_utterance()
                return

    def _force_finalize_from_timer(self):
        """Called by async timer when max duration is reached"""
        if self.has_speech:
            logger.warning(f"Max duration timer expired ({self.max_duration_seconds}s) for user {self.user_id}")
            self._finalize_utterance()

    def _finalize_utterance(self):
        if not self.utterance_buffer:
            return

        rms = audioop.rms(bytes(self.utterance_buffer), 2)
        if rms < 200:
            logger.debug(f"Discarding low-energy utterance (RMS={rms})")
            self._reset_state()
            return

        utterance_duration = len(self.utterance_buffer) / (16000 * 2)

        if utterance_duration < 0.5:
            logger.debug(f"Discarding short utterance ({utterance_duration:.2f}s)")
            self._reset_state()
            return

        logger.info(f"Utterance complete for user {self.user_id}: {utterance_duration:.2f}s, {len(self.utterance_buffer)} bytes")

        metadata = {
            "user_id": self.user_id,
            "duration": utterance_duration,
            "start_time": self.utterance_start_time,
            "end_time": time.time(),
        }

        if self.on_utterance:
            self.on_utterance(bytes(self.utterance_buffer), metadata)

        self._reset_state()

    def _reset_state(self):
        self.utterance_buffer.clear()
        self.speech_window.clear()
        self.has_speech = False
        self.utterance_start_time = None
        self.no_vad_frame_count = 0

        # Cancel max duration timer if active
        if self.max_duration_task:
            self.max_duration_task.cancel()
            self.max_duration_task = None

    def flush(self, force: bool = False):
        if force and len(self.utterance_buffer) > 0:
            logger.info(f"Force flushing utterance for user {self.user_id}")
            self._finalize_utterance()


class VADSink(discord.sinks.Sink):
    def __init__(
        self,
        on_utterance: Callable,
        *,
        channel_id: str,
        vad_level: int = 2,
        frame_ms: int = 30,
        silence_ms: int = 1000
    ):
        super().__init__()
        self.on_utterance = on_utterance
        self.channel_id = channel_id
        self.vad_level = vad_level
        self.frame_ms = frame_ms
        self.silence_ms = silence_ms

        if not VAD_AVAILABLE:
            logger.warning("webrtcvad not available, using timer-based fallback (3s chunks)")
            self.vad = None
        else:
            try:
                self.vad = webrtcvad.Vad(vad_level)
                logger.info(f"VADSink initialized (level={vad_level}, frame={frame_ms}ms, silence={silence_ms}ms)")
            except Exception as e:
                logger.warning(f"Failed to initialize VAD: {e}, using timer-based fallback")
                self.vad = None

        self.silence_frames = silence_ms // frame_ms
        self.streams: Dict[int, UserStream] = {}

    def write(self, data: bytes, user: int):
        if user not in self.streams:
            stream = UserStream(
                user_id=user,
                vad=self.vad,
                frame_ms=self.frame_ms,
                silence_frames=self.silence_frames,
                on_utterance=lambda pcm, meta: self._submit_utterance(user, pcm, meta)
            )
            # Pass the event loop from voice client
            if hasattr(self, 'vc') and self.vc:
                stream.loop = self.vc.loop
            self.streams[user] = stream

        self.streams[user].feed(data)

    def _submit_utterance(self, user_id: int, pcm16_data: bytes, metadata: dict):
        if hasattr(self, 'vc') and self.vc and self.vc.loop:
            asyncio.run_coroutine_threadsafe(
                self.on_utterance(self.channel_id, user_id, pcm16_data, metadata),
                self.vc.loop
            )
        else:
            logger.warning(f"Cannot submit utterance: voice client not available")

    def cleanup(self):
        logger.info("VADSink cleanup: flushing all streams")
        for stream in self.streams.values():
            stream.flush(force=True)
        super().cleanup()
