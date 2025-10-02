# Cross-Channel Communication Architecture

## Overview

This document outlines how to implement cross-channel communication in the Discord Agent system, allowing agents in different channels to trigger workflows and exchange information.

## Question

**Can a bot in the #engineering text channel trigger a workflow in the #admin channel?**

**Answer: Yes** - and it's significantly easier in the single-bot architecture (Option B) than it would be with multiple separate bots.

## Why Option B is Superior for Cross-Channel Workflows

The event-driven, single-process architecture makes cross-channel communication much simpler than multiple separate bot processes:

| Aspect | Option B (Single Bot) | Option A (Multiple Bots) |
|--------|----------------------|--------------------------|
| **Routing** | Internal event bus (instant, reliable) | Inter-process communication (HTTP/Redis/queue) |
| **Shared State** | Direct SQLite access | Need distributed database or API |
| **Complexity** | Simple function calls | Network calls, timeouts, retries |
| **Failure Modes** | Process crash (affects all) | Network partitions, partial failures |
| **Observability** | Single event log | Distributed tracing needed |
| **Latency** | Microseconds (in-memory) | Milliseconds (network) |

## Implementation Options

### 1. MCP Server Approach (Recommended)

Create a Discord Bridge MCP server that exposes cross-channel operations as tools.

#### Tools to Implement

```python
# discord-bridge-mcp/server.py

@tool
async def send_to_channel(channel_name: str, message: str) -> str:
    """
    Send a message to another Discord channel.

    Args:
        channel_name: Target channel name (e.g., "admin", "engineering")
        message: Message content to send

    Returns:
        Confirmation message
    """
    # Implementation publishes to event bus or calls Discord API

@tool
async def trigger_agent(
    channel_name: str,
    prompt: str,
    context: dict = None,
    return_response: bool = False
) -> str:
    """
    Trigger the agent in another channel with a prompt.

    Args:
        channel_name: Target channel name
        prompt: The prompt to send to the target agent
        context: Optional metadata (requester, original_channel, etc.)
        return_response: If True, wait for and return agent's response

    Returns:
        Status message or agent response
    """
    # Publishes TextInputEvent to event bus with target channel_id

@tool
async def get_channel_state(channel_name: str, key: str) -> str:
    """
    Read shared state written by another channel's agent.

    Args:
        channel_name: Source channel name
        key: State key to retrieve

    Returns:
        State value
    """
    # Reads from shared SessionDB or custom state store

@tool
async def set_channel_state(key: str, value: str, ttl_hours: int = 24) -> str:
    """
    Write state that other channel agents can read.

    Args:
        key: State key
        value: State value (JSON string)
        ttl_hours: How long to keep state

    Returns:
        Confirmation
    """
    # Writes to shared state store with expiration
```

#### Configuration

```yaml
# channels/engineering.yaml
channel:
  names: ["engineering"]

engine:
  type: codex
  config_home: ~/agents/engineering/.codex

mcp_servers:
  - name: discord-bridge
    command: python
    args: ["/path/to/discord-bridge-mcp/server.py"]
    env:
      EVENT_BUS_SOCKET: /tmp/discord-agent-bus.sock
    enabled: true

cross_channel:
  # Permission model
  can_trigger: ["admin", "general"]  # Whitelist of channels this agent can trigger
  can_be_triggered_by: ["admin"]  # Who can trigger this agent
  max_trigger_depth: 3  # Prevent infinite loops
  trigger_timeout: 60  # Seconds to wait for triggered agent response
```

#### How It Works

1. Engineering agent needs admin approval
2. Calls MCP tool: `trigger_agent("admin", "Approve deployment v1.2.3", context={"requester": "engineering"})`
3. MCP server validates permissions (engineering allowed to trigger admin?)
4. MCP server publishes `TextInputEvent` to event bus with admin channel_id
5. SessionOrchestrator routes event to admin agent
6. Admin agent processes and responds in #admin channel
7. (Optional) Admin agent calls `send_to_channel("engineering", "✅ Approved")` to notify originating channel

### 2. Direct Event Bus Access

Expose event bus operations directly via MCP or agent context injection.

**More flexible but lower-level** - agents can publish any event type, not just messages.

```python
@tool
async def publish_event(event_type: str, channel_id: str, data: dict) -> str:
    """Publish arbitrary event to event bus"""
    # More powerful but requires agents to understand event schema
```

### 3. Shared State Coordination (Pull Model)

