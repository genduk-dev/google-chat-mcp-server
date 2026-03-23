import os
import datetime
import uuid
from typing import List, Dict, Optional, Tuple
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from pathlib import Path

# If modifying these scopes, delete the file token.json.
SCOPES = [
    'https://www.googleapis.com/auth/chat.spaces.readonly',
    'https://www.googleapis.com/auth/chat.messages',
    'https://www.googleapis.com/auth/chat.memberships.readonly',
    'https://www.googleapis.com/auth/userinfo.profile',
    'https://www.googleapis.com/auth/directory.readonly',
]

# Cache for user display names: {user_id: display_name}
_user_display_name_cache: Dict[str, str] = {}
DEFAULT_CALLBACK_URL = "http://localhost:8000/auth/callback"
DEFAULT_TOKEN_PATH = 'token.json'
APP_MESSAGE_PREFIX = os.environ.get('APP_MESSAGE_PREFIX', 'client-genduk-')

# Store credentials info
token_info = {
    'credentials': None,
    'last_refresh': None,
    'token_path': DEFAULT_TOKEN_PATH
}

def set_token_path(path: str) -> None:
    """Set the global token path for OAuth storage.

    Args:
        path: Path where the token should be stored
    """
    token_info['token_path'] = os.path.expanduser(path)

# Global flag for message filtering
SAVE_TOKEN_MODE = True

def set_save_token_mode(enabled: bool) -> None:
    """Set whether to filter message fields to save tokens.
    
    Args:
        enabled: True to enable filtering, False to disable
    """
    global SAVE_TOKEN_MODE
    SAVE_TOKEN_MODE = enabled

def save_credentials(creds: Credentials, token_path: Optional[str] = None) -> None:
    """Save credentials to file and update in-memory cache.
    
    Args:
        creds: The credentials to save
        token_path: Path to save the token file
    """
    # Use configured token path if none provided
    if token_path is None:
        token_path = token_info['token_path']
    
    # Save to file
    token_path = Path(token_path)
    with open(token_path, 'w') as token:
        token.write(creds.to_json())
    
    # Update in-memory cache
    token_info['credentials'] = creds
    token_info['last_refresh'] = datetime.datetime.now(datetime.timezone.utc)

def get_credentials(token_path: Optional[str] = None) -> Optional[Credentials]:
    """Gets valid user credentials from storage or memory.
    
    Args:
        token_path: Optional path to token file. If None, uses the configured path.
    
    Returns:
        Credentials object or None if no valid credentials exist
    """
    if token_path is None:
        token_path = token_info['token_path']
    
    creds = token_info['credentials']
    
    # If no credentials in memory, try to load from file
    if not creds:
        token_path = Path(token_path)
        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
            token_info['credentials'] = creds
    
    # If we have credentials that need refresh
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            save_credentials(creds, token_path)
        except Exception:
            return None
    
    return creds if (creds and creds.valid) else None

async def refresh_token(token_path: Optional[str] = None) -> Tuple[bool, str]:
    """Attempt to refresh the current token.
    
    Args:
        token_path: Path to the token file. If None, uses the configured path.
    
    Returns:
        Tuple of (success: bool, message: str)
    """
    if token_path is None:
        token_path = token_info['token_path']
        
    try:
        creds = token_info['credentials']
        if not creds:
            token_path = Path(token_path)
            if not token_path.exists():
                return False, "No token file found"
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        
        if not creds.refresh_token:
            return False, "No refresh token available"
        
        creds.refresh(Request())
        save_credentials(creds, token_path)
        return True, "Token refreshed successfully"
    except Exception as e:
        return False, f"Failed to refresh token: {str(e)}"

