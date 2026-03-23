# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Personal fork of `chy168/google-chat-mcp-server`. Exposes Google Chat as MCP tools via OAuth2, allowing LLM clients to read, send, edit, delete, and react to messages.

## Commands

- **Run MCP server**: `uv run server.py`
- **Auth (CLI)**: `uv run python server.py --auth cli`
- **Auth (web)**: `uv run python server.py --auth web --port 8000`
- **Debug**: `fastmcp dev server.py --with-editable .`
- **Docker build**: `docker build -t google-chat-mcp-server:latest .`

No test suite exists yet (tests/ directory is empty).

## Architecture

Two runtime modes sharing a common Google Chat client:

1. **MCP Server** (`server.py`) — FastMCP instance registering tools: `get_chat_spaces`, `get_space_messages`, `send_space_message`, `delete_space_message`, `get_message`, `update_message`, `create_reaction`, `list_reactions`, `send_message_with_attachment`

2. **Auth Server** (`server_auth.py`) — FastAPI OAuth2 web flow; also `auth_cli.py` for headless CLI auth

**Core module**: `google_chat.py` — all Google Chat API + People API logic, credential management, member name prefetching, message filtering.

## Key Details

- Python 3.13, managed with `uv` and `hatchling` build backend
- MCP framework: `fastmcp`
- Auth credentials stored in `credentials.json` (GCP OAuth client secrets, not committed) and `token.json` (runtime, path configurable via `--token-path`)
- `APP_MESSAGE_PREFIX` env var controls the prefix for `clientAssignedMessageId` (default: `client-genduk-`), used to tag and identify app-sent messages
- `--raw-messages` flag disables field filtering (returns full API responses); filtered by default to save tokens
