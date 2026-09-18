# server.py
import argparse
import json
from typing import List, Dict

from fastmcp import FastMCP
from google_chat import list_chat_spaces, DEFAULT_CALLBACK_URL, set_token_path, set_filter_messages
from server_auth import run_auth_server
from auth_cli import run_cli_auth

# Create an MCP server
mcp = FastMCP("Google Chat")

@mcp.tool()
async def get_spaces() -> List[Dict]:
    """List all Google Chat spaces the bot has access to.

    This tool requires OAuth authentication. On first run, it will open a browser window
    for you to log in with your Google account. Make sure you have credentials.json
    downloaded from Google Cloud Console in the current directory.
    """
    return await list_chat_spaces()

@mcp.tool()
async def get_messages(space_name: str,
                       start_date: str = None,
                       end_date: str = None,
                       thread_name: str = None,
                       limit: int = None) -> str:
    """List messages from a Google Chat space, by date, by thread, or the latest N.

    Give at least one of start_date, thread_name or limit; they combine.
    - start_date alone covers that whole day (UTC); with end_date, start_date 00:00:00Z
      to end_date 23:59:59Z. Dates are YYYY-MM-DD.
    - thread_name returns that whole thread, including a root posted months ago, in one
      request. Take it from a message's thread or a channel event's thread_name.
    - limit returns only the most recent N matching messages (1-1000).

    Returns one object, grouped by thread to save tokens:
        {"space": "spaces/S",
         "threads": [{"thread": "spaces/S/threads/T",
                      "messages": [{"id": "T.M", "sender": "...", "time": "...", "text": "..."}]}],
         "truncated": true}   # only when more than 1000 messages matched
    Threads are ordered by their first returned message, messages oldest first.
    A message's full name is "{space}/messages/{id}"; use it for get_message, reactions,
    quote replies and attachments. Pass a group's "thread" as send_message's thread_name
    to reply in it. Optional message fields appear only when set: sender_type (when not
    HUMAN), sent_by_app, root (the thread's first message), edited, quoted, attachment,
    reactions ({emoji or :custom_name:: count}).

    Args:
        space_name: The space to fetch messages from ('spaces/SPACE_ID')
        start_date: Optional start date in YYYY-MM-DD format
        end_date: Optional end date in YYYY-MM-DD format, only used with start_date
        thread_name: Optional thread of this space ('spaces/SPACE_ID/threads/THREAD_ID')
        limit: Optional number of most recent matching messages to return

    Raises:
        ValueError: If no filter is given, a date is malformed, dates are in the wrong
                    order, thread_name belongs to another space, or limit is out of range
    """
    from google_chat import list_space_messages
    from datetime import datetime, timezone

    if not (start_date or thread_name or limit):
        raise ValueError("Give at least one of start_date, thread_name or limit")

    start_datetime = end_datetime = None
    try:
        if start_date:
            # Parse start date and set to beginning of day (00:00:00Z)
            start_datetime = datetime.strptime(start_date, '%Y-%m-%d').replace(
                hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc
            )
        # Parse end date if provided and set to end of day (23:59:59Z)
        if start_date and end_date:
            end_datetime = datetime.strptime(end_date, '%Y-%m-%d').replace(
                hour=23, minute=59, second=59, microsecond=999999, tzinfo=timezone.utc
            )

            # Validate date range
            if start_datetime > end_datetime:
                raise ValueError("start_date must be before end_date")
    except ValueError as e:
        if "strptime" in str(e) or "does not match format" in str(e):
            raise ValueError("Dates must be in YYYY-MM-DD format (e.g., '2024-03-22')")
        raise e

    result = await list_space_messages(space_name, start_datetime, end_datetime, thread_name, limit)
    # FastMCP's own serialization escapes non-ASCII (emoji, CJK) as \uXXXX and pads
    # separators, which costs tokens on every message.
    return json.dumps(result, ensure_ascii=False, separators=(',', ':'))

