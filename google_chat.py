import os
import logging
import datetime
import json
import uuid
import urllib.parse
import urllib.request
import urllib.error
from typing import List, Dict, Optional, Tuple
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from pathlib import Path

logger = logging.getLogger(__name__)

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

# Cached API service objects (keyed by credentials token)
_service_cache: Dict[str, object] = {}

def _get_service(api: str, version: str, creds: Credentials) -> object:
    """Get or create a cached Google API service object."""
    cache_key = f"{api}:{version}:{creds.token}"
    if cache_key not in _service_cache:
        # Clear stale entries for same api:version with old tokens
        prefix = f"{api}:{version}:"
        stale = [k for k in _service_cache if k.startswith(prefix) and k != cache_key]
        for k in stale:
            del _service_cache[k]
        _service_cache[cache_key] = build(api, version, credentials=creds)
    return _service_cache[cache_key]

def _build_send_kwargs(space_name: str, body: Dict, thread_key: Optional[str] = None, thread_name: Optional[str] = None) -> Dict:
    """Build kwargs for spaces().messages().create() with thread and messageId handling."""
    message_id = f"{APP_MESSAGE_PREFIX}{uuid.uuid4().hex[:12]}"
    kwargs = {'parent': space_name, 'body': body, 'messageId': message_id}
    if thread_name:
        body['thread'] = {'name': thread_name}
        kwargs['messageReplyOption'] = 'REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD'
    elif thread_key:
        body['thread'] = {'threadKey': thread_key}
        kwargs['messageReplyOption'] = 'REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD'
    return kwargs

def _format_sent_message(result: Dict) -> Dict:
    """Format a sent message API response into a consistent output dict."""
    return {
        'name': result.get('name'),
        'createTime': result.get('createTime'),
        'text': result.get('text'),
        'thread': result.get('thread'),
        'space': result.get('space', {}).get('name'),
        'clientAssignedMessageId': result.get('clientAssignedMessageId'),
    }

# Max messages to fetch in a single list_space_messages call
MAX_MESSAGES = 1000
DEFAULT_CALLBACK_URL = "http://localhost:8000/auth/callback"
DEFAULT_TOKEN_PATH = 'token.json'
APP_MESSAGE_PREFIX = os.environ.get('APP_MESSAGE_PREFIX', 'client-gchat-mcp-')

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
FILTER_MESSAGES = True

def set_filter_messages(enabled: bool) -> None:
    """Set whether to filter message fields to save tokens.
    
    Args:
        enabled: True to enable filtering, False to disable
    """
    global FILTER_MESSAGES
    FILTER_MESSAGES = enabled

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
        except Exception as e:
            logger.warning("Failed to refresh credentials: %s", e)
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
        chat_service = _get_service('chat', 'v1', creds)
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
            people_service = _get_service('people', 'v1', creds)
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
                except Exception as e:
                    logger.debug("People API batch lookup failed: %s", e)
    except Exception as e:
        logger.debug("Failed to prefetch space members: %s", e)


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
            service = _get_service('people', 'v1', creds)
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


async def list_space_members(space_name: str) -> List[Dict]:
    """List all members of a space with their user IDs and display names.

    Args:
        space_name: The space to list members from (format: 'spaces/SPACE_ID')

    Returns:
        List of member dicts with 'user_id', 'display_name', and 'mention' fields
    """
    try:
        creds = get_credentials()
        if not creds:
            raise Exception("No valid credentials found. Please authenticate first.")

        # Reuse prefetch to populate cache
        prefetch_space_members(space_name, creds)

        # Also collect raw membership data for role info
        chat_service = _get_service('chat', 'v1', creds)
        members = []
        page_token = None
        while True:
            list_args = {'parent': space_name, 'pageSize': 100}
            if page_token:
                list_args['pageToken'] = page_token
            response = chat_service.spaces().members().list(**list_args).execute()
            for membership in response.get('memberships', []):
                member = membership.get('member', {})
                user_id = member.get('name', '')
                if not user_id:
                    continue
                display_name = _user_display_name_cache.get(user_id, user_id)
                members.append({
                    'user_id': user_id,
                    'display_name': display_name,
                    'mention': f'<{user_id}>',
                    'type': member.get('type', 'HUMAN'),
                    'role': membership.get('role', 'ROLE_MEMBER'),
                })
            page_token = response.get('nextPageToken')
            if not page_token:
                break
        return members
    except Exception as e:
        raise Exception(f"Failed to list space members: {str(e)}")


