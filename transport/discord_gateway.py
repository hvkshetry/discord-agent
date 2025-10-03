"""Discord gateway - handles Discord API integration"""
import asyncio
import discord
from discord.ext import commands
import logging
import io
import numpy as np
from typing import Dict, Optional
from collections import deque
import time
import socket
from pathlib import Path

from discord import voice_client as discord_voice_client
from discord.utils import MISSING

from core.events import (
    Event, TextInputEvent, VoiceInputEvent, CommandEvent,
    AgentResponseEvent, ToolCallEvent, StatusEvent, ErrorEvent, VoiceOutputEvent,
    VoiceConnectionEvent, Attachment
)
from core.bus import EventBus
from core.profiles import ProfileLoader
from voice.vad_sink import VADSink

logger = logging.getLogger(__name__)


# Monkey patch Pycord 2.6.x voice handshake race condition
if not getattr(
    discord_voice_client.VoiceClient.on_voice_server_update,
    "__discord_agent_patched__",
    False
):
    async def _safe_voice_server_update(self, data):
        if self._voice_server_complete.is_set():
            discord_voice_client._log.info("Ignoring extraneous voice server update.")
            return

        self.token = data.get("token")
        self.server_id = int(data["guild_id"])
        endpoint = data.get("endpoint")

        if endpoint is None or self.token is None:
            discord_voice_client._log.warning(
                "Awaiting endpoint... This requires waiting. If timeout occurred "
                "consider raising the timeout and reconnecting."
            )
            return

        self.endpoint = endpoint.removeprefix("wss://")
        self.endpoint_ip = MISSING

        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.setblocking(False)

        if not self._handshaking:
            ws = getattr(self, "ws", MISSING)
            if ws is MISSING:
                discord_voice_client._log.warning(
                    "Voice server update received before websocket initialisation; "
                    "skipping websocket close"
                )
            else:
                await ws.close(4000)
            return

        self._voice_server_complete.set()

    _safe_voice_server_update.__discord_agent_patched__ = True
    discord_voice_client.VoiceClient.on_voice_server_update = _safe_voice_server_update