@mcp.tool()
async def list_unread_spaces(days: int = 1) -> str:
    """Find the spaces, DMs and group chats with messages you have not read yet.

    Checks every space active in the last `days` days (1-90) against your read marker,
    so it answers "what haven't I read". Your own messages never count as unread.
    DMs and group chats are named after who wrote the unread messages. Some spaces
    have a read marker months old (read only via notifications), so counts are capped
    at "100+". Read one with get_unread_messages, then mark it with mark_space_read.

    Returns:
        {"since", "checked", "spaces": [{"space", "name", "type", "unread", "last_read", "latest"}]},
        most recently active first.
    """
    from google_chat import list_unread_spaces as _list
    return json.dumps(await _list(days), ensure_ascii=False, separators=(',', ':'))

@mcp.tool()
async def get_unread_messages(space_name: str, limit: int = 50) -> str:
    """Read the messages you have not read in one space: those created after your read marker.

    Returns the newest `limit` (1-1000) in get_messages' thread-grouped format, plus
    "last_read"; "more": true means older unread messages exist beyond the limit.
    Reading does not mark them read; call mark_space_read for that.

    Args:
        space_name: The space to read ('spaces/SPACE_ID')
        limit: How many of the newest unread messages to return
    """
    from google_chat import get_unread_messages as _get
    return json.dumps(await _get(space_name, limit), ensure_ascii=False, separators=(',', ':'))

@mcp.tool()
async def mark_space_read(space_name: str) -> Dict:
    """Mark everything in a space as read, as if you had opened it in Google Chat.

    This changes your real read state, so only do it when the user asked to, or after
    reading the unread messages on their behalf.

    Args:
        space_name: The space to mark read ('spaces/SPACE_ID')
    """
    from google_chat import mark_space_read as _mark
    return await _mark(space_name)

@mcp.tool()
async def search_messages(query: str,
                          space_name: str = None,
                          limit: int = 50,
                          page_token: str = None) -> Dict:
    """Full-text search for Google Chat messages by content.

    Searches every space you have access to by default, or a single space when
    space_name is given. Use this to find a message or thread when you know
    roughly what was said but not where or when — get_messages requires you to
    already know the space and date.

    Results come back one page at a time. If the page you get back is not enough,
    call again with page_token set to the nextPageToken you received — that
    continues where you left off instead of re-fetching what you already have.
    Reuse the same query and space_name when continuing; the token is only valid
    for that combination.

    query is passed through verbatim as the API's filter string. A literal `"`, or
    a standalone `OR` token, is rejected locally before any request is made.
    Lowercase `or` and "or" inside a word (e.g. "order") are fine. space_name is
    also rejected locally if it contains a literal `"`.

    Args:
        query: Text to search for
        space_name: Optional 'spaces/XXXX' to restrict the search to one space
        limit: Max messages in this page (default 50, capped at 1000)
        page_token: nextPageToken from a previous call, to fetch the next page

    Returns:
        {'messages': [...], 'nextPageToken': str or None} — nextPageToken is None
        when there are no further results

    Raises:
        Exception: If not authenticated, or if the search API is unavailable
    """
    from google_chat import search_space_messages
    return await search_space_messages(query, space_name, limit, page_token)

@mcp.tool()
async def get_members(space_name: str) -> List[Dict]:
    """List all members of a Google Chat space with their user IDs and display names.

    Use this to look up user IDs for mentioning people in messages.
    Each member includes a 'mention' field with the ready-to-use mention syntax.

    Args:
        space_name: The space to list members from (format: 'spaces/SPACE_ID')

    Returns:
        List of members with user_id, display_name, mention, type, and role
    """
    from google_chat import list_space_members
    return await list_space_members(space_name)