# MCP functions
async def list_chat_spaces() -> List[Dict]:
    """Lists all Google Chat spaces the bot has access to."""
    try:
        creds = get_credentials()
        if not creds:
            raise Exception("No valid credentials found. Please authenticate first.")
            
        service = _get_service('chat', 'v1', creds)
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
            
        service = _get_service('chat', 'v1', creds)
        
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
            if not page_token or len(messages) >= MAX_MESSAGES:
                break

        if not FILTER_MESSAGES:
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
                'lastUpdateTime': msg.get('lastUpdateTime'),
                'text': msg.get('text'),
                'thread': msg.get('thread')
            }
            if msg.get('quotedMessageMetadata'):
                filtered_msg['quotedMessageMetadata'] = msg['quotedMessageMetadata']
            filtered_msg['threadReply'] = msg.get('threadReply', False)
            if msg.get('attachment'):
                filtered_msg['attachment'] = [
                    {'contentName': a.get('contentName'), 'contentType': a.get('contentType'),
                     'resourceName': a.get('attachmentDataRef', {}).get('resourceName')}
                    for a in msg['attachment']
                ]
            if msg.get('emojiReactionSummaries'):
                filtered_msg['emojiReactionSummaries'] = msg['emojiReactionSummaries']
            filtered_messages.append(filtered_msg)

        return filtered_messages
        
    except Exception as e:
        raise Exception(f"Failed to list messages in space: {str(e)}")


