#!/usr/bin/env python3
"""Discord Agent - Main application entry point"""
import asyncio
import logging
import signal
import sys
from pathlib import Path
from typing import Optional

import yaml

from core.bus import EventBus
from core.profiles import ProfileLoader
from orchestrator.session_orchestrator import SessionOrchestrator
from transport.discord_gateway import DiscordGateway
from state.persistence import SessionDB
from state.transcript import TranscriptStore
from voice.providers import ProviderRegistry
from voice.whisper_provider import WhisperProvider
from voice.kokoro_provider import KokoroProvider
from voice.piper_provider import PiperProvider

logger = logging.getLogger(__name__)


class DiscordAgent:
    """Main application controller"""

    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.config: Optional[dict] = None
        self.shutdown_event = asyncio.Event()

        # Components
        self.event_bus: Optional[EventBus] = None
        self.orchestrator: Optional[SessionOrchestrator] = None
        self.gateway: Optional[DiscordGateway] = None
        self.session_db: Optional[SessionDB] = None
        self.transcript_store: Optional[TranscriptStore] = None

    def load_config(self):
        """Load application configuration"""
        logger.info(f"Loading configuration from {self.config_path}")

        with open(self.config_path) as f:
            self.config = yaml.safe_load(f)

        # Validate required fields
        required = ["discord", "profiles_dir", "state_dir"]
        for field in required:
            if field not in self.config:
                raise ValueError(f"Missing required config field: {field}")

        logger.info("Configuration loaded successfully")

    async def initialize(self):
        """Initialize all components"""
        logger.info("Initializing Discord Agent")

        # Paths
        profiles_dir = Path(self.config["profiles_dir"])
        state_dir = Path(self.config["state_dir"])
        state_dir.mkdir(parents=True, exist_ok=True)

        # Event bus
        self.event_bus = EventBus()
        logger.info("Event bus created")

        # Profile loader
        profile_loader = ProfileLoader(profiles_dir)
        logger.info(f"Loaded {len(profile_loader.profiles)} profiles")

        # Persistence layer
        self.session_db = SessionDB(state_dir / "sessions.db")
        await self.session_db.initialize()

        self.transcript_store = TranscriptStore(state_dir / "transcripts.db")
        await self.transcript_store.initialize()

        # Voice providers
        provider_registry = ProviderRegistry()

        # Register STT providers
        if self.config.get("voice", {}).get("stt_enabled", True):
            whisper_config = self.config.get("voice", {}).get("whisper", {})
            whisper = WhisperProvider(
                model=whisper_config.get("model", "small")
            )
            provider_registry.register_stt("whisper", whisper)
            logger.info("Registered Whisper STT provider")

            asyncio.create_task(whisper.preload())
            logger.info("Whisper model pre-loading started")

        # Register TTS providers
        if self.config.get("voice", {}).get("tts_enabled", True):
            kokoro_config = self.config.get("voice", {}).get("kokoro", {})
            kokoro = KokoroProvider(
                voice=kokoro_config.get("voice", "af_sky"),
                lang_code=kokoro_config.get("lang_code", "a")
            )
            provider_registry.register_tts("kokoro", kokoro)
            logger.info("Registered Kokoro TTS provider")

            asyncio.create_task(kokoro.preload())
            logger.info("Kokoro pipeline pre-loading started")

            piper_config = self.config.get("voice", {}).get("piper", {})
            piper_voice_path = piper_config.get("voice_path")
            if piper_voice_path:
                piper = PiperProvider(voice_path=piper_voice_path)
                provider_registry.register_tts("piper", piper)
                logger.info("Registered Piper TTS provider")
            else:
                logger.info("Piper TTS provider skipped (no voice_path configured)")

        # Session orchestrator
        self.orchestrator = SessionOrchestrator(
            event_bus=self.event_bus,
            profile_loader=profile_loader,
            session_db=self.session_db,
            transcript_store=self.transcript_store,
            provider_registry=provider_registry
        )
        self.orchestrator.subscribe_to_events()
        logger.info("Session orchestrator initialized")

        # Discord gateway
        discord_config = self.config["discord"]
        self.gateway = DiscordGateway(
            event_bus=self.event_bus,
            profile_loader=profile_loader,
            token=discord_config["token"],
            command_prefix=discord_config.get("command_prefix", "!")
        )
        logger.info("Discord gateway initialized")

        logger.info("All components initialized successfully")

    async def run(self):
        """Run the application"""
        try:
            # Start event bus
            bus_task = asyncio.create_task(self.event_bus.run())

            # Start Discord gateway
            gateway_task = asyncio.create_task(self.gateway.start())

            # Start cleanup task
            cleanup_task = asyncio.create_task(self._periodic_cleanup())

            logger.info("Discord Agent is running")

            # Wait for shutdown signal
            await self.shutdown_event.wait()

            logger.info("Shutdown signal received")

            # Graceful shutdown
            await self._shutdown()

            # Cancel tasks
            gateway_task.cancel()
            cleanup_task.cancel()
            await self.event_bus.stop()

            # Wait for tasks to complete
            await asyncio.gather(bus_task, gateway_task, cleanup_task, return_exceptions=True)

        except Exception as e:
            logger.error(f"Fatal error: {e}", exc_info=True)
            sys.exit(1)

    async def _periodic_cleanup(self):
        """Periodic cleanup task"""
        while not self.shutdown_event.is_set():
            try:
                # Run cleanup every hour
                await asyncio.sleep(3600)

                logger.info("Running periodic cleanup")
                max_age = self.config.get("session", {}).get("max_age_hours", 24)
                await self.orchestrator.cleanup_old_sessions(max_age)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in cleanup task: {e}", exc_info=True)

    async def _shutdown(self):
        """Graceful shutdown"""
        logger.info("Starting graceful shutdown")

        # Close orchestrator (closes all engines)
        if self.orchestrator:
            await self.orchestrator.shutdown()

        # Close gateway
        if self.gateway:
            await self.gateway.close()

        # Close databases
        if self.session_db:
            await self.session_db.close()

        if self.transcript_store:
            await self.transcript_store.close()

        logger.info("Graceful shutdown complete")

    def handle_signal(self, sig):
        """Handle shutdown signals"""
        logger.info(f"Received signal {sig}")
        self.shutdown_event.set()


async def main():
    """Main entry point"""

    # Parse arguments
    if len(sys.argv) < 2:
        print("Usage: discord_agent.py <config.yaml>")
        sys.exit(1)

    config_path = Path(sys.argv[1])
    if not config_path.exists():
        print(f"Config file not found: {config_path}")
        sys.exit(1)

    # Setup basic logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    # Create application
    app = DiscordAgent(config_path)

    # Load configuration
    app.load_config()

    # Initialize components
    await app.initialize()

    # Setup signal handlers
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda s=sig: app.handle_signal(s))

    # Run application
    await app.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)