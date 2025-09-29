"""AgentEngine protocol definition"""
from typing import Protocol, AsyncIterator, Optional, List
from core.events import Event, Attachment


class AgentEngine(Protocol):
    """Unified interface for CLI engines (Codex, Claude Code, etc.)"""

    async def start(self, config_home: str) -> None:
        """
        Spawn CLI process with configuration.

        Args:
            config_home: Path to engine configuration directory
                        (e.g., ~/.codex, ~/.claude)
        """
        ...

    async def send_input(
        self,
        text: str,
        session_id: str,
        conversation_id: Optional[str] = None,
        attachments: Optional[List[Attachment]] = None
    ) -> AsyncIterator[Event]:
        """
        Send prompt to engine and stream normalized events.

        Args:
            text: User input text
            session_id: Session identifier for context
            conversation_id: Optional conversation ID for multi-turn (Codex)
            attachments: Optional list of file attachments

        Yields:
            Event: Normalized events (AgentResponseEvent, ToolCallEvent, etc.)
        """
        ...

    async def close(self) -> None:
        """Cleanup and close engine process"""
        ...