def prefetch_space_members(space_name: str, creds: Credentials) -> None:
    """Prefetch all members of a space and resolve their display names.

    First collects user IDs from Chat API memberships, then resolves names
    via People API directory lookup. Requires chat.memberships.readonly
    and directory.readonly scopes.

    Args:
        space_name: The space to fetch members from (format: 'spaces/SPACE_ID')
        creds: Valid credentials for API calls
    """
    try:
        # Step 1: Get all member user IDs from Chat API
        chat_service = build('chat', 'v1', credentials=creds)
        user_ids = []
        page_token = None
        while True:
            list_args = {'parent': space_name, 'pageSize': 100}
            if page_token:
                list_args['pageToken'] = page_token
            response = chat_service.spaces().members().list(**list_args).execute()
            for membership in response.get('memberships', []):
                member = membership.get('member', {})
                user_id = member.get('name', '')
                display_name = member.get('displayName', '')
                if display_name and user_id:
                    _user_display_name_cache[user_id] = display_name
                elif user_id and user_id not in _user_display_name_cache:
                    user_ids.append(user_id)
            page_token = response.get('nextPageToken')
            if not page_token:
                break

        # Step 2: Resolve names via People API for uncached users
        if user_ids:
            people_service = build('people', 'v1', credentials=creds)
            # People API getBatchGet supports up to 200 resource names
            resource_names = [uid.replace('users/', 'people/') for uid in user_ids]
            for i in range(0, len(resource_names), 50):
                batch = resource_names[i:i+50]
                try:
                    result = people_service.people().getBatchGet(
                        resourceNames=batch,
                        personFields='names'
                    ).execute()
                    for person_response in result.get('responses', []):
                        person = person_response.get('person', {})
                        resource_name = person.get('resourceName', '')
                        user_id = resource_name.replace('people/', 'users/')
                        names = person.get('names', [])
                        if names:
                            display_name = names[0].get('displayName', '')
                            if display_name:
                                _user_display_name_cache[user_id] = display_name
                except Exception:
                    pass  # People API batch failed, continue with what we have
    except Exception:
        pass  # Silently fail — will fall back to user IDs


def get_user_display_name(sender: Dict, creds: Credentials) -> str:
    """Get user display name with caching.

    Checks cache first (populated by prefetch_space_members), then tries
    People API for individual lookup, then falls back to raw user ID.

    Args:
        sender: The sender object from Chat API (contains 'name', 'type', optionally 'displayName')
        creds: Valid credentials for API calls

    Returns:
        User's display name, or a fallback identifier if lookup fails
    """
    user_id = sender.get('name', '')
    sender_type = sender.get('type', 'HUMAN')

    # Check if already cached (from prefetch_space_members)
    if user_id in _user_display_name_cache:
        return _user_display_name_cache[user_id]

    # If Chat API already provided displayName, use it
    if sender.get('displayName'):
        _user_display_name_cache[user_id] = sender['displayName']
        return sender['displayName']

    # For BOT type, extract short ID
    if sender_type == 'BOT':
        short_id = user_id.replace('users/', '') if user_id else 'unknown'
        display_name = f"Bot ({short_id[:8]}...)"
        _user_display_name_cache[user_id] = display_name
        return display_name

    # For HUMAN type, try People API individual lookup
    if sender_type == 'HUMAN' and user_id:
        try:
            person_id = user_id.replace('users/', 'people/')
            service = build('people', 'v1', credentials=creds)
            person = service.people().get(
                resourceName=person_id,
                personFields='names'
            ).execute()
            names = person.get('names', [])
            if names:
                display_name = names[0].get('displayName', user_id)
                _user_display_name_cache[user_id] = display_name
                return display_name
        except Exception:
            pass

    # Fallback: return user_id
    _user_display_name_cache[user_id] = user_id
    return user_id


