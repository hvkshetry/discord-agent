"""Claude Code CLI engine adapter"""
import asyncio
import json
import logging
import os
from typing import AsyncIterator, Optional, List

from core.events import Event, AgentResponseEvent, ToolCallEvent, StatusEvent, ErrorEvent, Attachment

logger = logging.getLogger(__name__)


class ClaudeEngine:
    """Claude Code CLI adapter using streaming JSON (one-shot execution per message)"""

    def __init__(self):
        self.config_home: Optional[str] = None
        self.current_process: Optional[asyncio.subprocess.Process] = None

    async def start(self, config_home: str) -> None:
        """Initialize engine configuration (Claude Code uses one-shot processes, not persistent)"""
        self.config_home = config_home
        logger.info(f"Initialized Claude Code engine with config: {config_home}")
        # Note: No persistent process - we spawn new process per message

    async def send_input(
        self,
        text: str,
        session_id: str,
        conversation_id: Optional[str] = None,
        attachments: Optional[List[Attachment]] = None
    ) -> AsyncIterator[Event]:
        """Spawn Claude Code process with prompt as CLI argument and stream events"""

        # Build prompt with file paths (same approach as Codex)
        if attachments:
            attachment_info = "\n\nAttached files:\n" + "\n".join([
                f"- {att.filename} ({att.content_type or 'unknown type'}): {att.path}"
                for att in attachments
            ])
            prompt = (text or "Please analyze the attached files.") + attachment_info
            logger.info(f"Added {len(attachments)} file path(s) to prompt")
        else:
            prompt = text

        logger.debug(f"Spawning Claude Code with prompt: {prompt[:100]}...")

        # Build command arguments
        cmd_args = [
            "claude",
            "-p",
            prompt,  # Prompt as CLI argument (NOT stdin!)
            "--output-format",
            "stream-json",
            "--model",
            "sonnet",
            "--verbose"  # Required for stream-json
        ]

        # Add resume flag if we have a conversation ID
        if conversation_id:
            cmd_args.extend(["--resume", conversation_id])
            logger.info(f"Resuming Claude Code session: {conversation_id}")

        # Don't override CLAUDE_CONFIG_DIR - let it use ~/.claude/ for auth
        # Just set working directory to the workspace
        env = os.environ.copy()

        # Set MCP timeouts to prevent hanging
        env["MCP_TIMEOUT"] = "30000"  # 30 second startup timeout
        env["MCP_TOOL_TIMEOUT"] = "60000"  # 60 second tool timeout

        try:
            # Spawn process for this message
            # Set large buffer limit for stdout/stderr to handle big JSON lines (e.g., large tool results)
            self.current_process = await asyncio.create_subprocess_exec(
                *cmd_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=self.config_home,  # Run in workspace directory (has .mcp.json, CLAUDE.md)
                limit=10 * 1024 * 1024  # 10MB buffer limit (default is 64KB)
            )
            logger.info(f"Claude Code process started: PID {self.current_process.pid}")

            # Start stderr reader in background
            stderr_task = asyncio.create_task(self._read_stderr(self.current_process))

            # Send initial status while waiting for Claude to start
            yield StatusEvent(
                channel_id=session_id,
                message="Starting Claude Code..." if not conversation_id else "Resuming session..."
            )

            # Read streaming responses
            async for event in self._read_responses(session_id, self.current_process):
                yield event

            # Wait for process to complete
            await self.current_process.wait()
            logger.info(f"Claude Code process completed: PID {self.current_process.pid}")

            # Clean up
            stderr_task.cancel()
            self.current_process = None

        except Exception as e:
            logger.error(f"Error running Claude Code: {e}")
            if self.current_process:
                self.current_process.kill()
                self.current_process = None
            yield ErrorEvent(
                channel_id=session_id,
                message=f"Engine error: {str(e)}",
                error_type=type(e).__name__
            )

    async def _read_responses(self, session_id: str, process: asyncio.subprocess.Process) -> AsyncIterator[Event]:
        """Read and parse Claude Code streaming JSON"""
        if not process or not process.stdout:
            return

        captured_session_id = None

        while True:
            try:
                line = await process.stdout.readline()
                if not line:
                    break

                line_str = line.decode().strip()
                if not line_str:
                    continue

                # Log all output for debugging
                logger.info(f"Claude Code stdout: {line_str[:200]}...")

                # Skip non-JSON lines
                if not line_str.startswith("{"):
                    continue

                obj = json.loads(line_str)

                # Handle different message types
                msg_type = obj.get("type")

                if msg_type == "system":
                    # System initialization message - capture session_id
                    captured_session_id = obj.get("session_id")
                    if captured_session_id:
                        logger.info(f"Captured Claude session_id: {captured_session_id}")

                    yield StatusEvent(
                        channel_id=session_id,
                        message="Initializing..."
                    )

                elif msg_type == "assistant":
                    # Assistant response message
                    message = obj.get("message", {})
                    content_blocks = message.get("content", [])

                    # Extract text from all content blocks
                    text_parts = []
                    for block in content_blocks:
                        if isinstance(block, dict):
                            block_type = block.get("type")
                            if block_type == "text":
                                text = block.get("text", "")
                                if text:
                                    text_parts.append(text)
                            elif block_type == "tool_use":
                                # Tool call within assistant message
                                tool_name = block.get("name", "unknown")
                                tool_input = block.get("input", {})
                                yield ToolCallEvent(
                                    channel_id=session_id,
                                    tool_name=tool_name,
                                    arguments=tool_input,
                                    status="started"
                                )

                    # Emit response with text content
                    if text_parts:
                        full_text = "\n".join(text_parts)

                        # Include session_id in metadata for conversation continuity
                        metadata = {}
                        if captured_session_id:
                            metadata["conversationId"] = captured_session_id

                        yield AgentResponseEvent(
                            channel_id=session_id,
                            content=full_text,
                            is_final=False,
                            metadata=metadata if metadata else None
                        )

                elif msg_type == "user":
                    # Tool result message (user role = tool results)
                    message = obj.get("message", {})
                    content_blocks = message.get("content", [])

                    for block in content_blocks:
                        if isinstance(block, dict) and block.get("type") == "tool_result":
                            tool_use_id = block.get("tool_use_id", "unknown")
                            yield StatusEvent(
                                channel_id=session_id,
                                message=f"Tool completed: {tool_use_id}"
                            )

                elif msg_type == "result":
                    # Final result message
                    subtype = obj.get("subtype")
                    is_error = obj.get("is_error", False)
                    result_text = obj.get("result", "")
                    duration_api_ms = obj.get("duration_api_ms", 0)

                    if is_error:
                        yield ErrorEvent(
                            channel_id=session_id,
                            message=f"Task failed: {result_text}",
                            error_type="ClaudeTaskError"
                        )
                    else:
                        # Check if we got empty result without any assistant messages
                        # This indicates slash command not found or immediate failure
                        if not result_text and duration_api_ms == 0:
                            logger.warning(f"Empty result with no API calls - possible invalid slash command")
                            yield ErrorEvent(
                                channel_id=session_id,
                                message="No response generated. Check if slash command exists (e.g., /meetingminutes not /meetingminites).",
                                error_type="EmptyResultError"
                            )
                        else:
                            # Send final completion signal
                            metadata = {}
                            if captured_session_id:
                                metadata["conversationId"] = captured_session_id

                            yield AgentResponseEvent(
                                channel_id=session_id,
                                content="",  # Empty final signal
                                is_final=True,
                                metadata=metadata if metadata else None
                            )

                    logger.info(f"Task completed: {subtype} (error={is_error}, api_ms={duration_api_ms})")
                    break

            except json.JSONDecodeError as e:
                logger.warning(f"JSON decode error: {e} - Line: {line_str[:100]}")
                continue
            except Exception as e:
                logger.error(f"Error reading Claude Code output: {e}", exc_info=True)
                break

    async def _read_stderr(self, process: asyncio.subprocess.Process):
        """Read stderr in background and log to telemetry"""
        if not process or not process.stderr:
            return

        try:
            async for line in process.stderr:
                line_str = line.decode().strip()
                if line_str:
                    logger.debug(f"Claude stderr: {line_str}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Error reading Claude stderr: {e}")

    async def close(self) -> None:
        """Close Claude Code process if running"""
        if self.current_process:
            logger.info("Closing Claude Code engine")
            try:
                self.current_process.terminate()
                await asyncio.wait_for(self.current_process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("Claude Code didn't terminate, killing")
                self.current_process.kill()
                await self.current_process.wait()
            self.current_process = None