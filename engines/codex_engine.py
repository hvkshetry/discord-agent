"""Codex CLI engine adapter"""
import asyncio
import json
import logging
import os
from typing import AsyncIterator, Optional, Dict
from pathlib import Path

from core.events import Event, AgentResponseEvent, ToolCallEvent, StatusEvent, ErrorEvent

logger = logging.getLogger(__name__)


class CodexEngine:
    """Codex CLI adapter using MCP JSON-RPC over stdio"""

    def __init__(self):
        self.process: Optional[asyncio.subprocess.Process] = None
        self.config_home: Optional[str] = None
        self.request_id = 0
        self.session_ids: Dict[str, str] = {}  # Map request_id -> session_id (both strings)
        self.sent_deltas: Dict[str, bool] = {}  # Track if we sent deltas for request
        self.delta_buffers: Dict[str, str] = {}  # Accumulate deltas per request

    def _normalize_request_id(self, req_id) -> Optional[str]:
        """Normalize request ID to string for consistent tracking"""
        if req_id is None:
            return None
        return str(req_id)

    async def start(self, config_home: str) -> None:
        """Spawn Codex process"""
        self.config_home = config_home
        logger.info(f"Starting Codex engine with config: {config_home}")

        env = {
            **os.environ,
            "CODEX_HOME": config_home
        }

        try:
            self.process = await asyncio.create_subprocess_exec(
                "codex",
                "mcp",
                "serve",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env
            )
            logger.info(f"Codex process started: PID {self.process.pid}")

            # Start stderr reader
            asyncio.create_task(self._read_stderr())
        except Exception as e:
            logger.error(f"Failed to start Codex: {e}")
            raise

    async def send_input(self, text: str, session_id: str, conversation_id: Optional[str] = None) -> AsyncIterator[Event]:
        """Send prompt and stream events"""
        if not self.process or not self.process.stdin:
            raise RuntimeError("Codex process not started")

        self.request_id += 1

        # Use codex-reply for multi-turn conversations
        if conversation_id:
            request = {
                "jsonrpc": "2.0",
                "id": self.request_id,
                "method": "tools/call",
                "params": {
                    "name": "codex-reply",
                    "arguments": {
                        "conversationId": conversation_id,
                        "prompt": text
                    }
                }
            }
            logger.info(f"Continuing Codex conversation: {conversation_id}")
        else:
            request = {
                "jsonrpc": "2.0",
                "id": self.request_id,
                "method": "tools/call",
                "params": {
                    "name": "codex",
                    "arguments": {
                        "prompt": text,
                        "approval-policy": "never",
                        "sandbox": "workspace-write"
                    }
                }
            }
            logger.info("Starting new Codex conversation")

        logger.debug(f"Sending to Codex: {text[:100]}...")

        try:
            # Send request
            self.process.stdin.write(json.dumps(request).encode() + b"\n")
            await self.process.stdin.drain()

            # Read responses
            async for event in self._read_responses(session_id):
                yield event

        except Exception as e:
            logger.error(f"Error communicating with Codex: {e}")
            yield ErrorEvent(
                channel_id=session_id,
                message=f"Engine error: {str(e)}",
                error_type=type(e).__name__
            )

    async def _read_responses(self, session_id: str) -> AsyncIterator[Event]:
        """Read and parse Codex stdout"""
        if not self.process or not self.process.stdout:
            return

        while True:
            try:
                line = await self.process.stdout.readline()
                if not line:
                    break

                line_str = line.decode().strip()
                if not line_str:
                    continue

                # Log all output for debugging
                logger.info(f"Codex stdout: {line_str}")

                # Skip non-JSON lines
                if not line_str.startswith("{"):
                    logger.warning(f"Skipping non-JSON line: {line_str}")
                    continue

                obj = json.loads(line_str)

                # Handle different message types
                if "method" in obj:
                    # Notification
                    if obj["method"] == "codex/event":
                        async for event in self._handle_codex_event(obj, session_id):
                            yield event
                elif "result" in obj:
                    # Response
                    async for event in self._handle_result(obj, session_id):
                        yield event
                elif "error" in obj:
                    # Error
                    yield ErrorEvent(
                        channel_id=session_id,
                        message=obj["error"].get("message", "Unknown error"),
                        error_type="CodexError"
                    )
                    break

                # Check if this is final response
                if obj.get("id") == self.request_id and "result" in obj:
                    break

            except json.JSONDecodeError:
                continue
            except Exception as e:
                logger.error(f"Error reading Codex output: {e}")
                break

    async def _handle_codex_event(self, obj: dict, session_id: str) -> AsyncIterator[Event]:
        """Handle codex/event notifications"""
        params = obj.get("params", {})
        msg = params.get("msg", {})
        event_type = msg.get("type")

        # Extract request_id from metadata for session tracking
        meta = params.get("_meta", {})
        request_id = self._normalize_request_id(meta.get("requestId"))

        # Capture session_id from session_configured event
        if event_type == "session_configured":
            codex_session_id = msg.get("session_id")
            if codex_session_id and request_id:
                self.session_ids[request_id] = codex_session_id
                logger.info(f"Captured Codex session_id: {codex_session_id} for request {request_id}")

        # Handle agent messages (complete) - Emit accumulated deltas
        elif event_type == "agent_message":
            # Get accumulated deltas
            content = ""
            if request_id and request_id in self.delta_buffers:
                content = self.delta_buffers[request_id]
                del self.delta_buffers[request_id]  # Clear buffer
            elif msg.get("message"):  # Fallback if no deltas
                content = msg.get("message", "")

            if content:
                # Track that we sent message for this request
                if request_id:
                    self.sent_deltas[request_id] = True

                # Include conversation ID in metadata
                conversation_id = self.session_ids.get(request_id) if request_id else None
                metadata = {}
                if request_id:
                    metadata["requestId"] = request_id
                if conversation_id:
                    metadata["conversationId"] = conversation_id

                # Emit complete message (not final unless it's the last one)
                yield AgentResponseEvent(
                    channel_id=session_id,
                    content=content,
                    is_final=False,
                    metadata=metadata if metadata else None
                )

        # Handle agent message deltas (streaming) - Accumulate, don't emit yet
        elif event_type == "agent_message_delta":
            delta = msg.get("delta", "")
            if delta and request_id:
                # Accumulate deltas in buffer
                if request_id not in self.delta_buffers:
                    self.delta_buffers[request_id] = ""
                self.delta_buffers[request_id] += delta

        # Handle agent reasoning (complete) - log only, don't spam Discord
        elif event_type == "agent_reasoning":
            text = msg.get("text", "")
            if text:
                logger.debug(f"Agent reasoning: {text[:100]}...")

        # Handle agent reasoning deltas (streaming) - SKIP: don't spam Discord
        elif event_type == "agent_reasoning_delta":
            pass  # Silently ignore reasoning deltas

        # Handle command execution start
        elif event_type == "exec_command_begin":
            command = msg.get("command", [])
            if command:
                yield ToolCallEvent(
                    channel_id=session_id,
                    tool_name="bash",
                    arguments={"command": " ".join(command) if isinstance(command, list) else str(command)},
                    status="started"
                )

        # Handle MCP tool calls
        elif event_type == "mcp_tool_call_begin":
            invocation = msg.get("invocation", {})
            tool = invocation.get("tool", "unknown")
            server = invocation.get("server", "")
            display_name = f"{server}/{tool}" if server else tool
            yield ToolCallEvent(
                channel_id=session_id,
                tool_name=display_name,
                arguments=invocation.get("arguments", {}),
                status="started"
            )

        elif event_type == "mcp_tool_call_end":
            invocation = msg.get("invocation", {})
            tool = invocation.get("tool", "unknown")
            server = invocation.get("server", "")
            display_name = f"{server}/{tool}" if server else tool
            yield ToolCallEvent(
                channel_id=session_id,
                tool_name=display_name,
                arguments={},
                status="completed"
            )

        # Handle task completion - SKIP: redundant with final result
        elif event_type == "task_complete":
            logger.debug(f"Task complete: {msg.get('last_agent_message', '')[:50]}")

        # Ignore unknown event types gracefully
        elif event_type:
            logger.debug(f"Ignoring unknown Codex event type: {event_type}")

    async def _handle_result(self, obj: dict, session_id: str) -> AsyncIterator[Event]:
        """Handle final JSON-RPC result"""
        result = obj.get("result", {})
        request_id = self._normalize_request_id(obj.get("id"))

        # If we sent streaming messages, just send final completion signal
        if request_id and self.sent_deltas.get(request_id, False):
            logger.debug(f"Sending final completion for request {request_id}")
            # Clean up tracking
            if request_id in self.sent_deltas:
                del self.sent_deltas[request_id]
            if request_id in self.delta_buffers:
                del self.delta_buffers[request_id]

            # Send empty final event to signal completion
            conversation_id = self.session_ids.get(request_id) if request_id else None
            metadata = {}
            if request_id:
                metadata["requestId"] = request_id
            if conversation_id:
                metadata["conversationId"] = conversation_id

            yield AgentResponseEvent(
                channel_id=session_id,
                content="",
                is_final=True,
                metadata=metadata if metadata else None
            )
            return

        # Check for error result
        if "error" in result:
            error_msg = result.get("error", "Unknown error")
            yield ErrorEvent(
                channel_id=session_id,
                message=f"Codex error: {error_msg}",
                error_type="CodexError"
            )
            return

        # Check is_error flag (optional field, absent = success)
        is_error = result.get("is_error", False)
        if is_error:
            # Extract error message from content if available
            content_list = result.get("content", [])
            error_text = ""
            for item in content_list:
                if isinstance(item, dict) and item.get("type") == "text":
                    error_text += item.get("text", "")

            yield ErrorEvent(
                channel_id=session_id,
                message=f"Codex error: {error_text or 'Unknown error'}",
                error_type="CodexError"
            )
            return

        # Extract conversationId from session_ids mapping
        conversation_id = self.session_ids.get(request_id) if request_id else None
        metadata = {}
        if request_id:
            metadata["requestId"] = request_id
        if conversation_id:
            metadata["conversationId"] = conversation_id
            logger.info(f"Adding conversation ID to response: {conversation_id}")

        # Extract all text content
        content_list = result.get("content", [])
        text_parts = []

        for item in content_list:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text", "")
                if text:
                    text_parts.append(text)

        # Yield final response if we have content (non-streaming case)
        full_text = " ".join(text_parts)
        if full_text:
            logger.info(f"Emitting non-streamed final result for request {request_id}")
            yield AgentResponseEvent(
                channel_id=session_id,
                content=full_text,
                is_final=True,
                metadata=metadata if metadata else None
            )

    async def _read_stderr(self):
        """Read stderr in background and log to telemetry"""
        if not self.process or not self.process.stderr:
            return

        try:
            async for line in self.process.stderr:
                line_str = line.decode().strip()
                if line_str:
                    logger.debug(f"Codex stderr: {line_str}")
        except Exception as e:
            logger.error(f"Error reading Codex stderr: {e}")

    async def close(self) -> None:
        """Close Codex process"""
        if self.process:
            logger.info("Closing Codex engine")
            try:
                self.process.terminate()
                await asyncio.wait_for(self.process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("Codex didn't terminate, killing")
                self.process.kill()
                await self.process.wait()
            self.process = None