Agents read/write to shared SQLite state for passive coordination.

```python
# Agent A (engineering) writes workflow request
await state_db.set("deployment_approval_pending", {
    "build": "v1.2.3",
    "requester": "engineering",
    "timestamp": "2025-10-02T10:30:00Z"
})

# Agent B (admin) polls or gets notified
approval = await state_db.get("deployment_approval_pending")
if approval:
    # Process approval
    await state_db.set("deployment_approved", {"build": approval["build"]})
```

**Better for async workflows** where immediate response isn't needed.

## Real-World Workflow Example

### Scenario: Production Deployment Approval Workflow

```
User in #engineering: "Deploy build v1.2.3 to production"

[Engineering Agent]
├─ Analyzes build status (tests, security scans)
├─ Determines admin approval needed
└─ Calls MCP tool:
   trigger_agent(
     channel_name="admin",
     prompt="Approve production deployment of v1.2.3. Build tests: ✅ Security scan: ✅ Breaking changes: None",
     context={"requester": "engineering", "user": "@john"}
   )

#admin channel:
Bot: 🔔 Request from #engineering (@john)
     Approve production deployment of v1.2.3
     • Build tests: ✅
     • Security scan: ✅
     • Breaking changes: None

[Admin Agent]
├─ Reviews deployment criteria
├─ Checks production schedule
├─ Validates compliance requirements
└─ Responds in #admin: "✅ Approved. Deployment authorized. Maintenance window: 2-4 AM UTC."

[Admin Agent also calls]
send_to_channel("engineering", "✅ Deployment approved by admin. Window: 2-4 AM UTC")

#engineering channel:
Bot: ✅ Deployment approved by admin. Window: 2-4 AM UTC

[Engineering Agent]
├─ Schedules deployment for maintenance window
├─ Executes deployment scripts
└─ Updates both channels with progress:
   - #engineering: "🚀 Deployment started..."
   - #admin: "📊 Deployment v1.2.3 in progress (started by engineering)"
```

## Design Considerations

### Concurrency Safety

✅ **Already handled** - Per-channel locks in SessionOrchestrator prevent concurrent access to same agent.

Multiple agents in different channels can run simultaneously without issues.

### Deadlock Prevention

⚠️ **Need safeguards:**

1. **Max trigger depth**: Limit nested triggers (A→B→C→...) to prevent infinite loops
2. **Timeout**: Triggered agent must respond within timeout (e.g., 60 seconds)
3. **Circular detection**: Track trigger chain, prevent A→B→A cycles
4. **Queue limits**: Prevent trigger flooding

```python
# In MCP server
class TriggerContext:
    chain: List[str]  # ["engineering", "admin"]
    depth: int
    max_depth: int = 3

    def can_trigger(self, target: str) -> bool:
        if self.depth >= self.max_depth:
            return False
        if target in self.chain:  # Circular
            return False
        return True
```

### User Transparency

✅ **Always show trigger source** for user trust:

```python
# When agent is triggered by another channel
message_prefix = f"🔗 Triggered by @{original_user} from #{original_channel}\n\n"
```

Users in target channel should see:
```
#admin
Bot: 🔗 Triggered by @john from #engineering

Approve production deployment of v1.2.3...
```

### Permission Model

**Define allowed interactions in channel profiles:**

```yaml
# channels/admin.yaml
cross_channel:
  can_trigger: ["engineering", "general"]  # Admin can trigger these
  can_be_triggered_by: ["engineering"]     # Only engineering can trigger admin
  require_user_whitelist: true             # Only specific users can trigger
  allowed_users: ["123456789"]             # Discord user IDs
```

**Permission checks in MCP server:**
```python
async def trigger_agent(channel_name: str, prompt: str):
    source_profile = get_current_channel_profile()
    target_profile = get_profile(channel_name)

    # Check if source can trigger target
    if channel_name not in source_profile.cross_channel.can_trigger:
        raise PermissionError(f"Not allowed to trigger #{channel_name}")

    # Check if target allows being triggered by source
    if source_profile.name not in target_profile.cross_channel.can_be_triggered_by:
        raise PermissionError(f"#{channel_name} doesn't accept triggers from this channel")
```

### Response Handling

**Two modes:**

1. **Fire-and-forget** (default): Trigger agent, don't wait for response
   ```python
   trigger_agent("admin", "Approve deployment")
   # Returns immediately
   ```