@mcp.tool()
async def send_message(space_name: str, text: str, thread_key: str = None, thread_name: str = None, quote_reply_message_name: str = None, file_paths: list = None, filenames: list = None) -> Dict:
    """Send a message to a Google Chat space, optionally with file attachments.

    Formatting (Google Chat renders these):
    - Links: [label](url) or <url|label> show as a hyperlinked label. A bare URL shows
      in full, and a bare Google Drive URL also gets a large preview card; use it when
      the file itself should stand out. Inline file chips cannot be created via the API.
    - *bold* or **bold**, _italic_, ~strike~ or ~~strike~~, `code`, ```code block```.
      Markdown inside code is sent as is. Headings and tables are not supported.
    - Mentions: <users/USER_ID> (see get_members), or <users/all> for everyone.

    Args:
        space_name: The space to send to (format: 'spaces/SPACE_ID')
        text: The message text to send (supports <users/USER_ID> mentions)
        thread_key: Optional thread key for bot-initiated threads (creates new thread if not found)
        thread_name: Optional thread name to reply in an existing thread (format: 'spaces/SPACE_ID/threads/THREAD_ID')
        quote_reply_message_name: Optional message resource name to quote-reply to (format: 'spaces/SPACE_ID/messages/MESSAGE_ID')
        file_paths: Optional list of local file paths or HTTP(S) URLs to upload as attachments
        filenames: Optional list of display names for the attachments (matched by index to file_paths)

    Returns:
        The created message object with name, createTime, text, thread, and space
    """
    from google_chat import send_space_message as _send
    return await _send(space_name, text, thread_key, thread_name, quote_reply_message_name, file_paths, filenames)

@mcp.tool()
async def delete_message(message_name: str) -> Dict:
    """Delete a message from a Google Chat space.

    Only messages sent by the authenticated bot/user can be deleted.

    Args:
        message_name: The resource name of the message to delete
                     (format: 'spaces/SPACE_ID/messages/MESSAGE_ID')

    Returns:
        Confirmation of deletion
    """
    from google_chat import delete_space_message as _delete
    return await _delete(message_name)

@mcp.tool()
async def get_message(message_name: str) -> Dict:
    """Fetch a single message by its resource name.

    Args:
        message_name: The resource name of the message
                     (format: 'spaces/SPACE_ID/messages/MESSAGE_ID')

    Returns:
        The message object with name, sender, createTime, text, and thread
    """
    from google_chat import get_message as _get_message
    return await _get_message(message_name)

@mcp.tool()
async def update_message(message_name: str, text: str = None, file_paths: list = None, filenames: list = None, remove_quote_reply: bool = False) -> Dict:
    """Edit an existing message in a Google Chat space — update text, add/replace attachments, or both.

    Only messages sent by the authenticated user can be edited.

    Args:
        message_name: The resource name of the message to update
                     (format: 'spaces/SPACE_ID/messages/MESSAGE_ID')
        text: New text content for the message, formatted as for send_message.
              If not provided, text is not changed.
        file_paths: List of local file paths or HTTP(S) URLs to upload as attachments.
                   If provided, replaces any existing attachments. If not provided, attachments are not changed.
        filenames: List of display names for the attachments (matched by index to file_paths).
        remove_quote_reply: If True, removes the quoted message from this message.
                           Note: quote replies can only be removed, not added via edit.

    Returns:
        The updated message object with name, createTime, lastUpdateTime, text, and thread
    """
    from google_chat import update_message as _update_message
    return await _update_message(message_name, text, file_paths, filenames, remove_quote_reply)

@mcp.tool()
async def create_reaction(message_name: str, emoji_unicode: str) -> Dict:
    """Add an emoji reaction to a message in a Google Chat space.

    Args:
        message_name: The resource name of the message to react to
                     (format: 'spaces/SPACE_ID/messages/MESSAGE_ID')
        emoji_unicode: The Unicode emoji string to react with (e.g. '👍', '❤️', '😂')

    Returns:
        The created reaction object
    """
    from google_chat import create_reaction as _create_reaction
    return await _create_reaction(message_name, emoji_unicode)

