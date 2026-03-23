# server.py
import argparse
from typing import List, Dict

from fastmcp import FastMCP
from google_chat import list_chat_spaces, DEFAULT_CALLBACK_URL, set_token_path, set_save_token_mode
from server_auth import run_auth_server
from auth_cli import run_cli_auth

# Create an MCP server
mcp = FastMCP("Google Chat")

@mcp.tool()
async def get_chat_spaces() -> List[Dict]:
    """List all Google Chat spaces the bot has access to.
    
    This tool requires OAuth authentication. On first run, it will open a browser window
    for you to log in with your Google account. Make sure you have credentials.json
    downloaded from Google Cloud Console in the current directory.
    """
    return await list_chat_spaces()

@mcp.tool()
async def get_space_messages(space_name: str, 
                           start_date: str,
                           end_date: str = None) -> List[Dict]:
    """List messages from a specific Google Chat space with optional time filtering.
    
    This tool requires OAuth authentication. The space_name should be in the format
    'spaces/your_space_id'. Dates should be in YYYY-MM-DD format (e.g., '2024-03-22').
    
    When only start_date is provided, it will query messages for that entire day.
    When both dates are provided, it will query messages from start_date 00:00:00Z
    to end_date 23:59:59Z.
    
    Args:
        space_name: The name/identifier of the space to fetch messages from
        start_date: Required start date in YYYY-MM-DD format
        end_date: Optional end date in YYYY-MM-DD format
    
    Returns:
        List of message objects from the space matching the time criteria
        
    Raises:
        ValueError: If the date format is invalid or dates are in wrong order
    """
    from google_chat import list_space_messages
    from datetime import datetime, timezone

    try:
        # Parse start date and set to beginning of day (00:00:00Z)
        start_datetime = datetime.strptime(start_date, '%Y-%m-%d').replace(
            hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc
        )
        
        # Parse end date if provided and set to end of day (23:59:59Z)
        end_datetime = None
        if end_date:
            end_datetime = datetime.strptime(end_date, '%Y-%m-%d').replace(
                hour=23, minute=59, second=59, microsecond=999999, tzinfo=timezone.utc
            )
            
            # Validate date range
            if start_datetime > end_datetime:
                raise ValueError("start_date must be before end_date")
    except ValueError as e:
        if "strptime" in str(e):
            raise ValueError("Dates must be in YYYY-MM-DD format (e.g., '2024-03-22')")
        raise e
    
    return await list_space_messages(space_name, start_datetime, end_datetime)

@mcp.tool()
async def send_space_message(space_name: str, text: str, thread_key: str = None, thread_name: str = None) -> Dict:
    """Send a message to a Google Chat space.

    Args:
        space_name: The space to send to (format: 'spaces/SPACE_ID')
        text: The message text to send
        thread_key: Optional thread key for bot-initiated threads (creates new thread if not found)
        thread_name: Optional thread name to reply in an existing thread (format: 'spaces/SPACE_ID/threads/THREAD_ID')

    Returns:
        The created message object with name, createTime, text, thread, and space
    """
    from google_chat import send_space_message as _send
    return await _send(space_name, text, thread_key, thread_name)

@mcp.tool()
async def delete_space_message(message_name: str) -> Dict:
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
async def update_message(message_name: str, text: str) -> Dict:
    """Edit the text of an existing message in a Google Chat space.

    Only messages sent by the authenticated user can be edited.

    Args:
        message_name: The resource name of the message to update
                     (format: 'spaces/SPACE_ID/messages/MESSAGE_ID')
        text: The new text content for the message

    Returns:
        The updated message object with name, createTime, lastUpdateTime, text, and thread
    """
    from google_chat import update_message as _update_message
    return await _update_message(message_name, text)

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
async def send_message_with_attachment(
    space_name: str,
    text: str,
    file_url: str,
    filename: str = None,
    thread_key: str = None,
    thread_name: str = None,
) -> Dict:
    """Send a message with a file link to a Google Chat space.

    Simplified attachment implementation — embeds the file as a clickable link
    in the message text. For true binary file uploads, a service account with
    media.upload access is required; this version works with any OAuth credentials.

    Args:
        space_name: The space to send to (format: 'spaces/SPACE_ID')
        text: The message text to accompany the file link
        file_url: The URL of the file to link (e.g. a Google Drive share link or public URL)
        filename: Optional display name for the file link. Defaults to the URL if not provided.
        thread_key: Optional thread key for bot-initiated threads (creates new thread if not found)
        thread_name: Optional thread name to reply in an existing thread
                    (format: 'spaces/SPACE_ID/threads/THREAD_ID')

    Returns:
        The created message object with name, createTime, text, thread, and space
    """
    from google_chat import send_message_with_attachment as _send_with_attachment
    return await _send_with_attachment(space_name, text, file_url, filename, thread_key, thread_name)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='MCP Server with Google Chat Authentication')
    parser.add_argument('--auth', choices=['web', 'cli'],
                        help='Run OAuth authentication (web: browser-based, cli: headless/terminal)')
    parser.add_argument('--host', default='localhost', help='Host to bind the auth server to (default: localhost)')
    parser.add_argument('--port', type=int, default=8000, help='Port to run the auth server on (default: 8000)')
    parser.add_argument('--token-path', default='token.json', help='Path to store OAuth token (default: token.json)')
    parser.add_argument('--raw-messages', action='store_true', help='Return raw API messages without filtering fields (filtered by default)')

    args = parser.parse_args()

    # Set the token path for OAuth storage
    set_token_path(args.token_path)

    # Set message filtering (disabled when --raw-messages is used)
    set_save_token_mode(not args.raw_messages)

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
    else:
        mcp.run()