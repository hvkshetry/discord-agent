"""Session persistence with SQLite"""
import aiosqlite
from pathlib import Path
from typing import Optional, Dict, Any
import json
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


class SessionDB:
    """SQLite-backed session storage"""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db: Optional[aiosqlite.Connection] = None

    async def initialize(self):
        """Initialize database schema"""
        self.db = await aiosqlite.connect(self.db_path)

        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                channel_id TEXT PRIMARY KEY,
                agent_type TEXT NOT NULL,
                context TEXT,
                created_at REAL NOT NULL,
                last_activity REAL NOT NULL
            )
        """)
        await self.db.commit()
        logger.info(f"Session database initialized: {self.db_path}")

    async def save_session(
        self,
        channel_id: str,
        agent_type: str,
        context: Optional[str] = None
    ):
        """Save or update session"""
        if not self.db:
            await self.initialize()

        now = datetime.now().timestamp()

        await self.db.execute(
            """
            INSERT OR REPLACE INTO sessions
            (channel_id, agent_type, context, created_at, last_activity)
            VALUES (?, ?, ?, COALESCE(
                (SELECT created_at FROM sessions WHERE channel_id = ?),
                ?
            ), ?)
            """,
            (channel_id, agent_type, context, channel_id, now, now)
        )
        await self.db.commit()

    async def get_session(self, channel_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve session"""
        if not self.db:
            await self.initialize()

        cursor = await self.db.execute(
            """
            SELECT agent_type, context, created_at, last_activity
            FROM sessions
            WHERE channel_id = ?
            """,
            (channel_id,)
        )
        row = await cursor.fetchone()

        if row:
            return {
                "agent_type": row[0],
                "context": row[1],
                "created_at": row[2],
                "last_activity": row[3]
            }
        return None

    async def update_activity(self, channel_id: str):
        """Update last activity timestamp"""
        if not self.db:
            return

        await self.db.execute(
            """
            UPDATE sessions
            SET last_activity = ?
            WHERE channel_id = ?
            """,
            (datetime.now().timestamp(), channel_id)
        )
        await self.db.commit()

    async def clear_session(self, channel_id: str):
        """Delete session"""
        if not self.db:
            return

        await self.db.execute(
            "DELETE FROM sessions WHERE channel_id = ?",
            (channel_id,)
        )
        await self.db.commit()
        logger.info(f"Cleared session for channel: {channel_id}")

    async def cleanup_old_sessions(self, max_age_hours: int = 24):
        """Remove old inactive sessions"""
        if not self.db:
            return

        cutoff = datetime.now().timestamp() - (max_age_hours * 3600)

        result = await self.db.execute(
            """
            DELETE FROM sessions
            WHERE last_activity < ?
            """,
            (cutoff,)
        )
        await self.db.commit()

        count = result.rowcount
        if count > 0:
            logger.info(f"Cleaned up {count} old sessions")

    async def close(self):
        """Close database connection"""
        if self.db:
            await self.db.close()
            self.db = None