@mcp.tool()
async def list_reactions(message_name: str) -> List[Dict]:
    """List all reactions on a message in a Google Chat space.

    Args:
        message_name: The resource name of the message
                     (format: 'spaces/SPACE_ID/messages/MESSAGE_ID')

    Returns:
        List of reaction objects, each containing emoji and user info
    """
    from google_chat import list_reactions as _list_reactions
    return await _list_reactions(message_name)

@mcp.tool()
async def find_direct_message(user_id: str) -> Dict:
    """Find an existing DM space with a specific user.

    Useful to check whether a DM already exists before creating a new one,
    avoiding duplicate DM spaces.

    Args:
        user_id: The user resource name to find a DM with (format: 'users/USER_ID')

    Returns:
        Space object if a DM exists, or empty dict if no DM found
    """
    from google_chat import find_direct_message as _find_direct_message
    return await _find_direct_message(user_id)

@mcp.tool()
async def find_group_chats(user_ids: List[str]) -> str:
    """Find the group chats (unnamed multi-person DMs) whose human members are exactly
    you plus the given users. Use it before messaging a set of people, to reuse their
    existing group chat instead of starting another.

    Args:
        user_ids: 1-49 other people, as 'users/USER_ID' or 'users/EMAIL'. Leave yourself out.

    Returns:
        [{"space", "name" (only when the chat was named), "last_active", "uri"}];
        empty when no such group chat exists
    """
    from google_chat import find_group_chats as _find
    return json.dumps(await _find(user_ids), ensure_ascii=False, separators=(',', ':'))

@mcp.tool()
async def get_space(space_name: str) -> Dict:
    """Details of one space: name, type, description and guidelines, history setting,
    member count, access, when it was last active, and who may post, reply, manage
    members or use @all (permissions: {setting: [roles allowed]}).

    Args:
        space_name: The space ('spaces/SPACE_ID')
    """
    from google_chat import get_space as _get_space
    return await _get_space(space_name)

@mcp.tool()
async def get_member(space_name: str, user: str) -> Dict:
    """Look up one person's membership in a space: whether they are in it, their role
    and when they joined. Cheaper than get_members for a single person.

    Args:
        space_name: The space ('spaces/SPACE_ID')
        user: 'users/USER_ID' or 'users/EMAIL'

    Returns:
        {"user_id", "display_name", "mention", "type", "role", "state", "joined"}, or
        {"user_id", "state": "NOT_A_MEMBER"} when they are not in the space
    """
    from google_chat import get_member as _get_member
    return await _get_member(space_name, user)

@mcp.tool()
async def list_pinned_messages(space_name: str) -> str:
    """List the pinned messages of a space, with their content.

    Returns:
        {"space", "pins": [message]}, each message in get_messages' format; a pin whose
        message you can no longer read is {"id", "unavailable": HTTP status}
    """
    from google_chat import list_pinned_messages as _list
    return json.dumps(await _list(space_name), ensure_ascii=False, separators=(',', ':'))

@mcp.tool()
async def pin_message(message_name: str) -> Dict:
    """Pin a message in its space, for everyone in the space.

    Args:
        message_name: 'spaces/SPACE_ID/messages/MESSAGE_ID'
    """
    from google_chat import pin_message as _pin
    return await _pin(message_name)

@mcp.tool()
async def unpin_message(message_name: str) -> Dict:
    """Unpin a message, for everyone in the space.

    Args:
        message_name: 'spaces/SPACE_ID/messages/MESSAGE_ID'
    """
    from google_chat import unpin_message as _unpin
    return await _unpin(message_name)