def _chat_api_post(path: str, body: Dict, creds: Credentials) -> Dict:
    """POST to a Chat API v1 endpoint that the discovery client does not expose.

    Used for methods missing from google-api-python-client's discovery document
    (currently spaces.messages.search, a Developer Preview method). Mirrors the
    raw-HTTP pattern in download_attachment.

    Args:
        path: API path below /v1/, e.g. 'spaces/-/messages:search'
        body: JSON request body
        creds: Valid credentials (get_credentials() has already refreshed them)

    Returns:
        Parsed JSON response

    Raises:
        urllib.error.HTTPError: propagated unchanged so callers can inspect .code
    """
    url = f"https://chat.googleapis.com/v1/{path}"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode('utf-8'),
        headers={
            "Authorization": f"Bearer {creds.token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


async def search_space_messages(query: str,
                                space_name: Optional[str] = None,
                                limit: int = 50,
                                page_token: Optional[str] = None) -> Dict:
    """Full-text search across Google Chat messages via spaces.messages.search.

    Args:
        query: Free-text search string, passed through as the API filter
        space_name: Optional 'spaces/XXXX' to scope the search; None searches
                    every accessible space
        limit: Max messages in this page of results (clamped to 1..MAX_MESSAGES)
        page_token: Opaque continuation token from a previous call's nextPageToken

    Returns:
        {'messages': [...], 'nextPageToken': str or None}

    Raises:
        Exception: If authentication fails, preview access is unavailable, or the
                   API request fails
    """
    creds = get_credentials()
    if not creds:
        raise Exception("No valid credentials found. Please authenticate first.")

    limit = max(1, min(limit, MAX_MESSAGES))
    parent = "spaces/-"
    filter_str = query
    if space_name:
        filter_str = f'{filter_str} AND space.name = "{space_name}"'

    results = []
    next_token = page_token
    try:
        while len(results) < limit:
            body = {
                'filter': filter_str,
                'pageSize': min(100, limit - len(results)),
            }
            if next_token:
                body['pageToken'] = next_token

            response = _chat_api_post(f"{parent}/messages:search", body, creds)
            results.extend(entry.get('message', {}) for entry in response.get('results', []))
            next_token = response.get('nextPageToken')
            if not next_token:
                break
    except urllib.error.HTTPError as e:
        detail = e.read().decode('utf-8', errors='replace')[:500]
        if e.code in (403, 404):
            raise Exception(
                "Failed to search messages: the spaces.messages.search method is in "
                "Google Workspace Developer Preview and this account/project appears "
                f"to have lost access (HTTP {e.code}). Other tools are unaffected. "
                f"API said: {detail}"
            )
        if e.code == 400:
            raise Exception(
                f"Failed to search messages: API rejected filter {filter_str!r} "
                f"(HTTP 400). API said: {detail}"
            )
        raise Exception(f"Failed to search messages: HTTP {e.code} {detail}")
    except Exception as e:
        raise Exception(f"Failed to search messages: {str(e)}")

    if not FILTER_MESSAGES:
        return {'messages': results, 'nextPageToken': next_token}

    filtered_messages = []
    for msg in results:
        sender = msg.get('sender', {})
        display_name = get_user_display_name(sender, creds) if sender else 'Unknown'
        client_msg_id = msg.get('clientAssignedMessageId', '')
        name = msg.get('name', '')
        space = msg.get('space', {}).get('name') or '/'.join(name.split('/')[:2])
        filtered_messages.append({
            'name': name,
            'space': space,
            'sender': display_name,
            'sender_type': sender.get('type', 'HUMAN'),
            'sent_by_app': client_msg_id.startswith(APP_MESSAGE_PREFIX) if client_msg_id else False,
            'createTime': msg.get('createTime'),
            'text': msg.get('text'),
            'thread': msg.get('thread'),
        })

    return {'messages': filtered_messages, 'nextPageToken': next_token}


async def send_space_message(space_name: str, text: str, thread_key: Optional[str] = None, thread_name: Optional[str] = None, quote_reply_message_name: Optional[str] = None, file_paths: Optional[List[str]] = None, filenames: Optional[List[str]] = None) -> Dict:
    """Send a message to a Google Chat space, optionally with file attachments.

    Args:
        space_name: The space to send to (format: 'spaces/SPACE_ID')
        text: The message text to send
        thread_key: Optional thread key for bot-initiated threads (creates new thread if not found)
        thread_name: Optional thread name to reply in an existing thread (format: 'spaces/SPACE_ID/threads/THREAD_ID')
        quote_reply_message_name: Optional message resource name to quote-reply to (format: 'spaces/SPACE_ID/messages/MESSAGE_ID')
        file_paths: Optional list of local file paths or HTTP(S) URLs to upload as attachments
        filenames: Optional list of display names for the attachments (matched by index to file_paths)

    Returns:
        The created message object
    """
    import mimetypes
    import tempfile
    from googleapiclient.http import MediaFileUpload

    try:
        creds = get_credentials()
        if not creds:
            raise Exception("No valid credentials found. Please authenticate first.")

        service = _get_service('chat', 'v1', creds)

        body = {'text': text}
        if quote_reply_message_name:
            # Fetch the quoted message to get lastUpdateTime (required by API)
            try:
                quoted_msg = service.spaces().messages().get(name=quote_reply_message_name).execute()
                last_update = quoted_msg.get('lastUpdateTime') or quoted_msg.get('createTime')
            except Exception:
                last_update = None
            metadata = {'name': quote_reply_message_name}
            if last_update:
                metadata['lastUpdateTime'] = last_update
            body['quotedMessageMetadata'] = metadata

        # Upload attachments if provided
        temp_files = []
        if file_paths:
            attachments = []
            for i, fp in enumerate(file_paths):
                local_path = fp
                temp_file = None
                if fp.startswith('http://') or fp.startswith('https://'):
                    url_path = urllib.parse.urlparse(fp).path
                    ext = os.path.splitext(url_path)[1] or '.bin'
                    temp_file = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
                    temp_file.close()
                    local_path = temp_file.name
                    temp_files.append(local_path)
                    urllib.request.urlretrieve(fp, local_path)

                if not os.path.exists(local_path):
                    raise Exception(f"File not found: {local_path}")

                fname = (filenames[i] if filenames and i < len(filenames) else None) or os.path.basename(local_path)
                mime_type, _ = mimetypes.guess_type(local_path)
                if not mime_type:
                    mime_type = 'application/octet-stream'

                media = MediaFileUpload(local_path, mimetype=mime_type)
                upload_result = service.media().upload(
                    parent=space_name,
                    body={'filename': fname},
                    media_body=media
                ).execute()
                upload_token = upload_result['attachmentDataRef']['attachmentUploadToken']

                attachments.append({
                    'contentName': fname,
                    'contentType': mime_type,
                    'attachmentDataRef': {'attachmentUploadToken': upload_token}
                })

            body['attachment'] = attachments

        kwargs = _build_send_kwargs(space_name, body, thread_key, thread_name)
        result = service.spaces().messages().create(**kwargs).execute()

        # Clean up temp files
        for tf in temp_files:
            try:
                os.unlink(tf)
            except OSError:
                pass

        return _format_sent_message(result)
    except Exception as e:
        for tf in temp_files if 'temp_files' in dir() else []:
            try:
                os.unlink(tf)
            except OSError:
                pass
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

        service = _get_service('chat', 'v1', creds)
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

        service = _get_service('chat', 'v1', creds)
        msg = service.spaces().messages().get(name=message_name).execute()

        if not FILTER_MESSAGES:
            return msg

        sender = msg.get('sender', {})
        display_name = get_user_display_name(sender, creds) if sender else 'Unknown'

        client_msg_id = msg.get('clientAssignedMessageId', '')
        result = {
            'name': msg.get('name'),
            'sender': display_name,
            'sender_type': sender.get('type', 'HUMAN'),
            'sent_by_app': client_msg_id.startswith(APP_MESSAGE_PREFIX) if client_msg_id else False,
            'createTime': msg.get('createTime'),
            'lastUpdateTime': msg.get('lastUpdateTime'),
            'text': msg.get('text'),
            'thread': msg.get('thread'),
        }
        if msg.get('quotedMessageMetadata'):
            result['quotedMessageMetadata'] = msg['quotedMessageMetadata']
        result['threadReply'] = msg.get('threadReply', False)
        if msg.get('attachment'):
            result['attachment'] = [
                {'contentName': a.get('contentName'), 'contentType': a.get('contentType'),
                 'resourceName': a.get('attachmentDataRef', {}).get('resourceName')}
                for a in msg['attachment']
            ]
        if msg.get('emojiReactionSummaries'):
            result['emojiReactionSummaries'] = msg['emojiReactionSummaries']
        return result
    except Exception as e:
        raise Exception(f"Failed to get message: {str(e)}")


async def update_message(message_name: str, text: str = None, file_paths: Optional[List[str]] = None, filenames: Optional[List[str]] = None, remove_quote_reply: bool = False) -> Dict:
    """Edit an existing message — update text, add/replace attachments, or both.

    Args:
        message_name: The resource name of the message to update
                     (format: 'spaces/SPACE_ID/messages/MESSAGE_ID')
        text: New text content for the message. If None, text is not changed.
        file_paths: List of local file paths or HTTP(S) URLs to upload as attachments.
                   If provided, replaces any existing attachments. If None, attachments are not changed.
        filenames: List of display names for the attachments (matched by index to file_paths).
        remove_quote_reply: If True, removes the quoted message from this message.
                           Note: quote replies can only be removed, not added via edit.

    Returns:
        The updated message object with name, createTime, lastUpdateTime, text, and thread
    """
    import mimetypes
    import tempfile
    from googleapiclient.http import MediaFileUpload

    temp_files = []
    try:
        creds = get_credentials()
        if not creds:
            raise Exception("No valid credentials found. Please authenticate first.")

        service = _get_service('chat', 'v1', creds)

        update_fields = []
        body = {}

        if text is not None:
            update_fields.append('text')
            body['text'] = text

        if remove_quote_reply:
            update_fields.append('quotedMessageMetadata')

        if file_paths is not None:
            space_name = '/'.join(message_name.split('/')[:2])
            attachments = []

            for i, fp in enumerate(file_paths):
                local_path = fp
                if fp.startswith('http://') or fp.startswith('https://'):
                    url_path = urllib.parse.urlparse(fp).path
                    ext = os.path.splitext(url_path)[1] or '.bin'
                    temp_file = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
                    temp_file.close()
                    local_path = temp_file.name
                    temp_files.append(local_path)
                    urllib.request.urlretrieve(fp, local_path)

                if not os.path.exists(local_path):
                    raise Exception(f"File not found: {local_path}")

                fname = (filenames[i] if filenames and i < len(filenames) else None) or os.path.basename(local_path)
                mime_type, _ = mimetypes.guess_type(local_path)
                if not mime_type:
                    mime_type = 'application/octet-stream'

                media = MediaFileUpload(local_path, mimetype=mime_type)
                upload_result = service.media().upload(
                    parent=space_name,
                    body={'filename': fname},
                    media_body=media
                ).execute()
                upload_token = upload_result['attachmentDataRef']['attachmentUploadToken']

                attachments.append({
                    'contentName': fname,
                    'contentType': mime_type,
                    'attachmentDataRef': {'attachmentUploadToken': upload_token}
                })

            update_fields.append('attachment')
            body['attachment'] = attachments

        if not update_fields:
            raise Exception("At least one of 'text' or 'file_paths' must be provided")

        result = service.spaces().messages().patch(
            name=message_name,
            updateMask=','.join(update_fields),
            body=body,
        ).execute()

        for tf in temp_files:
            try:
                os.unlink(tf)
            except OSError:
                pass

        return {
            'name': result.get('name'),
            'createTime': result.get('createTime'),
            'lastUpdateTime': result.get('lastUpdateTime'),
            'text': result.get('text'),
            'thread': result.get('thread'),
        }
    except Exception as e:
        for tf in temp_files:
            try:
                os.unlink(tf)
            except OSError:
                pass
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

        service = _get_service('chat', 'v1', creds)
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

        service = _get_service('chat', 'v1', creds)
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


async def find_direct_message(user_id: str) -> Dict:
    """Find an existing DM space with a specific user.

    Args:
        user_id: The user resource name to find a DM with (format: 'users/USER_ID')

    Returns:
        Space object if a DM exists, or empty dict if no DM found
    """
    try:
        creds = get_credentials()
        if not creds:
            raise Exception("No valid credentials found. Please authenticate first.")

        service = _get_service('chat', 'v1', creds)
        result = service.spaces().findDirectMessage(name=user_id).execute()
        return result
    except Exception as e:
        error_str = str(e)
        # 404 means no DM exists — return empty dict instead of raising
        if '404' in error_str or 'NOT_FOUND' in error_str:
            return {}
        raise Exception(f"Failed to find direct message: {error_str}")


async def delete_reaction(reaction_name: str) -> Dict:
    """Remove a reaction from a Google Chat message.

    Args:
        reaction_name: The full reaction resource name
                      (format: 'spaces/SPACE_ID/messages/MESSAGE_ID/reactions/REACTION_ID',
                       obtained from list_reactions)

    Returns:
        Confirmation dict
    """
    try:
        creds = get_credentials()
        if not creds:
            raise Exception("No valid credentials found. Please authenticate first.")

        service = _get_service('chat', 'v1', creds)
        service.spaces().messages().reactions().delete(name=reaction_name).execute()
        return {'deleted': reaction_name, 'success': True}
    except Exception as e:
        raise Exception(f"Failed to delete reaction: {str(e)}")


async def download_attachment(resource_name: str, save_dir: str = '/tmp', content_name: Optional[str] = None) -> Dict:
    """Download a file attachment from a Google Chat message.

    Uses the Chat API media endpoint with the attachment's resourceName
    (base64-encoded, from attachmentDataRef).

    Args:
        resource_name: The resourceName from attachmentDataRef (base64 string)
        save_dir: Directory to save the downloaded file (default: /tmp)
        content_name: Original filename from attachment metadata (e.g. 'image.png').
                     Used as fallback for file extension when API returns generic content type.

    Returns:
        Dict with path, contentName, contentType, and size
    """
    try:
        creds = get_credentials()
        if not creds:
            raise Exception("No valid credentials found. Please authenticate first.")

        token = creds.token
        encoded_name = urllib.parse.quote(resource_name, safe='')
        url = f"https://chat.googleapis.com/v1/media/{encoded_name}?alt=media"

        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        resp = urllib.request.urlopen(req)

        content_type = resp.headers.get('Content-Type', 'application/octet-stream')
        data = resp.read()

        # Determine file extension: prefer content type, fallback to content_name
        ext_map = {
            'image/png': '.png', 'image/jpeg': '.jpg', 'image/gif': '.gif',
            'image/webp': '.webp', 'application/pdf': '.pdf',
            'text/plain': '.txt', 'application/json': '.json',
        }
        ext = ext_map.get(content_type)
        if not ext and content_name:
            _, ext = os.path.splitext(content_name)
        if not ext:
            ext = '.bin'
        filename = f"gchat-{uuid.uuid4().hex[:8]}{ext}"
        filepath = os.path.join(save_dir, filename)

        os.makedirs(save_dir, exist_ok=True)
        with open(filepath, 'wb') as f:
            f.write(data)

        return {
            'path': filepath,
            'contentName': content_name or filename,
            'contentType': content_type,
            'size': len(data),
        }
    except Exception as e:
        raise Exception(f"Failed to download attachment: {str(e)}")

