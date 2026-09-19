# google-chat-mcp-server

An MCP server that gives agents Google Chat through the user's own OAuth
sign-in, and optionally runs as a Claude Code channel. A personal fork of
`chy168/google-chat-mcp-server`; `upstream` is fetch-only.

## The map

- `server.py`: every MCP tool and its docstring, the CLI flags, and
  `run_channel`, which adds the channel's tools and its stdio relay.
- `google_chat.py`: everything that talks to Google. Credentials, the Chat and
  People API calls, retries, and the compact output the tools return.
- `channel.py`: the channel. Watched spaces, the poller and its lease, the
  cursors, presence in mention-only spaces, batching, the context and
  attachments a delivery carries, edits, and the permission relay.
- `gate.py`: the Jev classifier gate that replaces mentions and presence with
  `CHANNEL_GATE=jev`. It knows nothing about Google Chat, so its rules can run
  behind another chat network; `channel.py` maps messages to it and acts on
  its decision.
- `server_auth.py`, `auth_cli.py`: sign-in outside an agent, from upstream.
- `tests/`: unit tests, no network.

## Rules that are not visible in the code

### Several server processes share one set of files

Every Claude Code session runs its own server process, and they all use the
files beside the token: `token.json`, the channel's state, cursors, lease and
pending permission prompts, and `user_names.json`. Write them through
`write_private` (a per-process temp file, 0600, atomic rename) and reread a
file when it changes on disk. Never treat an in-memory copy as the truth: a
session started before a re-login once wrote its old token back an hour later
and dropped the new scopes.

### The channel cannot tell whether it is a channel

`--channel` starts the poller. Claude Code's
`--dangerously-load-development-channels` is what makes a session listen, and
its `initialize` is the same with or without it, so the server cannot detect a
session that will drop its notifications. The lease is local files, so two
machines both poll. Both are documented for the user in the README; do not
build detection on a signal that does not exist.

The poller blocks the event loop, like every tool call. Anything that can
block for long (retries, big listings) has to stay under the lease's 60
seconds, or a standby takes over.

### A write that failed with a 5xx may have happened

Retry a 429 for any method: Google did not process it. Retry a 5xx only for a
read. In a burst test two of five writes that returned 503 had been posted.

### Tool docstrings are the agent's interface

The agent sees nothing but the docstrings and the output. A change to what a
tool returns or accepts changes its docstring in the same commit. Output is
compact JSON through `_json`, and optional fields appear only when set. This is
a private tool, so breaking changes are fine; say so in the commit.

### Text from Chat or from Claude Code is untrusted

It reaches Chat inside code (backticks or a fence), where mention and link
markup stays inert. Only senders on a space's allowlist reach the session, and
by default that is everyone in the space. Only the operator, the signed-in
user, answers a permission prompt.

### Presence lives in the poller's memory

Which spaces the bot is present in, what the session has seen, and the queued
batch are in memory, so a restart or a takeover starts idle. So are the gate's
pending decision and, per conversation, how far the session has read, which
is why a delivery to a fresh session fetches the last day from Chat instead
of trusting the buffer. What a tool
changes (leave, mute) goes through the state file instead, because the tool
may run in a session that is not polling.

## Commands

```sh
uv sync
timeout 120 uv run python -m unittest discover -s tests -t .
uv run server.py --token-path PATH            # the plain server
uv run server.py --token-path PATH --channel  # the channel
```

## Testing against the live API

Unit tests mock Google. A change to an API call is not done until it has run
against the real API through the MCP server, over stdio.

- **The channel:** read its stdout raw. The MCP client SDK drops
  `notifications/claude/channel`, so a test through it never sees a delivery.
- **Keep the live channel out of it:** use a temporary `--channel-state-path`
  and another `BOT_NAME` (for example `gendukt`), so the user's running
  channel session neither reacts nor loses its lease.
- **Leave nothing behind:** delete test messages and threads. Record status,
  notification or read state before changing it, and put back exactly what was
  there.
- **Ask first** before anything another person would see: a message in a
  shared space, a created space, an invitation.
