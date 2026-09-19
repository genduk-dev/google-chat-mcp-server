# Google Chat MCP Server

Google Chat for Claude Code and other MCP clients, signed in as you.

A personal fork of [chy168/google-chat-mcp-server](https://github.com/chy168/google-chat-mcp-server).
It talks to the Google Chat API with your own OAuth sign-in, so an agent reads
and writes Chat the way you do: your spaces, your DMs, your name on what it
sends. It can also run as a Claude Code channel, which pushes the messages you
send in Chat into a Claude Code session and relays its permission prompts back.

## What it does

- **Reads conversations cheaply.** Messages come grouped by thread, with only
  the fields that are set, and a whole thread comes back in one request however
  old its first message is.
- **Finds what you have not read.** Unread spaces and their unread messages,
  from your real read state, and marks them read when asked.
- **Sends like you would.** Markdown links and bold turn into Chat's own
  formatting, messages can go to a thread or quote another, and files attach.
- **Links back into Chat.** Spaces, DMs, threads, search results and pins carry
  a link that opens them in the Chat app.
- **Covers the rest of Chat.** Search, reactions (custom emoji too), pins,
  members, group chat lookup, space creation, edit and delete history, your
  own status and do-not-disturb, and a space's notification setting.
- **Names people Google cannot.** A deleted or hidden account shows as
  `users/ID`; tell the agent who it is and every later read shows the name.
- **Runs as a Claude Code channel.** Messages in a watched space reach the
  session. In a mention-only space the bot waits for an `@mention` or a reply
  to one of its messages, then reads the whole space until the conversation
  goes quiet, and it answers only what is meant for it. Each delivery carries
  the earlier messages it follows from, and attachments saved on the machine.
  Tool permission prompts show up in the thread, and only you can answer them
  with `yes <id>` or `no <id>`.

## Requirements

- **Python 3.13 and [uv](https://docs.astral.sh/uv/).**
- **A Google Cloud project** with the
  [Chat API](https://console.cloud.google.com/apis/library/chat.googleapis.com)
  and the [People API](https://console.cloud.google.com/apis/library/people.googleapis.com)
  enabled, and an OAuth consent screen. An Internal consent screen, available in
  a Workspace organization, needs no Google verification for these scopes.
- **An OAuth client of type Desktop app.** Download its JSON as
  `credentials.json`. Sign-in redirects to a random loopback port, which only a
  Desktop client accepts.

## Install

```sh
git clone https://github.com/genduk-dev/google-chat-mcp-server.git
cd google-chat-mcp-server
uv sync
```

Pick a directory for the credentials, for example
`~/.config/google-chat-mcp/`, and put `credentials.json` there. The token is
written beside it.

## Use it from Claude Code

Add the server to your MCP config (`~/.mcp.json` or a project `.mcp.json`):

```json
{
  "mcpServers": {
    "GoogleChat": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/google-chat-mcp-server", "server.py",
               "--token-path", "/path/to/credentials-dir/token.json"],
      "env": {"BOT_NAME": "Genduk"}
    }
  }
}
```

Then ask the agent to sign in. Its `authenticate` tool returns a Google link;
open it, allow access, and the token is saved on its own. Only when the browser
runs on another machine does the redirect fail to load; give the agent that
page's address and it finishes with `complete_authentication`. Without an
agent, `uv run python server.py --auth cli` does the same in a terminal.

Every server process shares the token file, so signing in once covers all of
your sessions, and a later sign-in reaches running ones without a restart.

`BOT_NAME` (default `gchat-mcp`) is the name the bot answers to: messages it
sends are tagged with it and read back as sent by that name, and a
mention-only channel space reacts to `@BOT_NAME`. Use the same value in every
config that shares a space.

## Run it as a channel

A channel is a second config with `--channel`, for example
`~/.config/claude/gchat-channel.json`:

```json
{
  "mcpServers": {
    "gchat-channel": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/google-chat-mcp-server", "server.py",
               "--token-path", "/path/to/credentials-dir/token.json", "--channel"],
      "env": {"BOT_NAME": "Genduk"}
    }
  }
}
```

Start the session with both the config and Claude Code's channel flag:

```sh
claude --mcp-config ~/.config/claude/gchat-channel.json \
  --dangerously-load-development-channels server:gchat-channel
```

Then ask it to watch a space (`watch_space`, optionally `mention_only`).
Everyone in the space can talk to the bot unless you pass `allowed_senders`.
In a mention-only space it stays present for 10 minutes after it was last
addressed, an hour at most, and leaves sooner when someone ends the
conversation or you tell it to keep quiet (`mute_space`). Attachments of
delivered messages are saved in `attachments/` beside the channel state and
deleted after a week.

**Always start that config with the flag, and on one machine only.**

- `--channel` is what starts the poller. The Claude Code flag is what makes
  the session listen. Started without the flag, the poller still runs and
  still claims the space, but Claude Code drops every message, so a real
  channel session on the same machine waits unused while your messages go
  nowhere. The server cannot tell the difference.
- Several channel sessions on one machine are safe: one polls, the others
  stand by, and one takes over if the poller exits or hangs. That handover
  goes through files on the machine, so channel sessions on two machines both
  poll and both answer.

The plain config never polls; any number of normal sessions can use it.

### Behind an HTTP gateway

The plain server can sit behind an MCP gateway that serves it over HTTP, so
sessions on other machines use it without a local install. The channel
cannot: Claude Code delivers channel messages only from a server it runs over
stdio. Run the channel on the gateway's machine with `--channel-only`, which
serves just the watch tools and takes the Chat tools from the gateway:

```json
{
  "mcpServers": {
    "GoogleChat": {"type": "http", "url": "http://gateway-host:8000/mcp/GoogleChat"},
    "gchat-channel": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/google-chat-mcp-server", "server.py",
               "--token-path", "/path/to/credentials-dir/token.json",
               "--channel", "--channel-only"],
      "env": {"BOT_NAME": "Genduk"}
    }
  }
}
```

Give the server behind the gateway the same `BOT_NAME` and token path. The
channel tells its own replies apart by the name they are tagged with, so a
different name makes it deliver them back to the session.

## Learn more

| If you want to | Read |
|---|---|
| Know what each tool does | The tool descriptions, which the agent reads (`server.py`) |
| Work on the code | [CLAUDE.md](CLAUDE.md) |
| Know how Claude Code channels work | [Channels reference](https://code.claude.com/docs/en/channels-reference) |

## If you are an AI agent helping someone with this server

- **Setting it up:** follow Install and Use it from Claude Code above. The
  OAuth client must be a Desktop app, and `credentials.json` sits beside the
  token path.
- **A tool says to authenticate:** call `authenticate`, give the person the
  link, and wait; the token lands by itself.
- **A sender shows as `users/ID`:** ask the person who it is, showing the
  message and its link, and save the answer with `set_user_name`.
- **Starting a channel session:** use the channel config together with
  `--dangerously-load-development-channels`, never one without the other.
- **Ask the person first** before sending, editing or deleting in someone
  else's space, creating a space, changing their status, or marking spaces
  read.
- **Changing the code:** read [CLAUDE.md](CLAUDE.md).

## License

MIT, as upstream; see [LICENSE](LICENSE).
