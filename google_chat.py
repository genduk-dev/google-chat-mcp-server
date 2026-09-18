import os
import asyncio
import concurrent.futures
import threading
# Google may return additional scopes previously granted (include_granted_scopes),
# so relax the strict scope-match check oauthlib otherwise enforces.
os.environ['OAUTHLIB_RELAX_TOKEN_SCOPE'] = '1'

import logging
import datetime
import json
import re
import uuid
import urllib.parse
import urllib.request
import urllib.error
from typing import List, Dict, Optional, Tuple
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import AuthorizedSession, Request
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
    'https://www.googleapis.com/auth/chat.users.readstate',
    'https://www.googleapis.com/auth/chat.spaces.pins',
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
def _parse_bot_name(raw: str) -> str:
    """Lowercase BOT_NAME and check it fits a Google Chat custom message ID.

    The ID is 'client-{name}-' plus 12 hex chars; Google allows only lowercase
    letters, digits and hyphens, up to 63 characters in total.
    """
    name = raw.strip().lower()
    if not re.fullmatch(r'[a-z0-9-]{1,43}', name):
        raise ValueError(
            f"BOT_NAME {raw!r} must be 1-43 letters, digits or hyphens to form a valid "
            "Google Chat message ID")
    return name


# One name identifies the bot twice: the clientAssignedMessageId prefix that
# marks messages this server sent, and the @mention the channel listens for.
BOT_NAME = _parse_bot_name(os.environ.get('BOT_NAME', 'gchat-mcp'))
# The name as written, for display. BOT_NAME is its lowercased ID form.
BOT_DISPLAY_NAME = os.environ.get('BOT_NAME', 'gchat-mcp').strip()
APP_MESSAGE_PREFIX = f'client-{BOT_NAME}-'

# Holds the in-progress OAuth flow between a start_authentication() and
# complete_authentication() call, since they happen as two separate tool calls.
_pending_auth_flow: Optional[InstalledAppFlow] = None

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


# Google Chat markup in formattedText: labelled links, user mentions, custom
# emoji. Anything else in angle brackets is literal text and stays as it is.
_CHAT_MARKUP = re.compile(r'<(https?://[^|>]+)\|([^>]*)>|<(users/[^>]+)>|<customEmojis/(:[^>]+:)>')


def _mention_names(msg: Dict) -> Dict[str, str]:
    """Map users/ID to the @Name text each USER_MENTION annotation covers.

    The annotated span of 'text' is what the sender saw, so it is used over the
    annotation's displayName, which is missing for some users and reads
    "Deleted User" for removed accounts. Annotation offsets count UTF-16 code
    units, so an emoji before a mention would shift a str slice.
    """
    utf16 = (msg.get('text') or '').encode('utf-16-le')
    names = {}
    for a in msg.get('annotations', []):
        if a.get('type') != 'USER_MENTION':
            continue
        user = a.get('userMention', {}).get('user', {})
        # An @all mention carries an empty user.
        key = user.get('name') or 'users/all'
        start, length = a.get('startIndex', 0), a.get('length', 0)
        span = utf16[start * 2:(start + length) * 2].decode('utf-16-le', 'replace')
        if span.startswith('@'):
            names[key] = span
        elif user.get('displayName'):
            names[key] = f"@{user['displayName']}"
    return names


def message_text(msg: Dict) -> str:
    """Render a message's formattedText as readable markdown-ish text.

    Google's plain 'text' drops every link target and turns custom emoji into
    a replacement character; formattedText keeps them as Chat markup:
    <url|label> becomes [label](url), <users/ID> becomes the @Name written in
    the message (see _mention_names), including <users/all> (@all, @semua),
    and <customEmojis/:name:> becomes :name:. Chat's own *bold*, _italic_ and
    code markers are kept.
    """
    formatted = msg.get('formattedText')
    if not formatted:
        return msg.get('text') or ''
    names = _mention_names(msg)

    def render(m: re.Match) -> str:
        url, label, user, emoji = m.groups()
        if url:
            return f'[{label or url}]({url})'
        if user:
            # users/all is written in the sender's language, e.g. @semua.
            return names.get(user) or ('@all' if user == 'users/all' else f'@{user}')
        return emoji

    return _CHAT_MARKUP.sub(render, formatted)