@mcp.tool()
async def list_space_events(space_name: str,
                            event_types: List[str] = None,
                            start_time: str = None,
                            end_time: str = None,
                            limit: int = 100) -> str:
    """What changed in a space, oldest first: messages edited or deleted, reactions added
    or removed, people joining or leaving, space settings changed. get_messages shows
    only the current state; use this to find what changed since a point in time.

    Events carry the resource as it is now, so an edit shows the current text and a
    deleted message shows only its id and deletion time. Google keeps 28 days of events.

    Args:
        space_name: The space ('spaces/SPACE_ID')
        event_types: Any of message.created, message.updated, message.deleted,
            reaction.created, reaction.deleted, membership.created, membership.updated,
            membership.deleted, space.updated. Default: message.updated, message.deleted,
            reaction.created, reaction.deleted.
        start_time: Exclusive start, 'YYYY-MM-DD' (00:00Z) or RFC 3339. Default: 28 days ago.
        end_time: Inclusive end, same format. Default: now.
        limit: Max events (1-1000)

    Returns:
        {"space", "events": [{"time", "type", ...}], "more"?}. Message events carry the
        message in get_messages' format (or "deleted" and "deletion"), reaction events
        "message", "user", "emoji", membership events the member. "more": true means
        later events were cut by limit; call again with start_time set to the last time.
    """
    from google_chat import list_space_events as _list
    return json.dumps(await _list(space_name, event_types, start_time, end_time, limit),
                      ensure_ascii=False, separators=(',', ':'))

@mcp.tool()
async def delete_reaction(reaction_name: str) -> Dict:
    """Remove a reaction from a Google Chat message.

    Use list_reactions() to find the full reaction resource name.

    Args:
        reaction_name: The full reaction resource name
                      (format: 'spaces/SPACE_ID/messages/MESSAGE_ID/reactions/REACTION_ID')

    Returns:
        Confirmation of deletion
    """
    from google_chat import delete_reaction as _delete_reaction
    return await _delete_reaction(reaction_name)

@mcp.tool()
async def download_attachment(resource_name: str, save_dir: str = '/tmp', content_name: str = None) -> Dict:
    """Download a file attachment from a Google Chat message.

    Use this after get_message or get_messages returns a message with
    an 'attachment' field. Pass the resourceName and contentName from the attachment metadata.

    Args:
        resource_name: The resourceName string from the attachment's metadata
                      (base64-encoded, from attachmentDataRef)
        save_dir: Directory to save the file (default: /tmp)
        content_name: Original filename (e.g. 'image.png') for correct file extension

    Returns:
        Dict with path (saved file location), contentName, contentType, and size in bytes
    """
    from google_chat import download_attachment as _download
    return await _download(resource_name, save_dir, content_name)

@mcp.tool()
def authenticate() -> str:
    """Start (or restart) Google Chat OAuth authentication.

    Call this if another tool fails with a credentials/authentication error. It returns
    an authorization URL - share it with the user and ask them to open it in a browser and
    complete authorization. Once they do, call complete_authentication with the resulting
    callback URL to finish.

    Returns:
        The authorization URL for the user to open in a browser
    """
    from google_chat import start_authentication
    return start_authentication()

@mcp.tool()
def complete_authentication(callback_url: str) -> Dict:
    """Complete an in-progress OAuth flow for Google Chat.

    Call authenticate first to start the flow and get the authorization URL. After the
    user authorizes in their browser, it redirects to a
    'http://localhost:8000/auth/callback?code=...&scope=...' URL - that page will likely
    fail to load, but the URL in the browser's address bar is still valid. Pass that full
    URL here as callback_url (a bare code also works).

    Args:
        callback_url: The full callback URL from the browser address bar after authorizing

    Returns:
        A dict with authentication status details
    """
    from google_chat import complete_authentication as _complete_authentication
    return _complete_authentication(callback_url)

