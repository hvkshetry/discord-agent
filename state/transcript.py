"""Replayable transcript store for session events"""
import aiosqlite
from pathlib import Path
from typing import List, Optional
import json
import logging

from core.events import Event

logger = logging.getLogger(__name__)


class TranscriptStore:
    """Stores replayable event transcripts per channel"""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db: Optional[aiosqlite.Connection] = None

    async def initialize(self):
        """Initialize database schema"""
        self.db = await aiosqlite.connect(self.db_path)

        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS transcripts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id TEXT NOT NULL,
                timestamp REAL NOT NULL,
                event_type TEXT NOT NULL,
                event_data TEXT NOT NULL
            )
        """)

        # Create index separately
        await self.db.execute("""
            CREATE INDEX IF NOT EXISTS idx_channel_timestamp
            ON transcripts(channel_id, timestamp)
        """)

        await self.db.commit()
        logger.info(f"Transcript store initialized: {self.db_path}")

    async def append(self, channel_id: str, event: Event):
        """Append event to transcript"""
        if not self.db:
            await self.initialize()

        await self.db.execute(
            """
            INSERT INTO transcripts (channel_id, timestamp, event_type, event_data)
            VALUES (?, ?, ?, ?)
            """,
            (channel_id, event.timestamp, event.type, event.json())
        )
        await self.db.commit()

    async def append_dict(self, channel_id: str, event_dict: dict):
        """Append event dict to transcript (for events with non-serializable fields)"""
        if not self.db:
            await self.initialize()

        await self.db.execute(
            """
            INSERT INTO transcripts (channel_id, timestamp, event_type, event_data)
            VALUES (?, ?, ?, ?)
            """,
            (channel_id, event_dict['timestamp'], event_dict['type'], json.dumps(event_dict))
        )
        await self.db.commit()

    async def get_transcript(
        self,
        channel_id: str,
        since: float = 0,
        limit: Optional[int] = None
    ) -> List[Event]:
        """
        Retrieve events for replay.

        Args:
            channel_id: Channel ID
            since: Only events after this timestamp
            limit: Maximum number of events

        Returns:
            List of events
        """
        if not self.db:
            await self.initialize()

        query = """
            SELECT event_type, event_data
            FROM transcripts
            WHERE channel_id = ? AND timestamp > ?
            ORDER BY timestamp
        """
        params = [channel_id, since]

        if limit:
            query += " LIMIT ?"
            params.append(limit)

        cursor = await self.db.execute(query, params)
        rows = await cursor.fetchall()

        events = []
        for row in rows:
            try:
                event_data = json.loads(row[1])
                # Reconstruct event (simplified - would need proper deserialization)
                events.append(event_data)
            except Exception as e:
                logger.error(f"Error deserializing event: {e}")

        return events

    async def clear_channel(self, channel_id: str):
        """Clear transcript for channel"""
        if not self.db:
            return

        await self.db.execute(
            "DELETE FROM transcripts WHERE channel_id = ?",
            (channel_id,)
        )
        await self.db.commit()
        logger.info(f"Cleared transcript for channel: {channel_id}")

    async def close(self):
        """Close database connection"""
        if self.db:
            await self.db.close()
            self.db = None