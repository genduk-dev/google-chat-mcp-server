# Google Chat MCP Server (Personal Fork)

> **Fork of [chy168/google-chat-mcp-server](https://github.com/chy168/google-chat-mcp-server)** with additional features for richer Google Chat interaction via MCP.

A Python MCP server that exposes Google Chat as tools for LLM clients. Read, send, edit, delete, and react to messages — all through the Model Context Protocol.

## Fork Additions

- **Full message CRUD** — send, get, update, and delete messages
- **Thread replies** — reply via `thread_key` (bot-initiated) or `thread_name` (existing thread)
- **Emoji reactions** — create and list reactions on messages
- **File link attachments** — send messages with clickable file links
- **Smart name resolution** — bulk People API prefetch for display names
- **App message tagging** — `clientAssignedMessageId` prefix to identify app-sent messages (configurable via `APP_MESSAGE_PREFIX` env var)
- **Token-saving mode** — filtered message output by default; use `--raw-messages` for full API response
- **CLI auth** — headless OAuth flow for remote/SSH environments

## Requirements

- Python 3.13
- [uv](https://docs.astral.sh/uv/) package manager
- Google Cloud project with these APIs enabled:
  - [Google Chat API](https://console.cloud.google.com/apis/library/chat.googleapis.com)
  - [People API](https://console.cloud.google.com/apis/library/people.googleapis.com)
- OAuth2 client credentials (`credentials.json`) from [Google Cloud Console](https://console.cloud.google.com/auth/clients)

## Setup

### 1. Clone and install

```bash
git clone https://github.com/genduk-dev/google-chat-mcp-server.git
cd google-chat-mcp-server
uv sync
```

### 2. Create OAuth credentials

1. Go to [Google Cloud Console > Auth Platform > Clients](https://console.cloud.google.com/auth/clients)
2. Create a **Web application** client ([reference](https://developers.google.com/identity/protocols/oauth2/?hl=en))
3. Add authorized JavaScript origin: `http://localhost:8000`
4. Add authorized redirect URI: `http://localhost:8000/auth/callback`
5. Download the client secrets JSON and save as `credentials.json` in the project root

### 3. Authenticate

**CLI mode** (recommended for headless/remote environments):
```bash
uv run python server.py --auth cli
```
Follow the prompts — open the URL in any browser, complete authorization, paste the redirect URL back.

**Web mode** (local browser available):
```bash
uv run python server.py --auth web --port 8000
```
Open `http://localhost:8000/auth` and complete the Google login flow.

Both modes save the token to `token.json` (configurable via `--token-path`).

## MCP Client Configuration

```json
{
  "mcpServers": {
    "google_chat": {
      "command": "uv",
      "args": [
        "--directory", "/path/to/google-chat-mcp-server",
        "run", "server.py",
        "--token-path", "/path/to/google-chat-mcp-server/token.json"
      ]
    }
  }
}
```

## Tools

| Tool | Description |
|------|-------------|
| `get_chat_spaces()` | List all accessible Google Chat spaces |
| `get_space_messages(space_name, start_date, end_date?)` | List messages with date filtering (YYYY-MM-DD) |
| `get_message(message_name)` | Fetch a single message by resource name |
| `send_space_message(space_name, text, thread_key?, thread_name?)` | Send a message, optionally in a thread |
| `update_message(message_name, text)` | Edit a message's text |
| `delete_space_message(message_name)` | Delete a message |
| `create_reaction(message_name, emoji_unicode)` | Add an emoji reaction |
| `list_reactions(message_name)` | List all reactions on a message |
| `send_message_with_attachment(space_name, text, file_url, ...)` | Send a message with a file link |

Resource name formats:
- Space: `spaces/SPACE_ID`
- Message: `spaces/SPACE_ID/messages/MESSAGE_ID`
- Thread: `spaces/SPACE_ID/threads/THREAD_ID`

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `APP_MESSAGE_PREFIX` | `client-genduk-` | Prefix for `clientAssignedMessageId` to identify app-sent messages |

## Docker

### Build image
```bash
docker build -t google-chat-mcp-server:latest .
```

### Run MCP server
```bash
docker run -it --rm \
  -v /path/to/project:/data \
  google-chat-mcp-server:latest \
  --token-path=/data/token.json
```

### Run auth server in container

> **Note:** `credentials.json` must be accessible inside the container at `/app/credentials.json`.

```bash
# Web mode
docker run -it --rm \
  -p 8000:8000 \
  -v /path/to/project/credentials.json:/app/credentials.json:ro \
  -v /path/to/project:/data \
  google-chat-mcp-server:latest \
  --auth web --host 0.0.0.0 --port 8000 --token-path=/data/token.json

# CLI mode
docker run -it --rm \
  -v /path/to/project/credentials.json:/app/credentials.json:ro \
  -v /path/to/project:/data \
  google-chat-mcp-server:latest \
  --auth cli --token-path=/data/token.json
```

## Development

```bash
# Run MCP server directly
uv run server.py

# Debug with FastMCP inspector
fastmcp dev server.py --with-editable .

# Raw API output (no field filtering)
uv run server.py --raw-messages
```

## Architecture

```
server.py          — FastMCP entry point, registers all MCP tools
google_chat.py     — Google Chat API + People API client, credential management
server_auth.py     — FastAPI OAuth2 web auth server
auth_cli.py        — Headless CLI OAuth flow
```