# Code spans and fences are left alone so markdown inside them stays literal.
_CODE = re.compile(r'```.*?```|`[^`\n]*`', re.DOTALL)
_MD_LINK = re.compile(r'\[([^\]\n]+)\]\((https?://[^)\s]+)\)')
_MD_BOLD = re.compile(r'\*\*(?=\S)(.+?)(?<=\S)\*\*')
_MD_STRIKE = re.compile(r'~~(?=\S)(.+?)(?<=\S)~~')


def to_chat_markup(text: str) -> str:
    """Convert the common Markdown an agent writes into Google Chat markup.

    Chat shows [label](url) and **bold** literally. Read tools render links as
    [label](url), so an agent tends to write them back that way. This turns
    [label](url) into <url|label>, **bold** into *bold* and ~~strike~~ into
    ~strike~, outside code spans. Chat syntax that is already correct passes
    through unchanged.
    """
    def convert(chunk: str) -> str:
        chunk = _MD_LINK.sub(lambda m: f'<{m.group(2)}|{m.group(1)}>', chunk)
        chunk = _MD_BOLD.sub(r'*\1*', chunk)
        return _MD_STRIKE.sub(r'~\1~', chunk)

    out, last = [], 0
    for code in _CODE.finditer(text):
        out.append(convert(text[last:code.start()]))
        out.append(code.group(0))
        last = code.end()
    out.append(convert(text[last:]))
    return ''.join(out)


def _attachment_fields(a: Dict) -> Dict:
    fields = {'contentName': a.get('contentName'), 'contentType': a.get('contentType'),
              'resourceName': a.get('attachmentDataRef', {}).get('resourceName')}
    # Drive attachments have no downloadable resourceName; the file ID lets a
    # Drive tool open them.
    drive_id = a.get('driveDataRef', {}).get('driveFileId')
    if drive_id:
        fields['driveFileId'] = drive_id
    return fields


def _sender_fields(msg: Dict, creds: Credentials) -> Dict:
    """Sender fields for filtered output.

    Messages this server sent go out as the authenticated user, so Google
    reports that user as a HUMAN sender. They are attributed to the bot here so
    a reader can tell the bot's turns from the user's. --raw-messages keeps
    Google's own sender.
    """
    client_msg_id = msg.get('clientAssignedMessageId', '')
    if client_msg_id.startswith(APP_MESSAGE_PREFIX):
        return {'sender': BOT_DISPLAY_NAME, 'sender_type': 'BOT', 'sent_by_app': True}
    sender = msg.get('sender', {})
    return {
        'sender': get_user_display_name(sender, creds) if sender else 'Unknown',
        'sender_type': sender.get('type', 'HUMAN'),
        'sent_by_app': False,
    }


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

def _short_time(ts: Optional[str]) -> Optional[str]:
    """'2026-09-18T09:24:19.953311Z' -> '2026-09-18T09:24:19Z'."""
    return ts.split('.')[0].rstrip('Z') + 'Z' if ts else ts