# MCP functions
async def list_chat_spaces() -> List[Dict]:
    """Lists all Google Chat spaces the bot has access to."""
    try:
        creds = get_credentials()
        if not creds:
            raise Exception("No valid credentials found. Please authenticate first.")
            
        service = build('chat', 'v1', credentials=creds)
        all_spaces = []
        page_token = None
        while True:
            list_args = {'pageSize': 100}
            if page_token:
                list_args['pageToken'] = page_token
            response = service.spaces().list(**list_args).execute()
            all_spaces.extend(response.get('spaces', []))
            page_token = response.get('nextPageToken')
            if not page_token:
                break
        return all_spaces
    except Exception as e:
        raise Exception(f"Failed to list chat spaces: {str(e)}") 

async def list_space_messages(space_name: str, 
                            start_date: Optional[datetime.datetime] = None,
                            end_date: Optional[datetime.datetime] = None) -> List[Dict]:
    """Lists messages from a specific Google Chat space with optional time filtering.
    
    Args:
        space_name: The name/identifier of the space to fetch messages from
        start_date: Optional start datetime for filtering messages. If provided without end_date,
                   will query messages for the entire day of start_date
        end_date: Optional end datetime for filtering messages. Only used if start_date is also provided
    
    Returns:
        List of message objects from the space matching the time criteria
        
    Raises:
        Exception: If authentication fails or API request fails
    """
    try:
        creds = get_credentials()
        if not creds:
            raise Exception("No valid credentials found. Please authenticate first.")
            
        service = build('chat', 'v1', credentials=creds)
        
        # Prepare filter string based on provided dates
        filter_str = None
        if start_date:
            if end_date:
                # Format for date range query
                filter_str = f"createTime > \"{start_date.isoformat()}\" AND createTime < \"{end_date.isoformat()}\""
            else:
                # For single day query, set range from start of day to end of day
                day_start = start_date.replace(hour=0, minute=0, second=0, microsecond=0)
                day_end = day_start + datetime.timedelta(days=1)
                filter_str = f"createTime > \"{day_start.isoformat()}\" AND createTime < \"{day_end.isoformat()}\""
        
        # Make API request with pagination
        messages = []
        page_token = None
        
        while True:
            list_args = {
                'parent': space_name,
                'pageSize': 100
            }
            if filter_str:
                list_args['filter'] = filter_str
            if page_token:
                list_args['pageToken'] = page_token
                
            response = service.spaces().messages().list(**list_args).execute()
            
            # Extend messages list with current page results
            current_page_messages = response.get('messages', [])
            if current_page_messages:
                messages.extend(current_page_messages)
            
            page_token = response.get('nextPageToken')
            if not page_token:
                break

        if not SAVE_TOKEN_MODE:
            return messages

        # Prefetch space members to resolve display names
        prefetch_space_members(space_name, creds)

        filtered_messages = []
        for msg in messages:
            sender = msg.get('sender', {})
            display_name = get_user_display_name(sender, creds) if sender else 'Unknown'

            client_msg_id = msg.get('clientAssignedMessageId', '')
            filtered_msg = {
                'name': msg.get('name'),
                'sender': display_name,
                'sender_type': sender.get('type', 'HUMAN'),
                'sent_by_app': client_msg_id.startswith(APP_MESSAGE_PREFIX) if client_msg_id else False,
                'createTime': msg.get('createTime'),
                'text': msg.get('text'),
                'thread': msg.get('thread')
            }
            filtered_messages.append(filtered_msg)

        return filtered_messages
        
    except Exception as e:
        raise Exception(f"Failed to list messages in space: {str(e)}")


