"""Event types for Discord Agent event bus"""
from pydantic import BaseModel, Field
from typing import Literal, Optional, Any, Dict
from datetime import datetime


class Event(BaseModel):
    """Base event type"""
    type: str
    channel_id: str
    timestamp: float = Field(default_factory=lambda: datetime.now().timestamp())


class TextInputEvent(Event):
    """User text message event"""
    type: Literal["text_input"] = "text_input"
    user_id: str
    content: str


class VoiceInputEvent(Event):
    """User voice audio event"""
    type: Literal["voice_input"] = "voice_input"
    user_id: str
    audio_data: bytes


class AgentResponseEvent(Event):
    """Agent response event"""
    type: Literal["agent_response"] = "agent_response"
    content: str
    is_final: bool = False
    metadata: Optional[Dict[str, Any]] = None


class ToolCallEvent(Event):
    """Agent tool call event"""
    type: Literal["tool_call"] = "tool_call"
    tool_name: str
    arguments: Dict[str, Any]
    status: Optional[str] = None


class StatusEvent(Event):
    """Status update event"""
    type: Literal["status"] = "status"
    message: str
    level: str = "info"


class ErrorEvent(Event):
    """Error event"""
    type: Literal["error"] = "error"
    message: str
    error_type: Optional[str] = None
    traceback: Optional[str] = None


class CommandEvent(Event):
    """Slash command event"""
    type: Literal["command"] = "command"
    user_id: str
    command: str
    arguments: Dict[str, Any] = Field(default_factory=dict)


class VoiceOutputEvent(Event):
    """Voice audio output for Discord voice channel playback"""
    type: Literal["voice_output"] = "voice_output"
    audio_bytes: bytes
    sample_rate: int = 24000
    voice_name: str
    original_text: str
    metadata: Optional[Dict[str, Any]] = None


class VoiceConnectionEvent(Event):
    """Voice channel connection state change event"""
    type: Literal["voice_connection"] = "voice_connection"
    state: Literal["joined", "left"]
    voice_channel_id: str
    text_channel_id: str