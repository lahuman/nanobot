# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Architecture Overview

nanobot is an ultra-lightweight AI agent framework built around a small, readable core agent loop. The architecture centralizes on a simple principle: messages flow in from various chat platforms, the LLM decides when tools are needed, and memory/context are pulled in as needed without heavy orchestration.

### Core Components

**Agent Loop** (`nanobot/agent/`)
- `loop.py`: Main agent execution flow - handles tool execution, LLM interactions, and turn management
- `runner.py`: Executes tool-using agents with proper error handling and recovery
- `memory.py`: Two-stage memory system - Consolidator (short-term compression) and Dream (long-term reflection)
- `subagent.py`: Support for spawning and managing sub-agents
- `tools/`: Tool registry and built-in tools (bash, web search, file operations, etc.)

**Chat Channels** (`nanobot/channels/`)
- 20+ platform integrations: Telegram, Discord, WeChat, Slack, Email, Matrix, WhatsApp, Feishu, etc.
- Each channel inherits from `base.py` and implements platform-specific authentication, message handling, and media support
- `manager.py`: Coordinates multi-channel operation with shared sessions

**Configuration & Session Management**
- `config/`: Multi-layer config loading with environment variable resolution and validation
- `session/`: Conversation history persistence with automatic compaction and unified cross-channel sessions

**LLM Providers** (`nanobot/providers/`)
- Provider registry for multiple LLM backends (OpenAI, Anthropic, Azure, GitHub Copilot, etc.)
- Unified interface for tool calling, streaming, and provider-specific optimizations (prompt caching, reasoning)

**Memory System**
- Three-layer architecture:
  - Session messages: live conversation state
  - `memory/history.jsonl`: compressed archive of past turns
  - `SOUL.md`, `USER.md`, `memory/MEMORY.md`: durable knowledge files maintained by the Dream process
- Auto-compaction on context window pressure
- Git-based versioning for memory files

**Skills System** (`nanobot/skills/`)
- Extensible skill directory with markdown templates
- Skills are Jinja2 templates that get rendered into system prompts
- Supports dynamic skill discovery and runtime loading

## Common Development Commands

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Run specific test file
pytest tests/test_file.py

# Lint code
ruff check nanobot/

# Format code
ruff format nanobot/

# Interactive agent session
nanobot agent

# Setup/configuration wizard
nanobot onboard

# WebUI gateway (for web interface)
nanobot gateway
```

## Testing Approach

- pytest with `asyncio_mode = "auto"` for async test support
- Tests located in `tests/` directory with `test_*.py` naming
- Focus on integration tests for channels and agent behavior
- Mock external services (LLM APIs, chat platforms) in unit tests

## Code Style Guidelines

- **Line length**: 100 characters (enforced by ruff)
- **Python version**: 3.11+ with modern typing (`from __future__ import annotations`)
- **Async**: All I/O operations use async/await, no synchronous blocking in main loops
- **Imports**: ruff handles import sorting and formatting (rules: E, F, I, N, W, ignoring E501)
- **Design principles**:
  - Optimize for the next reader, not cleverness
  - Prefer smallest change that solves the real problem
  - Keep boundaries clean, avoid unnecessary abstractions
  - Do not create complexity to hide complexity
  - One clear way to do things, not multiple clever ways

## Branching Strategy

Two-branch model to balance stability and exploration:

- **`main`**: Stable releases - production-ready, bug fixes and minor improvements only
- **`nightly`**: Experimental features - new functionality, refactoring, breaking changes

**Targeting rules**:
- New features → `nightly`
- Bug fixes with no behavior changes → `main`
- Documentation → `main`
- Refactoring → `nightly`
- Unsure → `nightly` (easier to cherry-pick stable features to main than undo risky changes)

Stable features are cherry-picked from `nightly` to `main` weekly, never full branch merges.

## Key Design Patterns

**Agent Execution Flow**
1. Messages arrive via channel → normalized to common format
2. Agent loop processes message with tools/memory as context
3. LLM decides when to use tools (structured tool calls)
4. Tool results appended to conversation
5. Final response sent back through channel

**Tool System**
- Tools registered via decorators with type hints → automatic schema generation
- Tools must be deterministic and side-effect-aware (visible to agent)
- Tool results cached and truncated to prevent context overflow
- Workspace restrictions enforce security boundaries

**Memory Strategy**
- Short-term: full conversation in session (up to context limit)
- Medium-term: Consolidator compresses old messages to `history.jsonl`
- Long-term: Dream process synthesizes history into durable `.md` files
- All memory files are append-only or versioned for safety

**Error Handling**
- Failures at tool layer caught and returned as tool error messages (not exceptions)
- Provider errors trigger retry logic with exponential backoff
- Session corruption auto-recovery via atomic writes with temp files
- Graceful degradation - missing config values use sensible defaults

## Configuration Structure

Config file: `~/.nanobot/config.json` (overridden via `--config` or env vars)

Key sections:
- `providers`: API keys and endpoints for LLM providers
- `agents.defaults`: Model selection, iteration limits, context windows
- `channels`: Platform-specific settings (Telegram bot tokens, etc.)
- `tools`: Workspace restrictions, MCP servers, disabled skills

Config uses Pydantic for validation with environment variable substitution.

## WebUI Development

WebUI requires source checkout (not included in PyPI package):

```bash
# Enable WebSocket channel in config
{ "channels": { "websocket": { "enabled": true } } }

# Start gateway
nanobot gateway

# In separate terminal, start dev server
cd webui && bun install && bun run dev
```

WebUI codebase in `webui/` uses TypeScript/React and connects via WebSocket channel.

## MCP Integration

MCP (Model Context Protocol) servers integrate as tools:
- Configure in `tools.mcp_servers` section of config
- Both stdio and SSE transports supported
- Each server spawns as subprocess with stdio or connects to SSE endpoint
- Tool schemas automatically discovered and registered

## Common Gotchas

- **File paths**: Always use `Path.expanduser().resolve()` for user-provided paths
- **Async context**: Use `MessageBus` for cross-component communication, raw async for internal
- **Tool results**: Truncate aggressively - LLM context is precious, send only what's needed
- **Windows**: UTF-8 encoding issues common, reconfigure sys.stdout/stderr early
- **Sessions**: Keys must be globally unique for isolation, use channel-specific prefixes
- **Memory**: Don't rely on session history persisting indefinitely, always check `history.jsonl`
