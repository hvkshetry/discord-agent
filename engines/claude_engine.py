"""Claude Code CLI engine adapter"""
import asyncio
import json
import logging
import os
from typing import AsyncIterator, Optional

from core.events import Event, AgentResponseEvent, ToolCallEvent, StatusEvent, ErrorEvent

logger = logging.getLogger(__name__)


class ClaudeEngine:
    """Claude Code CLI adapter using streaming JSON"""

    def __init__(self):
        self.process: Optional[asyncio.subprocess.Process] = None
        self.config_home: Optional[str] = None

    async def start(self, config_home: str) -> None:
        """Spawn Claude Code process"""
        self.config_home = config_home
        logger.info(f"Starting Claude Code engine with config: {config_home}")

        env = {
            **os.environ,
            "CLAUDE_HOME": config_home
        }

        try:
            self.process = await asyncio.create_subprocess_exec(
                "claude",
                "-p",
                "--output-format=stream-json",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env
            )
            logger.info(f"Claude Code process started: PID {self.process.pid}")

            # Start stderr reader
            asyncio.create_task(self._read_stderr())
        except Exception as e:
            logger.error(f"Failed to start Claude Code: {e}")
            raise

    async def send_input(self, text: str, session_id: str, conversation_id: Optional[str] = None) -> AsyncIterator[Event]:
        """Send prompt and stream events"""
        if not self.process or not self.process.stdin:
            raise RuntimeError("Claude Code process not started")

        logger.debug(f"Sending to Claude Code: {text[:100]}...")

        try:
            # Send prompt
            self.process.stdin.write(text.encode() + b"\n")
            await self.process.stdin.drain()

            # Read streaming responses
            async for event in self._read_responses(session_id):
                yield event

        except Exception as e:
            logger.error(f"Error communicating with Claude Code: {e}")
            yield ErrorEvent(
                channel_id=session_id,
                message=f"Engine error: {str(e)}",
                error_type=type(e).__name__
            )

    async def _read_responses(self, session_id: str) -> AsyncIterator[Event]:
        """Read and parse Claude Code streaming JSON"""
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

                # Skip non-JSON lines
                if not line_str.startswith("{"):
                    continue

                obj = json.loads(line_str)

                # Handle different message types
                msg_type = obj.get("type")

                if msg_type == "content_block_delta":
                    # Streaming response
                    delta = obj.get("delta", {})
                    text = delta.get("text", "")
                    if text:
                        yield AgentResponseEvent(
                            channel_id=session_id,
                            content=text,
                            is_final=False
                        )

                elif msg_type == "message_start":
                    # Message starting
                    yield StatusEvent(
                        channel_id=session_id,
                        message="Processing..."
                    )

                elif msg_type == "message_stop":
                    # Message complete - use status event instead of empty content
                    yield StatusEvent(
                        channel_id=session_id,
                        message="Response complete"
                    )
                    break

                elif msg_type == "tool_use":
                    # Tool call
                    tool_name = obj.get("name", "unknown")
                    tool_input = obj.get("input", {})
                    yield ToolCallEvent(
                        channel_id=session_id,
                        tool_name=tool_name,
                        arguments=tool_input,
                        status="started"
                    )

                elif msg_type == "tool_result":
                    # Tool result
                    yield StatusEvent(
                        channel_id=session_id,
                        message=f"Tool completed: {obj.get('tool_use_id', 'unknown')}"
                    )

                elif msg_type == "error":
                    # Error
                    error_msg = obj.get("error", {}).get("message", "Unknown error")
                    yield ErrorEvent(
                        channel_id=session_id,
                        message=error_msg,
                        error_type="ClaudeError"
                    )
                    break

            except json.JSONDecodeError:
                continue
            except Exception as e:
                logger.error(f"Error reading Claude Code output: {e}")
                break

    async def _read_stderr(self):
        """Read stderr in background and log to telemetry"""
        if not self.process or not self.process.stderr:
            return

        try:
            async for line in self.process.stderr:
                line_str = line.decode().strip()
                if line_str:
                    logger.debug(f"Claude stderr: {line_str}")
        except Exception as e:
            logger.error(f"Error reading Claude stderr: {e}")

    async def close(self) -> None:
        """Close Claude Code process"""
        if self.process:
            logger.info("Closing Claude Code engine")
            try:
                self.process.terminate()
                await asyncio.wait_for(self.process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("Claude Code didn't terminate, killing")
                self.process.kill()
                await self.process.wait()
            self.process = None