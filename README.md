# Discord Agent - Event-Driven CLI Agent Framework

**Event-Driven | CLI-Agnostic | Voice-Optimized | Production-Ready**

A single-application Discord bot that exposes Codex CLI or Claude Code CLI as Discord agents with text and voice interaction capabilities.

## Features

✅ **Switchable Engines**: Toggle between Codex CLI and Claude Code CLI via configuration
✅ **Event-Driven Architecture**: Perfect decoupling via async event bus
✅ **Superior Voice Quality**: Kokoro TTS (primary) with Piper TTS fallback
✅ **Multi-Channel Agents**: Different Discord channels map to different agent configurations
✅ **Session Persistence**: Conversations persist across bot restarts with transcript replay
✅ **Attachment Support**: Send images, PDFs, and files for agent analysis
✅ **Mobile Access**: Full functionality from Discord mobile app (iPhone, Android)
✅ **Self-Hosted**: Runs on always-on PC, no cloud dependencies
✅ **Telemetry**: Structured logging with task timing and error tracking

## Architecture

```
discord_agent.py (Single Application)
  ├─ EventBus (async queue for all events)
  ├─ DiscordGateway (Discord → events)
  ├─ SessionOrchestrator (event routing + lifecycle)
  ├─ AgentEngines (Codex/Claude adapters)
  ├─ VoiceIO (Kokoro/Piper/Whisper providers)
  └─ StateStore (SQLite persistence + transcripts)
```

**Key Principle**: Everything flows through events. Transport, orchestration, execution, and persistence are fully decoupled.

## Installation

### Prerequisites

- Linux/WSL2
- Python 3.9+
- Codex CLI (`codex` command) and/or Claude Code CLI (`claude` command)
- FFmpeg (for audio processing)
- espeak-ng (for TTS)

### Quick Start

```bash
# Clone repository
git clone https://github.com/yourusername/discord-agent.git
cd discord-agent

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Install system dependencies
sudo apt-get install espeak-ng ffmpeg

# Copy example configs
cp config.example.yaml config.yaml
cp .env.example .env
cp channels/office.example.yaml channels/office.yaml
cp channels/code.example.yaml channels/code.yaml

# Edit configuration files
# - Add Discord bot token to .env
# - Add your Discord user ID to config.yaml
# - Configure agent homes in channel YAML files

# Run
python3 discord_agent.py config.yaml
```

## Configuration

### Bot Configuration (`config.yaml`)

```yaml
discord:
  token: ${DISCORD_BOT_TOKEN}
  command_prefix: "!"

profiles_dir: ./channels
state_dir: ./state

voice:
  stt_enabled: true
  tts_enabled: true
  whisper:
    model: base
    device: cpu
  kokoro:
    model: kokoro-v0_19
    device: cpu
  piper:
    model_path: null

session:
  max_age_hours: 24

logging:
  level: INFO
  structured: true
```

### Channel Profiles (`channels/*.yaml`)

Each channel profile maps Discord channels to agent configurations:

```yaml
# channels/office.yaml
channel:
  names: ["office", "work"]
  description: "Microsoft 365 specialist"

engine:
  type: codex
  config_home: ~/agents/office/.codex

voice:
  enabled: true
  tts_provider: kokoro
  tts_voice: af_sky

session:
  persist: true
  transcript_store: true
```

## Usage

### Text Interaction

1. Open Discord and navigate to configured channel (e.g., #office)
2. Type your message: "Check my calendar for tomorrow"
3. Bot responds with agent output
4. Continue conversation - context is maintained

### Attachment Support

1. Upload image, PDF, or file to Discord channel with your message
2. Bot downloads attachment to local storage
3. File path is included in prompt context for agent to read
4. Agent can analyze images, PDFs, spreadsheets, or any file type
5. **Storage**: Files saved to `artifacts/discord/<channel>/<message>/` (max 10MB per file)

### Voice Interaction

1. Join a Discord voice channel
2. Bot auto-joins (if `auto_join_voice: true`)
3. Speak your request
4. Bot transcribes, processes, and responds with natural voice
5. Continue conversation naturally

### Commands

```
!join               - Join your voice channel
!leave              - Leave voice channel
!reset              - Reset agent session (clear history)
```

## Project Structure

```
discord-agent/
├── core/                    # Core infrastructure
│   ├── events.py           # Event types (Pydantic)
│   ├── bus.py              # Event bus
│   ├── profiles.py         # Channel profile loader
│   └── telemetry.py        # Observability
├── engines/                 # CLI adapters
│   ├── protocol.py         # AgentEngine protocol
│   ├── codex_engine.py     # Codex MCP adapter
│   └── claude_engine.py    # Claude Code adapter
├── orchestrator/            # Session management
│   └── session_orchestrator.py  # Event routing + lifecycle
├── transport/               # Communication layer
│   └── discord_gateway.py  # Discord integration
├── voice/                   # Audio processing
│   ├── providers.py        # Provider registry
│   ├── kokoro_provider.py  # Kokoro TTS
│   ├── piper_provider.py   # Piper TTS (fallback)
│   └── whisper_provider.py # Whisper STT
├── state/                   # Persistence
│   ├── transcript.py       # Event transcripts
│   └── persistence.py      # Session storage
├── channels/                # Channel profiles
│   ├── office.example.yaml
│   └── code.example.yaml
├── config.example.yaml      # Bot configuration template
├── requirements.txt         # Python dependencies
└── discord_agent.py         # Main entry point
```

## Engine Switching

To switch from Codex to Claude Code:

```yaml
# channels/office.yaml
engine:
  type: claude-code  # Changed from: codex
  config_home: ~/agents/office/.claude
```

Restart bot. That's it!

## Voice Quality

- **Primary**: Kokoro TTS (82M parameters, natural voice)
- **Fallback**: Piper TTS (fast, reliable)
- **STT**: Whisper (base model)

Automatic failover ensures voice always works.

## Development Status

### ✅ Completed

**Phase 1-3: Core Infrastructure**
- Event system (events, bus)
- Channel profiles loader
- Telemetry system
- AgentEngine protocol
- Codex engine adapter (JSON-RPC over stdio)
- Claude Code engine adapter (streaming JSON)
- Voice providers (Kokoro, Piper, Whisper)
- Persistence layer (transcripts, sessions)

**Phase 4-6: Integration**
- Session orchestrator (event routing + lifecycle)
- Discord gateway (text + voice integration)
- Main application wiring
- Configuration system
- Graceful shutdown handling

**Verified & Fixed**
- All Codex review issues resolved
- Python syntax validated
- Ready for testing

### 🚧 Next Steps

- End-to-end testing with live Discord bot
- Voice channel integration refinement
- Rich Discord embeds for tool calls
- Performance optimization
- Deployment guide

## Contributing

1. Fork the repository
2. Create a feature branch
3. Implement and test
4. Submit pull request

## License

MIT License

## Acknowledgments

Built on:
- [Codex CLI](https://github.com/openai/codex) - AI-powered development assistant
- [Claude Code](https://github.com/anthropics/claude-code) - Claude CLI tool
- [Kokoro TTS](https://huggingface.co/hexgrad/Kokoro-82M) - High-quality open-source TTS
- [discord.py](https://github.com/Rapptz/discord.py) - Discord API wrapper
- Model Context Protocol (MCP) - Standardized AI tool integration

## Support

For issues, questions, or contributions:
- GitHub Issues: [yourusername/discord-agent](https://github.com/yourusername/discord-agent/issues)
- Documentation: [docs/](docs/)

---

**One Application. One Purpose. Maximum Elegance.**