async def send_space_message(space_name: str, text: str, thread_key: Optional[str] = None, thread_name: Optional[str] = None) -> Dict:
    """Send a message to a Google Chat space.

    Args:
        space_name: The space to send to (format: 'spaces/SPACE_ID')
        text: The message text to send
        thread_key: Optional thread key for bot-initiated threads (creates new thread if not found)
        thread_name: Optional thread name to reply in an existing thread (format: 'spaces/SPACE_ID/threads/THREAD_ID')

    Returns:
        The created message object
    """
    try:
        creds = get_credentials()
        if not creds:
            raise Exception("No valid credentials found. Please authenticate first.")

        service = build('chat', 'v1', credentials=creds)

        body = {'text': text}

        # Auto-assign client message ID with app prefix for attribution
        message_id = f"{APP_MESSAGE_PREFIX}{uuid.uuid4().hex[:12]}"

        kwargs = {
            'parent': space_name,
            'body': body,
            'messageId': message_id,
        }

        if thread_name:
            body['thread'] = {'name': thread_name}
            kwargs['messageReplyOption'] = 'REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD'
        elif thread_key:
            body['thread'] = {'threadKey': thread_key}
            kwargs['messageReplyOption'] = 'REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD'

        result = service.spaces().messages().create(**kwargs).execute()
        return {
            'name': result.get('name'),
            'createTime': result.get('createTime'),
            'text': result.get('text'),
            'thread': result.get('thread'),
            'space': result.get('space', {}).get('name'),
            'clientAssignedMessageId': result.get('clientAssignedMessageId'),
        }
    except Exception as e:
        raise Exception(f"Failed to send message: {str(e)}")


async def delete_space_message(message_name: str) -> Dict:
    """Delete a message from a Google Chat space.

    Args:
        message_name: The resource name of the message to delete
                     (format: 'spaces/SPACE_ID/messages/MESSAGE_ID')

    Returns:
        Confirmation dict
    """
    try:
        creds = get_credentials()
        if not creds:
            raise Exception("No valid credentials found. Please authenticate first.")

        service = build('chat', 'v1', credentials=creds)
        service.spaces().messages().delete(name=message_name).execute()
        return {'deleted': message_name, 'success': True}
    except Exception as e:
        raise Exception(f"Failed to delete message: {str(e)}")


async def get_message(message_name: str) -> Dict:
    """Fetch a single message by its resource name.

    Args:
        message_name: The resource name of the message
                     (format: 'spaces/SPACE_ID/messages/MESSAGE_ID')

    Returns:
        The message object with name, sender, createTime, text, and thread
    """
    try:
        creds = get_credentials()
        if not creds:
            raise Exception("No valid credentials found. Please authenticate first.")

        service = build('chat', 'v1', credentials=creds)
        msg = service.spaces().messages().get(name=message_name).execute()

        if not SAVE_TOKEN_MODE:
            return msg

        sender = msg.get('sender', {})
        display_name = get_user_display_name(sender, creds) if sender else 'Unknown'

        client_msg_id = msg.get('clientAssignedMessageId', '')
        return {
            'name': msg.get('name'),
            'sender': display_name,
            'sender_type': sender.get('type', 'HUMAN'),
            'sent_by_app': client_msg_id.startswith(APP_MESSAGE_PREFIX) if client_msg_id else False,
            'createTime': msg.get('createTime'),
            'text': msg.get('text'),
            'thread': msg.get('thread'),
        }
    except Exception as e:
        raise Exception(f"Failed to get message: {str(e)}")


async def update_message(message_name: str, text: str) -> Dict:
    """Edit the text of an existing message.

    Args:
        message_name: The resource name of the message to update
                     (format: 'spaces/SPACE_ID/messages/MESSAGE_ID')
        text: The new text content for the message

    Returns:
        The updated message object with name, createTime, text, and thread
    """
    try:
        creds = get_credentials()
        if not creds:
            raise Exception("No valid credentials found. Please authenticate first.")

        service = build('chat', 'v1', credentials=creds)
        result = service.spaces().messages().patch(
            name=message_name,
            updateMask='text',
            body={'text': text},
        ).execute()

        return {
            'name': result.get('name'),
            'createTime': result.get('createTime'),
            'lastUpdateTime': result.get('lastUpdateTime'),
            'text': result.get('text'),
            'thread': result.get('thread'),
        }
    except Exception as e:
        raise Exception(f"Failed to update message: {str(e)}")