2. **Wait for response**: Trigger and get agent's response back
   ```python
   response = trigger_agent("admin", "Approve deployment", return_response=True)
   # Waits up to timeout, returns admin agent's response
   ```

## Implementation Plan

### Phase 1: Basic MCP Server (1-2 days)

- [ ] Create discord-bridge-mcp Python package
- [ ] Implement `send_to_channel()` tool
- [ ] Implement `trigger_agent()` tool (fire-and-forget mode)
- [ ] Add permission validation
- [ ] Test basic cross-channel messaging

### Phase 2: Safety & Observability (1 day)

- [ ] Add circular trigger detection
- [ ] Implement max depth limiting
- [ ] Add trigger timeout handling
- [ ] Implement user transparency (show trigger source)
- [ ] Add telemetry for cross-channel events

### Phase 3: Advanced Features (1-2 days)

- [ ] Implement wait-for-response mode
- [ ] Add shared state tools (`get/set_channel_state`)
- [ ] Create workflow orchestration helpers
- [ ] Add trigger queue management
- [ ] Implement user permission whitelists

### Phase 4: Documentation & Examples (0.5 day)

- [ ] Document configuration schema
- [ ] Create example workflows
- [ ] Add troubleshooting guide
- [ ] Update channel profile examples

## Technical Architecture Changes

### Minimal Changes Required

✅ **No core architecture changes needed** - the event-driven system already supports this:

1. **EventBus**: Already handles routing events to channels
2. **SessionOrchestrator**: Already manages per-channel engines
3. **Channel Profiles**: Just add `cross_channel` config section

### New Components

```
discord-agent/
├── mcp-servers/
│   └── discord-bridge/
│       ├── server.py          # MCP server implementation
│       ├── permissions.py     # Permission validation
│       ├── state.py           # Shared state management
│       └── pyproject.toml     # Package config
├── core/
│   └── cross_channel.py       # Cross-channel utilities (optional)
```

### Event Flow

```
#engineering: User message "Deploy v1.2.3"
  ↓
DiscordGateway: Creates TextInputEvent
  ↓
EventBus: Routes to SessionOrchestrator
  ↓
SessionOrchestrator: Invokes engineering agent
  ↓
Engineering Agent: Calls trigger_agent("admin", ...)
  ↓
Discord Bridge MCP: Validates permissions
  ↓
Discord Bridge MCP: Publishes TextInputEvent with admin channel_id
  ↓
EventBus: Routes to SessionOrchestrator
  ↓
SessionOrchestrator: Invokes admin agent
  ↓
Admin Agent: Processes and responds
  ↓
DiscordGateway: Sends response to #admin
```

## Alternatives Considered

### Option A: Multiple Separate Bots

**Would require:**
- HTTP API or message queue for inter-bot communication
- Distributed state management (Redis, PostgreSQL)
- Service discovery mechanism
- Network failure handling
- Distributed tracing

**Verdict:** Much more complex, no benefits for this use case.

### Option C: Discord Webhooks

**Agents post to webhooks in other channels.**

**Downsides:**
- No agent processing in target channel (just passive message)
- Can't get agent's response
- Limited compared to full agent triggering

## Security Considerations

1. **Permission boundaries**: Strict whitelisting of allowed triggers
2. **User authentication**: Validate trigger requests came from authorized users
3. **Audit logging**: Log all cross-channel triggers for review
4. **Rate limiting**: Prevent trigger spam/abuse
5. **Content validation**: Sanitize prompts before passing to target agent
6. **Isolation**: Target agent shouldn't have access to source channel's secrets

## Future Enhancements

- **Workflow DAGs**: Define multi-step workflows across channels
- **Conditional routing**: Route based on agent response content
- **Parallel triggers**: Trigger multiple agents simultaneously
- **Approval workflows**: Built-in approve/reject patterns
- **Event subscriptions**: Agents subscribe to events in other channels
- **Cross-channel context**: Share conversation context between agents

## Conclusion

**Cross-channel communication is not only possible but RECOMMENDED** in the single-bot architecture.

The event-driven design makes this feature:
- ✅ Simple to implement (MCP server + minimal config)
- ✅ Reliable (no network layer)
- ✅ Observable (single process, unified logging)
- ✅ Flexible (supports complex workflows)

This capability makes Option B **superior** to multiple separate bots for any use case involving agent coordination.

---

**Status**: Design complete, ready for implementation
**Priority**: Medium (nice-to-have for advanced workflows)
**Effort**: ~3-4 days for full implementation