def _compact_message(msg: Dict, creds: Credentials, space_name: str) -> Dict:
    """One message for grouped output: fields that are empty or implied by the group are left out."""
    prefix = f"{space_name}/messages/"
    out = {'id': msg.get('name', '').removeprefix(prefix)}
    # A day-filtered group can start mid-thread, so the root is marked, not inferred.
    if not msg.get('threadReply'):
        out['root'] = True
    fields = _sender_fields(msg, creds)
    out['sender'] = fields['sender']
    if fields['sender_type'] != 'HUMAN':
        out['sender_type'] = fields['sender_type']
    if fields['sent_by_app']:
        out['sent_by_app'] = True
    out['time'] = _short_time(msg.get('createTime'))
    if msg.get('lastUpdateTime'):
        out['edited'] = _short_time(msg['lastUpdateTime'])
    out['text'] = message_text(msg)
    quoted = msg.get('quotedMessageMetadata', {}).get('name')
    if quoted:
        out['quoted'] = quoted.removeprefix(prefix)
    if msg.get('attachment'):
        out['attachment'] = [_attachment_fields(a) for a in msg['attachment']]
    if msg.get('emojiReactionSummaries'):
        out['reactions'] = {}
        for r in msg['emojiReactionSummaries']:
            emoji = r.get('emoji', {})
            custom = emoji.get('customEmoji', {})
            key = emoji.get('unicode') or custom.get('emojiName') or custom.get('uid') or '?'
            out['reactions'][key] = out['reactions'].get(key, 0) + r.get('reactionCount', 0)
    return out


async def list_space_messages(space_name: str,
                              start_date: Optional[datetime.datetime] = None,
                              end_date: Optional[datetime.datetime] = None,
                              thread_name: Optional[str] = None,
                              limit: Optional[int] = None,
                              after: Optional[str] = None):
    """Lists messages from a Google Chat space, filtered by time and/or thread.

    Args:
        space_name: The space to fetch messages from ('spaces/SPACE_ID')
        start_date: Optional start datetime. Without end_date, covers that whole day
        end_date: Optional end datetime, only used with start_date
        thread_name: Optional 'spaces/SPACE_ID/threads/THREAD_ID'; Google filters server-side,
                     so an old thread costs one request however many messages came after it
        limit: Optional number of most recent matching messages to return (1-1000)
        after: Optional RFC 3339 time; only messages created after it

    Returns:
        With filtering on: {'space', 'threads': [{'thread', 'messages': [...]}], 'more'?, 'truncated'?},
        where 'more' means messages beyond limit matched and 'truncated' means the
        1000-message cap cut a result without a limit short;
        threads ordered by their first returned message, messages oldest first.
        With --raw-messages: the raw message list.

    Raises:
        Exception: If authentication fails or API request fails
    """
    if thread_name and not thread_name.startswith(f"{space_name}/threads/"):
        raise ValueError(f"thread_name must be a thread of {space_name} ('{space_name}/threads/THREAD_ID')")
    if limit is not None and not 1 <= limit <= MAX_MESSAGES:
        raise ValueError(f"limit must be between 1 and {MAX_MESSAGES}")
    try:
        creds = get_credentials()
        if not creds:
            raise Exception("No valid credentials found. Please authenticate first.")

        service = _get_service('chat', 'v1', creds)

        filters = []
        if start_date:
            if end_date:
                filters.append(f"createTime > \"{start_date.isoformat()}\" AND createTime < \"{end_date.isoformat()}\"")
            else:
                day_start = start_date.replace(hour=0, minute=0, second=0, microsecond=0)
                day_end = day_start + datetime.timedelta(days=1)
                filters.append(f"createTime > \"{day_start.isoformat()}\" AND createTime < \"{day_end.isoformat()}\"")
        if thread_name:
            filters.append(f"thread.name = {thread_name}")
        if after:
            filters.append(f'createTime > "{after}"')

        # With a limit, read newest first so the API stops after the last N.
        wanted = limit or MAX_MESSAGES
        messages = []
        page_token = None
        truncated = more = False
        while True:
            list_args = {'parent': space_name, 'pageSize': min(wanted - len(messages), 1000)}
            if filters:
                list_args['filter'] = ' AND '.join(filters)
            if limit:
                list_args['orderBy'] = 'createTime DESC'
            if page_token:
                list_args['pageToken'] = page_token
            response = service.spaces().messages().list(**list_args).execute()
            messages.extend(response.get('messages', []))
            page_token = response.get('nextPageToken')
            if not page_token:
                break
            if len(messages) >= wanted:
                # Hitting a caller's limit is expected; hitting the cap is not.
                more = bool(limit)
                truncated = not limit
                break
        if limit:
            messages.reverse()

        if not FILTER_MESSAGES:
            return messages

        prefetch_space_members(space_name, creds)

        threads: Dict[str, List[Dict]] = {}
        for msg in messages:
            key = msg.get('thread', {}).get('name', '')
            threads.setdefault(key, []).append(_compact_message(msg, creds, space_name))

        result = {'space': space_name,
                  'threads': [{'thread': t, 'messages': msgs} for t, msgs in threads.items()]}
        if more:
            result['more'] = True
        if truncated:
            result['truncated'] = True
        return result

    except ValueError:
        raise
    except Exception as e:
        raise Exception(f"Failed to list messages in space: {str(e)}")