class DiscordGateway:
    """
    Discord bot gateway for event bus integration.

    Responsibilities:
    - Listen for Discord messages and voice
    - Convert to event bus events
    - Send agent responses back to Discord
    - Handle voice channel audio recording
    """

    def __init__(
        self,
        event_bus: EventBus,
        profile_loader: ProfileLoader,
        token: str,
        command_prefix: str = "!"
    ):
        self.bus = event_bus
        self.profiles = profile_loader
        self.token = token

        # Discord bot setup
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        intents.guilds = True

        self.bot = commands.Bot(command_prefix=command_prefix, intents=intents)

        # Voice connection state
        self.voice_clients: Dict[str, discord.VoiceClient] = {}
        self.voice_buffers: Dict[str, deque] = {}
        self.last_audio_time: Dict[str, float] = {}
        self.voice_connecting: set[str] = set()

        # Simple message tracking for multi-turn conversations
        self.response_messages: Dict[str, discord.Message] = {}
        self.response_request_ids: Dict[str, str] = {}  # Track which request each message belongs to

        # Voice output state
        self.voice_output_queues: Dict[str, asyncio.Queue] = {}
        self.voice_player_tasks: Dict[str, asyncio.Task] = {}

        # Gateway reconnect tracking
        self._first_ready = True

        # Setup event handlers
        self._setup_discord_handlers()
        self._subscribe_to_bus()

    def _setup_discord_handlers(self):
        """Setup Discord event handlers"""

        @self.bot.event
        async def on_ready():
            logger.info(f"Discord bot ready: {self.bot.user}")

            if not self._first_ready and self.voice_clients:
                logger.warning("Gateway reconnected - forcing voice client reconnection to rebuild encryption keys")
                await self._reconnect_voice_clients()

            self._first_ready = False

        @self.bot.event
        async def on_message(message: discord.Message):
            await self._handle_message(message)

        @self.bot.event
        async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
            await self._handle_voice_state_update(member, before, after)

        @self.bot.command(name="join")
        async def join_voice(ctx: commands.Context):
            """Join voice channel"""
            await self._join_voice_channel(ctx)

        @self.bot.command(name="leave")
        async def leave_voice(ctx: commands.Context):
            """Leave voice channel"""
            await self._leave_voice_channel(ctx)

        @self.bot.command(name="reset")
        async def reset_session(ctx: commands.Context):
            """Reset agent session"""
            await self._reset_session(ctx)

    def _subscribe_to_bus(self):
        """Subscribe to event bus events"""
        self.bus.subscribe("agent_response", self._handle_agent_response)
        self.bus.subscribe("tool_call", self._handle_tool_call)
        self.bus.subscribe("status", self._handle_status)
        self.bus.subscribe("error", self._handle_error)
        self.bus.subscribe("voice_output", self._handle_voice_output)
        logger.info("Discord gateway subscribed to event bus")

    async def _handle_message(self, message: discord.Message):
        """Handle incoming Discord message"""

        # Ignore bot messages
        if message.author.bot:
            return

        # Check if channel is configured
        channel_id = str(message.channel.id)
        profile = self.profiles.get_profile(channel_id)

        if not profile:
            # Ignore unconfigured channels
            return

        # Handle commands
        if message.content.startswith(self.bot.command_prefix):
            await self.bot.process_commands(message)
            return

        # Download attachments
        saved = []
        MAX_SIZE = 10 * 1024 * 1024  # 10MB limit

        if message.attachments:
            base_dir = Path("artifacts/discord") / channel_id / str(message.id)
            base_dir.mkdir(parents=True, exist_ok=True)

            for index, attachment in enumerate(message.attachments):
                if attachment.size and attachment.size > MAX_SIZE:
                    logger.warning(f"Skipping large attachment {attachment.filename}: {attachment.size} bytes")
                    continue

                try:
                    data = await attachment.read()
                    target = base_dir / attachment.filename
                    target.write_bytes(data)

                    saved.append(Attachment(
                        id=f"{message.id}-{index}",
                        filename=attachment.filename,
                        content_type=attachment.content_type,
                        size=attachment.size,
                        path=str(target),
                    ))
                    logger.info(f"Downloaded attachment: {attachment.filename} ({attachment.size} bytes)")
                except Exception as e:
                    logger.error(f"Failed to download attachment {attachment.filename}: {e}")

        # Publish text input event with attachments
        logger.info(f"Message from {message.author} in {message.channel}: {message.content[:100]} with {len(saved)} attachments")

        await self.bus.publish(TextInputEvent(
            channel_id=channel_id,
            user_id=str(message.author.id),
            content=message.content or "",
            attachments=saved
        ))

    async def _join_voice_channel(self, ctx: commands.Context):
        """Join user's voice channel"""
        if not ctx.author.voice:
            await ctx.send("You must be in a voice channel!")
            return

        voice_channel = ctx.author.voice.channel
        channel_id = str(ctx.channel.id)

        profile = self.profiles.get_profile(channel_id)
        if not profile or not profile.voice or not profile.voice.enabled:
            await ctx.send("Voice is not configured for this channel")
            return

        try:
            await self._ensure_voice_connection(ctx.channel, voice_channel)
            await ctx.send(f"Joined {voice_channel.name}")

        except Exception as e:
            logger.error(f"Error joining voice: {e}", exc_info=True)
            await ctx.send(f"Failed to join voice: {str(e)}")

    async def _leave_voice_channel(self, ctx: commands.Context):
        """Leave voice channel"""
        channel_id = str(ctx.channel.id)

        if channel_id not in self.voice_clients:
            await ctx.send("Not in a voice channel")
            return

        await self._stop_voice_connection(channel_id)
        await ctx.send("Left voice channel")

    async def _reset_session(self, ctx: commands.Context):
        """Reset agent session"""
        channel_id = str(ctx.channel.id)

        await self.bus.publish(CommandEvent(
            channel_id=channel_id,
            user_id=str(ctx.author.id),
            command="reset"
        ))

        await ctx.send("Session reset requested")

    async def _ensure_voice_connection(
        self,
        text_channel: discord.TextChannel,
        voice_channel: discord.VoiceChannel
    ):
        channel_id = str(text_channel.id)
        voice_channel_id = str(voice_channel.id)

        if channel_id in self.voice_connecting:
            logger.debug(f"Already connecting to voice for {channel_id}, skipping")
            return

        profile = self.profiles.get_profile(channel_id)
        if not profile or not profile.voice or not profile.voice.enabled:
            logger.warning(f"Voice not configured for channel {channel_id}")
            return

        if channel_id in self.voice_clients:
            existing_vc = self.voice_clients[channel_id]
            if existing_vc.channel.id == voice_channel.id:
                logger.debug(f"Already connected to {voice_channel.name}")
                return
            else:
                logger.info(f"Moving from {existing_vc.channel.name} to {voice_channel.name}")
                await self._stop_voice_connection(channel_id)

        self.voice_connecting.add(channel_id)
        try:
            voice_client = await voice_channel.connect()
            self.voice_clients[channel_id] = voice_client

            vad_sink = VADSink(
                on_utterance=self._handle_vad_utterance,
                channel_id=channel_id,
                vad_level=2,
                frame_ms=20,
                silence_ms=700  # Increased to handle natural pauses in speech
            )

            voice_client.start_recording(
                vad_sink,
                self._voice_receive_callback,
                text_channel
            )

            self.voice_output_queues[channel_id] = asyncio.Queue()
            self.voice_player_tasks[channel_id] = asyncio.create_task(
                self._voice_player_loop(channel_id)
            )

            await self.bus.publish(VoiceConnectionEvent(
                channel_id=channel_id,
                state="joined",
                voice_channel_id=voice_channel_id,
                text_channel_id=channel_id
            ))

            logger.info(f"Joined voice channel {voice_channel.name} for text channel {channel_id}")

        except Exception as e:
            logger.error(f"Error in _ensure_voice_connection: {e}", exc_info=True)
            raise
        finally:
            self.voice_connecting.discard(channel_id)

    async def _stop_voice_connection(self, channel_id: str):
        if channel_id not in self.voice_clients:
            logger.debug(f"No voice client for {channel_id}")
            return

        try:
            voice_client = self.voice_clients[channel_id]
            voice_channel_id = str(voice_client.channel.id)

            try:
                voice_client.stop_recording()
            except Exception as e:
                logger.warning(f"Error stopping recording: {e}")

            await voice_client.disconnect()
            del self.voice_clients[channel_id]

            if channel_id in self.voice_buffers:
                del self.voice_buffers[channel_id]
            if channel_id in self.last_audio_time:
                del self.last_audio_time[channel_id]

            if channel_id in self.voice_player_tasks:
                self.voice_player_tasks[channel_id].cancel()
                del self.voice_player_tasks[channel_id]

            if channel_id in self.voice_output_queues:
                del self.voice_output_queues[channel_id]

            await self.bus.publish(VoiceConnectionEvent(
                channel_id=channel_id,
                state="left",
                voice_channel_id=voice_channel_id,
                text_channel_id=channel_id
            ))

            logger.info(f"Left voice channel for {channel_id}")

        except Exception as e:
            logger.error(f"Error in _stop_voice_connection: {e}", exc_info=True)

    async def _reconnect_voice_clients(self):
        """Force reconnect all voice clients to rebuild encryption keys after gateway reconnect"""
        reconnect_info = []

        for channel_id, voice_client in list(self.voice_clients.items()):
            try:
                voice_channel = voice_client.channel
                text_channel_id = int(channel_id)
                text_channel = self.bot.get_channel(text_channel_id)

                if text_channel and voice_channel:
                    reconnect_info.append((text_channel, voice_channel))
                    await self._stop_voice_connection(channel_id)
                    logger.info(f"Disconnected voice for {channel_id} to prepare for reconnect")

            except Exception as e:
                logger.error(f"Error disconnecting voice client {channel_id}: {e}", exc_info=True)

        await asyncio.sleep(0.5)

        for text_channel, voice_channel in reconnect_info:
            try:
                await self._ensure_voice_connection(text_channel, voice_channel)
                logger.info(f"Reconnected voice for {text_channel.id} with fresh encryption keys")
            except Exception as e:
                logger.error(f"Error reconnecting voice for {text_channel.id}: {e}", exc_info=True)

    async def _handle_vad_utterance(self, channel_id: str, user_id: int, pcm16_data: bytes, metadata: dict):
        try:
            logger.info(f"VAD utterance from user {user_id} in channel {channel_id}: {len(pcm16_data)} bytes, {metadata.get('duration', 0):.2f}s")

            await self.bus.publish(VoiceInputEvent(
                channel_id=channel_id,
                user_id=str(user_id),
                audio_data=pcm16_data
            ))

        except Exception as e:
            logger.error(f"Error in _handle_vad_utterance: {e}", exc_info=True)

    async def _handle_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState
    ):
        if member.bot:
            return

        if before.channel is None and after.channel is not None:
            result = self.profiles.get_profile_for_voice_channel(str(after.channel.id))
            if not result:
                logger.debug(f"No profile mapped to voice channel {after.channel.name} ({after.channel.id})")
                return

            text_channel_id, profile = result
            text_channel = self.bot.get_channel(int(text_channel_id))

            if not text_channel:
                logger.error(f"Text channel {text_channel_id} not found for voice channel {after.channel.name}")
                return

            logger.info(f"{member.name} joined voice channel {after.channel.name}, auto-joining")
            try:
                await self._ensure_voice_connection(text_channel, after.channel)
            except Exception as e:
                logger.error(f"Failed to auto-join voice: {e}")

        elif before.channel is not None and after.channel is None:
            result = self.profiles.get_profile_for_voice_channel(str(before.channel.id))
            if not result:
                return

            text_channel_id, profile = result

            if text_channel_id in self.voice_clients:
                voice_client = self.voice_clients[text_channel_id]
                remaining_users = [m for m in voice_client.channel.members if not m.bot]

                if not remaining_users:
                    logger.info(f"No users left in voice channel, disconnecting")
                    try:
                        await self._stop_voice_connection(text_channel_id)
                    except Exception as e:
                        logger.error(f"Failed to disconnect voice: {e}")

        elif before.channel is not None and after.channel is not None and before.channel != after.channel:
            before_result = self.profiles.get_profile_for_voice_channel(str(before.channel.id))
            after_result = self.profiles.get_profile_for_voice_channel(str(after.channel.id))

            if before_result:
                before_text_channel_id, _ = before_result

                if before_text_channel_id in self.voice_clients:
                    voice_client = self.voice_clients[before_text_channel_id]
                    if voice_client.channel.id == before.channel.id:
                        logger.info(f"{member.name} moved from {before.channel.name}")

                        remaining_users = [m for m in voice_client.channel.members if not m.bot]
                        if not remaining_users:
                            logger.info(f"No users left in {before.channel.name}, disconnecting")
                            try:
                                await self._stop_voice_connection(before_text_channel_id)
                            except Exception as e:
                                logger.error(f"Failed to disconnect voice: {e}")

            if after_result:
                after_text_channel_id, after_profile = after_result
                after_text_channel = self.bot.get_channel(int(after_text_channel_id))

                if after_text_channel:
                    logger.info(f"{member.name} moved to {after.channel.name}, joining")
                    try:
                        await self._ensure_voice_connection(after_text_channel, after.channel)
                    except Exception as e:
                        logger.error(f"Failed to follow user to new channel: {e}")

    async def _voice_receive_callback(self, sink: discord.sinks.WaveSink, channel: discord.TextChannel):
        """
        Voice recording callback.

        Note: This is a simplified version. Production would need:
        - Proper silence detection
        - Audio chunking
        - Per-user audio separation
        """
        channel_id = str(channel.id)
        profile = self.profiles.get_profile(channel_id)

        if not profile or not profile.voice:
            return

        # Get audio data from sink
        # This is placeholder - actual implementation needs proper audio handling
        for user_id, audio in sink.audio_data.items():
            # Skip bot's own audio
            if user_id == self.bot.user.id:
                continue

            # Get audio bytes
            audio_bytes = audio.file.getvalue()

            if len(audio_bytes) > 0:
                # Publish voice input event
                await self.bus.publish(VoiceInputEvent(
                    channel_id=channel_id,
                    user_id=str(user_id),
                    audio_data=audio_bytes
                ))

                logger.info(f"Received voice input from user {user_id}: {len(audio_bytes)} bytes")

    async def _handle_agent_response(self, event: AgentResponseEvent):
        """Handle agent response - send complete messages to Discord"""
        try:
            channel_id = event.channel_id
            channel = self.bot.get_channel(int(channel_id))

            if not channel:
                logger.warning(f"Channel not found: {channel_id}")
                return

            # Extract request ID from metadata
            request_id = event.metadata.get("requestId") if event.metadata else "unknown"

            # Check if this is a new request (different from current tracked request)
            if channel_id in self.response_request_ids:
                if self.response_request_ids[channel_id] != request_id:
                    # New request - clear old message reference
                    if channel_id in self.response_messages:
                        del self.response_messages[channel_id]
                    self.response_request_ids[channel_id] = request_id
            else:
                # First time for this channel
                self.response_request_ids[channel_id] = request_id

            # Handle the event
            if event.content:  # Non-empty content
                # Always send new message for each complete response
                # (Claude Code sends multiple complete assistant messages, not deltas)
                message = await channel.send(self._truncate_message(event.content))
                self.response_messages[channel_id] = message

            # No cleanup needed - keep request_id tracking for multi-turn detection

        except Exception as e:
            logger.error(f"Error sending agent response: {e}", exc_info=True)

    async def _handle_tool_call(self, event: ToolCallEvent):
        """Handle tool call - display with embed"""
        try:
            channel_id = int(event.channel_id)
            channel = self.bot.get_channel(channel_id)

            if not channel:
                return

            # Skip tool embeds when voice is active (awkward when spoken via TTS)
            if event.channel_id in self.voice_clients:
                logger.debug(f"Suppressing tool embed in voice mode: {event.tool_name}")
                return

            # Create rich embed for tool call
            embed = discord.Embed(
                title=f"🔧 {event.tool_name}",
                description=f"Status: {event.status or 'running'}",
                color=discord.Color.blue()
            )

            # Add arguments as fields
            for key, value in event.arguments.items():
                value_str = str(value)
                # Discord limit is 1024 chars per field value
                # Account for ``` wrapper (6 chars) and truncation marker (3 chars)
                if len(value_str) > 1015:
                    value_str = value_str[:1015] + "..."
                embed.add_field(name=key, value=f"```{value_str}```", inline=False)

            await channel.send(embed=embed)

        except Exception as e:
            logger.error(f"Error sending tool call: {e}", exc_info=True)

    async def _handle_status(self, event: StatusEvent):
        """Handle status event - show typing indicator"""
        try:
            channel_id = int(event.channel_id)
            channel = self.bot.get_channel(channel_id)

            if not channel:
                return

            # Send as simple message
            # Could also use typing indicator: async with channel.typing()
            await channel.send(f"_{event.message}_")

        except Exception as e:
            logger.error(f"Error sending status: {e}", exc_info=True)

    async def _handle_error(self, event: ErrorEvent):
        """Handle error event - send to Discord"""
        try:
            channel_id = int(event.channel_id)
            channel = self.bot.get_channel(channel_id)

            if not channel:
                return

            # Create error embed
            embed = discord.Embed(
                title="❌ Error",
                description=event.message,
                color=discord.Color.red()
            )

            if event.error_type:
                embed.add_field(name="Type", value=event.error_type, inline=True)

            await channel.send(embed=embed)

        except Exception as e:
            logger.error(f"Error sending error message: {e}", exc_info=True)

    async def _handle_voice_output(self, event: VoiceOutputEvent):
        """Handle voice output - enqueue for playback"""
        try:
            channel_id = event.channel_id

            # Check if bot is in voice channel
            if channel_id not in self.voice_clients:
                logger.debug(f"Not in voice for {channel_id}, skipping audio")
                return

            voice_client = self.voice_clients[channel_id]

            # Check if any non-bot members in voice
            if not any(not m.bot for m in voice_client.channel.members):
                logger.debug(f"No listeners in voice channel, skipping audio")
                return

            # Enqueue for playback
            if channel_id in self.voice_output_queues:
                await self.voice_output_queues[channel_id].put(event)
                logger.info(f"Enqueued voice output: {len(event.audio_bytes)} bytes")

        except Exception as e:
            logger.error(f"Error handling voice output: {e}", exc_info=True)

    async def _voice_player_loop(self, channel_id: str):
        """
        Background task that plays queued audio sequentially.
        Prevents overlapping playback.
        """
        logger.info(f"Voice player started for {channel_id}")

        while True:
            try:
                # Wait for next audio chunk
                event = await self.voice_output_queues[channel_id].get()

                # Check voice client still exists
                if channel_id not in self.voice_clients:
                    logger.warning(f"Voice client gone for {channel_id}, stopping player")
                    break

                voice_client = self.voice_clients[channel_id]

                # Wait for any current playback to finish
                while voice_client.is_playing():
                    await asyncio.sleep(0.1)

                # Create audio source
                audio_source = self._create_audio_source(
                    event.audio_bytes,
                    event.sample_rate
                )

                # Play audio
                voice_client.play(
                    audio_source,
                    after=lambda e: logger.error(f"Playback error: {e}") if e else None
                )

                logger.info(f"Playing voice chunk: {event.original_text[:50]}...")

            except asyncio.CancelledError:
                logger.info(f"Voice player cancelled for {channel_id}")
                break
            except Exception as e:
                logger.error(f"Voice player error: {e}", exc_info=True)
                await asyncio.sleep(1)  # Brief pause before retry

    def _create_audio_source(self, audio_bytes: bytes, sample_rate: int = 24000):
        """
        Convert Kokoro PCM to Discord-compatible audio.

        Kokoro: 16-bit PCM mono @ 24kHz
        Discord: 48kHz stereo preferred

        Uses FFmpeg to resample and convert to stereo.
        """
        try:
            import soundfile as sf

            # Convert bytes to numpy array
            audio_array = np.frombuffer(audio_bytes, dtype=np.int16)

            # Create WAV in memory
            wav_buffer = io.BytesIO()
            sf.write(wav_buffer, audio_array, sample_rate, format='WAV', subtype='PCM_16')
            wav_buffer.seek(0)

            # FFmpeg: resample 24kHz→48kHz, mono→stereo
            # Input: WAV from pipe, Output: raw PCM s16le 48kHz stereo
            return discord.FFmpegPCMAudio(
                wav_buffer,
                pipe=True,
                before_options='-f wav',
                options='-f s16le -ar 48000 -ac 2'
            )
        except Exception as e:
            logger.error(f"Error creating audio source: {e}", exc_info=True)
            raise

    def _truncate_message(self, content: str, max_length: int = 2000) -> str:
        """Truncate message to Discord's limit"""
        if len(content) <= max_length:
            return content
        return content[:max_length - 4] + "..."

    async def start(self):
        """Start Discord bot"""
        logger.info("Starting Discord gateway")
        await self.bot.start(self.token)

    async def close(self):
        """Close Discord bot"""
        logger.info("Closing Discord gateway")

        # Disconnect from all voice channels
        for voice_client in self.voice_clients.values():
            await voice_client.disconnect()

        await self.bot.close()
