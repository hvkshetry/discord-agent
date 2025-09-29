"""Event bus for Discord Agent"""
import asyncio
from typing import Callable, Dict, List
import logging

from .events import Event

logger = logging.getLogger(__name__)


class EventBus:
    """Central event bus for all system events"""

    def __init__(self):
        self.queue: asyncio.Queue[Event] = asyncio.Queue()
        self.handlers: Dict[str, List[Callable]] = {}
        self._running = False

    async def publish(self, event: Event):
        """Publish event to bus"""
        await self.queue.put(event)
        logger.debug(f"Published {event.type} for channel {event.channel_id}")

    def subscribe(self, event_type: str, handler: Callable):
        """Subscribe handler to event type"""
        self.handlers.setdefault(event_type, []).append(handler)
        logger.info(f"Subscribed handler to {event_type}")

    async def run(self):
        """Event loop - dispatch events to handlers"""
        self._running = True
        logger.info("Event bus started")

        while self._running:
            try:
                event = await self.queue.get()

                # Dispatch to registered handlers
                handlers = self.handlers.get(event.type, [])
                if not handlers:
                    logger.warning(f"No handlers for {event.type}")

                for handler in handlers:
                    asyncio.create_task(self._safe_handle(handler, event))

            except asyncio.CancelledError:
                logger.info("Event bus cancelled")
                break
            except Exception as e:
                logger.error(f"Event bus error: {e}", exc_info=True)

    async def _safe_handle(self, handler: Callable, event: Event):
        """Handle event with error catching"""
        try:
            await handler(event)
        except Exception as e:
            logger.error(
                f"Handler error for {event.type}: {e}",
                exc_info=True,
                extra={"channel_id": event.channel_id}
            )

    async def stop(self):
        """Stop event bus"""
        self._running = False
        # Wake the queue with a sentinel to unblock run()
        from core.events import Event
        await self.queue.put(Event(type="__stop__", channel_id="__system__"))
        logger.info("Event bus stopping")