_self_id_cache: Dict[str, str] = {}


def self_user_id(creds: Credentials) -> str:
    """The authenticated user as a Chat 'users/ID' name."""
    if 'id' not in _self_id_cache:
        person = _get_service('people', 'v1', creds).people().get(
            resourceName='people/me', personFields='names').execute()
        _self_id_cache['id'] = person['resourceName'].replace('people/', 'users/')
    return _self_id_cache['id']


CHAT_API = 'https://chat.googleapis.com/v1'
UNREAD_WORKERS = 8
_thread_local = threading.local()


def _parse_time(ts: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(ts.replace('Z', '+00:00'))


def _http(creds: Credentials) -> AuthorizedSession:
    """One HTTP session per worker thread; the cached googleapiclient services
    use httplib2, which is not thread-safe."""
    if getattr(_thread_local, 'session', None) is None:
        _thread_local.session = AuthorizedSession(creds)
    return _thread_local.session


def _space_unread(creds: Credentials, space: Dict, self_id: str) -> Optional[Dict]:
    """Unread summary for one space, or None when everything is read."""
    http = _http(creds)
    resp = http.get(f"{CHAT_API}/users/me/{space['name']}/spaceReadState")
    resp.raise_for_status()
    last_read = resp.json().get('lastReadTime')
    if last_read and _parse_time(space['lastActiveTime']) <= _parse_time(last_read):
        return None
    params = {'pageSize': 100}
    if last_read:
        params['filter'] = f'createTime > "{last_read}"'
    resp = http.get(f"{CHAT_API}/{space['name']}/messages", params=params)
    resp.raise_for_status()
    page = resp.json()
    # Your own messages after the read marker are not unread for you.
    others = [m for m in page.get('messages', []) if m.get('sender', {}).get('name') != self_id]
    if not others:
        return None
    name = space.get('displayName')
    if not name:
        # DMs and group chats have no display name; show who wrote the unread messages.
        senders = []
        for m in others:
            sender = m.get('sender', {}).get('displayName')
            if sender and sender not in senders:
                senders.append(sender)
        name = ', '.join(senders[:3]) + (' and others' if len(senders) > 3 else '')
    count = len(others)
    return {'space': space['name'], 'name': name, 'type': space.get('spaceType'),
            'unread': f'{count}+' if page.get('nextPageToken') else count,
            'last_read': _short_time(last_read), 'latest': _short_time(space['lastActiveTime'])}


async def list_unread_spaces(days: int = 1) -> Dict:
    """Spaces with messages you have not read, among those active in the last `days` days.

    Returns:
        {'since', 'checked', 'spaces': [{'space', 'name', 'type', 'unread', 'last_read', 'latest'}]},
        most recently active first. 'unread' is capped as '100+'.
    """
    if not 1 <= days <= 90:
        raise ValueError("days must be between 1 and 90")
    creds = get_credentials()
    if not creds:
        raise Exception("No valid credentials found. Please authenticate first.")
    self_id = self_user_id(creds)
    service = _get_service('chat', 'v1', creds)
    spaces, page_token = [], None
    while True:
        response = service.spaces().list(pageSize=1000, **({'pageToken': page_token} if page_token else {})).execute()
        spaces.extend(response.get('spaces', []))
        page_token = response.get('nextPageToken')
        if not page_token:
            break
    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    active = sorted((s for s in spaces if s.get('lastActiveTime') and _parse_time(s['lastActiveTime']) >= since),
                    key=lambda s: s['lastActiveTime'], reverse=True)

    def check(space):
        return _space_unread(creds, space, self_id)

    with concurrent.futures.ThreadPoolExecutor(max_workers=UNREAD_WORKERS) as pool:
        results = await asyncio.get_running_loop().run_in_executor(None, lambda: list(pool.map(check, active)))
    return {'since': _short_time(since.isoformat().replace('+00:00', 'Z')), 'checked': len(active),
            'spaces': [r for r in results if r]}


async def get_unread_messages(space_name: str, limit: int = 50):
    """Messages in a space created after your read marker, newest `limit`, in get_messages' format."""
    creds = get_credentials()
    if not creds:
        raise Exception("No valid credentials found. Please authenticate first.")
    state = _get_service('chat', 'v1', creds).users().spaces().getSpaceReadState(
        name=f"users/me/{space_name}/spaceReadState").execute()
    last_read = state.get('lastReadTime')
    result = await list_space_messages(space_name, limit=limit, after=last_read)
    if isinstance(result, dict):
        result['last_read'] = _short_time(last_read)
    return result


async def mark_space_read(space_name: str) -> Dict:
    """Move your read marker in a space to now."""
    creds = get_credentials()
    if not creds:
        raise Exception("No valid credentials found. Please authenticate first.")
    now = datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00', 'Z')
    state = _get_service('chat', 'v1', creds).users().spaces().updateSpaceReadState(
        name=f"users/me/{space_name}/spaceReadState", updateMask='lastReadTime',
        body={'lastReadTime': now}).execute()
    return {'space': space_name, 'last_read': _short_time(state.get('lastReadTime'))}


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

    query is passed through verbatim as the API's filter string — it is not escaped
    or wrapped. The API rejects a literal `"` inside it and rejects a bare, standalone
    `OR` token (the boolean operator) between terms — both are validated locally
    before any request is made. Lowercase `or` and "or" inside a word (e.g. "order",
    "sponsor") are ordinary text and are NOT rejected. space_name is also validated
    locally for a literal `"`, since it is interpolated into the same filter string.

    Args:
        query: Free-text search string, passed through as the API filter
        space_name: Optional 'spaces/XXXX' to scope the search; None searches
                    every accessible space
        limit: Max messages in this page of results (clamped to 1..MAX_MESSAGES)
        page_token: Opaque continuation token from a previous call's nextPageToken

    Returns:
        {'messages': [...], 'nextPageToken': str or None}

    Raises:
        Exception: If authentication fails, preview access is unavailable, the
                   API request fails, or query/space_name are invalid
    """
    creds = get_credentials()
    if not creds:
        raise Exception("No valid credentials found. Please authenticate first.")

    if not query or not query.strip():
        raise Exception("Failed to search messages: query cannot be empty")
    if '"' in query:
        raise Exception(
            'Failed to search messages: query cannot contain a literal " character '
            "(it is interpolated unescaped into the API filter string, which rejects "
            "unescaped quotes)"
        )
    if re.search(r'\bOR\b', query):
        raise Exception(
            "Failed to search messages: query cannot contain a standalone OR token "
            "(the API treats bare OR as its boolean operator and rejects it; "
            "lowercase 'or' or 'or' inside a word is fine)"
        )
    if space_name is not None and not (space_name.startswith("spaces/") and len(space_name) > len("spaces/")):
        raise Exception(
            f"Failed to search messages: space_name {space_name!r} must look like "
            "'spaces/<id>'"
        )
    if space_name is not None and '"' in space_name:
        raise Exception(
            'Failed to search messages: space_name cannot contain a literal " character '
            "(it is interpolated unescaped into the API filter string)"
        )

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
            page_entries = [entry['message'] for entry in response.get('results', []) if 'message' in entry]
            results.extend(page_entries)
            next_token = response.get('nextPageToken')
            if not next_token or not page_entries:
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
        name = msg.get('name', '')
        space = msg.get('space', {}).get('name') or '/'.join(name.split('/')[:2])
        filtered_messages.append({
            'name': name,
            'space': space,
            **_sender_fields(msg, creds),
            'createTime': msg.get('createTime'),
            'text': message_text(msg),
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

        body = {'text': to_chat_markup(text)}
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

        result = {
            'name': msg.get('name'),
            **_sender_fields(msg, creds),
            'createTime': msg.get('createTime'),
            'lastUpdateTime': msg.get('lastUpdateTime'),
            'text': message_text(msg),
            'thread': msg.get('thread'),
        }
        if msg.get('quotedMessageMetadata'):
            result['quotedMessageMetadata'] = msg['quotedMessageMetadata']
        result['threadReply'] = msg.get('threadReply', False)
        if msg.get('attachment'):
            result['attachment'] = [_attachment_fields(a) for a in msg['attachment']]
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
            body['text'] = to_chat_markup(text)

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


def start_authentication(credentials_path: Optional[str] = None) -> str:
    """Starts an OAuth authentication flow and returns the authorization URL.

    The user should open the URL, complete authorization, then pass the resulting
    callback URL to complete_authentication() to finish the flow.

    Args:
        credentials_path: Path to the OAuth client credentials.json file. If None, uses
                          credentials.json next to the configured token file, since an MCP
                          client usually launches the server from an unrelated directory.

    Returns:
        The authorization URL for the user to open in a browser

    Raises:
        Exception: If credentials.json is missing or the flow can't be created
    """
    global _pending_auth_flow

    if credentials_path is None:
        credentials_path = str(Path(token_info['token_path']).parent / 'credentials.json')
    creds_file = Path(credentials_path)
    if not creds_file.exists():
        raise Exception(
            f"{credentials_path} not found. Download it from Google Cloud Console "
            "and save it in the current directory."
        )

    flow = InstalledAppFlow.from_client_secrets_file(
        str(creds_file),
        SCOPES,
        redirect_uri=DEFAULT_CALLBACK_URL
    )

    auth_url, _ = flow.authorization_url(
        access_type='offline',
        prompt='consent',
        include_granted_scopes='true'
    )

    _pending_auth_flow = flow
    return auth_url


def complete_authentication(callback_url: str, token_path: Optional[str] = None) -> Dict:
    """Completes an OAuth flow started by start_authentication() using the callback URL.

    Args:
        callback_url: The full callback URL from the browser address bar after authorizing
                      (e.g. 'http://localhost:8000/auth/callback?code=...&scope=...'), or
                      just the bare authorization code
        token_path: Optional path to save the token to. If None, uses the configured path.

    Returns:
        A dict with authentication status details

    Raises:
        Exception: If no authentication flow is in progress, the callback has no code,
                   or the code exchange fails
    """
    global _pending_auth_flow

    if _pending_auth_flow is None:
        raise Exception(
            "No authentication flow in progress. Call start_authentication() first."
        )

    from urllib.parse import urlparse, parse_qs

    if callback_url.startswith('http'):
        parsed = urlparse(callback_url)
        params = parse_qs(parsed.query)

        if 'error' in params:
            raise Exception(f"Authorization failed: {params['error'][0]}")

        if 'code' not in params:
            raise Exception("No authorization code found in the callback URL.")

        code = params['code'][0]
    else:
        code = callback_url

    try:
        flow = _pending_auth_flow
        flow.fetch_token(code=code)
        creds = flow.credentials

        save_credentials(creds, token_path)

        return {
            'authenticated': True,
            'has_refresh_token': bool(creds.refresh_token),
            'expiry': creds.expiry.isoformat() if creds.expiry else None,
        }
    finally:
        _pending_auth_flow = None