def run_channel(args) -> None:
    """Serve the normal tools plus the channel capability, watch tools, and poller."""
    from pathlib import Path
    import anyio
    from mcp.server.stdio import stdio_server
    from channel import Channel, ChannelStore, INSTRUCTIONS

    state_path = Path(args.channel_state_path or Path(args.token_path).parent / 'channel_state.json')
    channel = Channel(ChannelStore(state_path), args.poll_seconds)

    def watch_space(space_name: str, allowed_senders: List[str] = None, mention_only: bool = False) -> Dict:
        """Start pushing new messages from a Google Chat space into this Claude Code session,
        or replace the settings of a space that is already watched.

        Only messages from allowed_senders are delivered. When omitted, only the
        authenticated user is allowed. With mention_only, only messages that
        mention @BOT_NAME (case-insensitive) are delivered; without it, every
        message from an allowed sender is. Calling again replaces both settings,
        so pass the current allowed_senders when you only want to change
        mention_only. Takes
        effect on the next poll; history before this call is never replayed.

        Args:
            space_name: The space to watch (format: 'spaces/SPACE_ID')
            allowed_senders: Optional list of 'users/USER_ID' whose messages are delivered
            mention_only: Deliver only messages that mention @BOT_NAME
        """
        return channel.watch(space_name, allowed_senders, mention_only)

    def unwatch_space(space_name: str) -> Dict:
        """Stop pushing messages from a Google Chat space into this session.

        Args:
            space_name: The space to stop watching (format: 'spaces/SPACE_ID')
        """
        return channel.unwatch(space_name)

    def list_watched_spaces() -> Dict:
        """List the channel config: the bot name and its @mention, the message ID prefix
        that marks this server's own messages, which process is polling (poller.active_here
        is false when another channel session on this machine receives the messages), and
        each watched space with its allowed senders and mention_only setting. The bot name
        comes from the BOT_NAME env var and cannot be changed by a tool."""
        return channel.list_watched()

    for fn in (watch_space, unwatch_space, list_watched_spaces):
        mcp.add_tool(fn)

    server = mcp._mcp_server
    server.instructions = INSTRUCTIONS
    options = server.create_initialization_options(
        experimental_capabilities={'claude/channel': {}})

    async def main():
        async with stdio_server() as (read_stream, write_stream):
            async with anyio.create_task_group() as tg:
                tg.start_soon(channel.run, write_stream)
                await server.run(read_stream, write_stream, options)
                tg.cancel_scope.cancel()

    anyio.run(main)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='MCP Server with Google Chat Authentication')
    parser.add_argument('--auth', choices=['web', 'cli'],
                        help='Run OAuth authentication (web: browser-based, cli: headless/terminal)')
    parser.add_argument('--host', default='localhost', help='Host to bind the auth server to (default: localhost)')
    parser.add_argument('--port', type=int, default=8000, help='Port to run the auth server on (default: 8000)')
    parser.add_argument('--token-path', default='token.json', help='Path to store OAuth token (default: token.json)')
    parser.add_argument('--raw-messages', action='store_true', help='Return raw API messages without filtering fields (filtered by default)')
    parser.add_argument('--channel', action='store_true', help='Run as a Claude Code channel: push new messages from watched spaces into the session')
    parser.add_argument('--channel-state-path', help='Where watched spaces are stored (default: channel_state.json next to the token)')
    parser.add_argument('--poll-seconds', type=float, default=5.0, help='Channel poll interval in seconds (default: 5)')

    args = parser.parse_args()

    # Set the token path for OAuth storage
    set_token_path(args.token_path)

    # Set message filtering (disabled when --raw-messages is used)
    set_filter_messages(not args.raw_messages)

    if args.auth == 'web':
        print(f"\nStarting OAuth authentication server at http://{args.host}:{args.port}")
        print("Available endpoints:")
        print("  - /auth   : Start OAuth authentication flow")
        print("  - /status : Check authentication status")
        print("  - /auth/callback : OAuth callback endpoint")
        print(f"\nDefault callback URL: {DEFAULT_CALLBACK_URL}")
        print(f"Token will be stored at: {args.token_path}")
        print("\nPress CTRL+C to stop the server")
        print("-" * 50)
        run_auth_server(port=args.port, host=args.host)
    elif args.auth == 'cli':
        run_cli_auth()
    elif args.channel:
        run_channel(args)
    else:
        mcp.run()
