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

from core.events import (
    Event, TextInputEvent, VoiceInputEvent, CommandEvent,
    AgentResponseEvent, ToolCallEvent, StatusEvent, ErrorEvent, VoiceOutputEvent
)
from core.bus import EventBus
from core.profiles import ProfileLoader

logger = logging.getLogger(__name__)


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

        # Simple message tracking for multi-turn conversations
        self.response_messages: Dict[str, discord.Message] = {}
        self.response_request_ids: Dict[str, str] = {}  # Track which request each message belongs to

        # Voice output state
        self.voice_output_queues: Dict[str, asyncio.Queue] = {}
        self.voice_player_tasks: Dict[str, asyncio.Task] = {}

        # Setup event handlers
        self._setup_discord_handlers()
        self._subscribe_to_bus()

    def _setup_discord_handlers(self):
        """Setup Discord event handlers"""

        @self.bot.event
        async def on_ready():
            logger.info(f"Discord bot ready: {self.bot.user}")

        @self.bot.event
        async def on_message(message: discord.Message):
            await self._handle_message(message)

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

        # Publish text input event
        logger.info(f"Message from {message.author} in {message.channel}: {message.content[:100]}")

        await self.bus.publish(TextInputEvent(
            channel_id=channel_id,
            user_id=str(message.author.id),
            content=message.content
        ))

    async def _join_voice_channel(self, ctx: commands.Context):
        """Join user's voice channel"""
        if not ctx.author.voice:
            await ctx.send("You must be in a voice channel!")
            return

        voice_channel = ctx.author.voice.channel
        channel_id = str(ctx.channel.id)

        # Check if voice is configured for this channel
        profile = self.profiles.get_profile(channel_id)
        if not profile or not profile.voice or not profile.voice.enabled:
            await ctx.send("Voice is not configured for this channel")
            return

        try:
            # Connect to voice
            voice_client = await voice_channel.connect()
            self.voice_clients[channel_id] = voice_client

            # Start recording
            voice_client.start_recording(
                discord.sinks.WaveSink(),
                self._voice_receive_callback,
                ctx.channel
            )

            # Start voice output queue and player
            self.voice_output_queues[channel_id] = asyncio.Queue()
            self.voice_player_tasks[channel_id] = asyncio.create_task(
                self._voice_player_loop(channel_id)
            )
            logger.info(f"Voice output player started for {channel_id}")

            await ctx.send(f"Joined {voice_channel.name}")
            logger.info(f"Joined voice channel: {voice_channel.name}")

        except Exception as e:
            logger.error(f"Error joining voice: {e}", exc_info=True)
            await ctx.send(f"Failed to join voice: {str(e)}")

    async def _leave_voice_channel(self, ctx: commands.Context):
        """Leave voice channel"""
        channel_id = str(ctx.channel.id)

        if channel_id not in self.voice_clients:
            await ctx.send("Not in a voice channel")
            return

        voice_client = self.voice_clients[channel_id]
        await voice_client.disconnect()
        del self.voice_clients[channel_id]

        if channel_id in self.voice_buffers:
            del self.voice_buffers[channel_id]
        if channel_id in self.last_audio_time:
            del self.last_audio_time[channel_id]

        # Stop voice output player
        if channel_id in self.voice_player_tasks:
            self.voice_player_tasks[channel_id].cancel()
            del self.voice_player_tasks[channel_id]

        if channel_id in self.voice_output_queues:
            del self.voice_output_queues[channel_id]

        await ctx.send("Left voice channel")
        logger.info(f"Left voice channel for {channel_id}")

    async def _reset_session(self, ctx: commands.Context):
        """Reset agent session"""
        channel_id = str(ctx.channel.id)

        await self.bus.publish(CommandEvent(
            channel_id=channel_id,
            user_id=str(ctx.author.id),
            command="reset"
        ))

        await ctx.send("Session reset requested")

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
                if channel_id in self.response_messages:
                    # Append to existing message
                    message = self.response_messages[channel_id]
                    current_content = message.content
                    new_content = current_content + "\n\n" + event.content
                    await message.edit(content=self._truncate_message(new_content))
                else:
                    # Create new message
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

            # Create rich embed for tool call
            embed = discord.Embed(
                title=f"🔧 {event.tool_name}",
                description=f"Status: {event.status or 'running'}",
                color=discord.Color.blue()
            )

            # Add arguments as fields
            for key, value in event.arguments.items():
                value_str = str(value)
                if len(value_str) > 1024:
                    value_str = value_str[:1021] + "..."
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