async def create_reaction(message_name: str, emoji_unicode: str) -> Dict:
    """Add an emoji reaction to a message.

    Args:
        message_name: The resource name of the message to react to
                     (format: 'spaces/SPACE_ID/messages/MESSAGE_ID')
        emoji_unicode: The Unicode emoji string to react with (e.g. '👍', '❤️', '😂')

    Returns:
        The created reaction object
    """
    try:
        creds = get_credentials()
        if not creds:
            raise Exception("No valid credentials found. Please authenticate first.")

        service = build('chat', 'v1', credentials=creds)
        result = service.spaces().messages().reactions().create(
            parent=message_name,
            body={'emoji': {'unicode': emoji_unicode}},
        ).execute()

        return result
    except Exception as e:
        raise Exception(f"Failed to create reaction: {str(e)}")


async def list_reactions(message_name: str) -> List[Dict]:
    """List all reactions on a message.

    Args:
        message_name: The resource name of the message
                     (format: 'spaces/SPACE_ID/messages/MESSAGE_ID')

    Returns:
        List of reaction objects, each containing emoji and user info
    """
    try:
        creds = get_credentials()
        if not creds:
            raise Exception("No valid credentials found. Please authenticate first.")

        service = build('chat', 'v1', credentials=creds)
        all_reactions = []
        page_token = None
        while True:
            list_args = {'parent': message_name, 'pageSize': 100}
            if page_token:
                list_args['pageToken'] = page_token
            result = service.spaces().messages().reactions().list(**list_args).execute()
            all_reactions.extend(result.get('reactions', []))
            page_token = result.get('nextPageToken')
            if not page_token:
                break

        return all_reactions
    except Exception as e:
        raise Exception(f"Failed to list reactions: {str(e)}")


async def send_message_with_attachment(
    space_name: str,
    text: str,
    file_url: str,
    filename: Optional[str] = None,
    thread_name: Optional[str] = None,
) -> Dict:
    """Send a message with a file link to a Google Chat space.

    NOTE: This is a simplified attachment implementation. The Google Chat API's
    media.upload endpoint for user OAuth has significant restrictions (requires
    service account or specific app configuration). This function sends a message
    where the file is referenced as a clickable link embedded in the message text.
    For true file uploads, use a service account with the Chat API media.upload
    endpoint or share the file via Google Drive first.

    Args:
        space_name: The space to send to (format: 'spaces/SPACE_ID')
        text: The message text to accompany the file link
        file_url: The URL of the file to link (e.g. a Google Drive share link or public URL)
        filename: Optional display name for the file link. Defaults to the URL if not provided.
        thread_name: Optional thread name to reply in an existing thread
                    (format: 'spaces/SPACE_ID/threads/THREAD_ID')

    Returns:
        The created message object with name, createTime, text, thread, and space
    """
    try:
        creds = get_credentials()
        if not creds:
            raise Exception("No valid credentials found. Please authenticate first.")

        service = build('chat', 'v1', credentials=creds)

        link_label = filename or file_url
        full_text = f"{text}\n📎 {link_label}: {file_url}" if text else f"📎 {link_label}: {file_url}"

        body = {'text': full_text}
        message_id = f"{APP_MESSAGE_PREFIX}{uuid.uuid4().hex[:12]}"
        kwargs = {'parent': space_name, 'body': body, 'messageId': message_id}

        if thread_name:
            body['thread'] = {'name': thread_name}
            kwargs['messageReplyOption'] = 'REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD'

        result = service.spaces().messages().create(**kwargs).execute()

        return {
            'name': result.get('name'),
            'createTime': result.get('createTime'),
            'text': result.get('text'),
            'thread': result.get('thread'),
            'space': result.get('space', {}).get('name'),
            'clientAssignedMessageId': result.get('clientAssignedMessageId'),
        }
    except Exception as e:
        raise Exception(f"Failed to send message with attachment: {str(e)}")

