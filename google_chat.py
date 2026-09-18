import os
import asyncio
import concurrent.futures
import http.server
import threading
import time
# Google may return additional scopes previously granted (include_granted_scopes),
# so relax the strict scope-match check oauthlib otherwise enforces.
os.environ['OAUTHLIB_RELAX_TOKEN_SCOPE'] = '1'

import logging
import datetime
import json
import random
import re
import uuid
import urllib.parse
import urllib.request
from typing import List, Dict, Optional, Tuple
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import AuthorizedSession, Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import HttpRequest
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
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
    'https://www.googleapis.com/auth/chat.customemojis.readonly',
    'https://www.googleapis.com/auth/chat.spaces.create',
    'https://www.googleapis.com/auth/chat.users.spacesettings',
    'https://www.googleapis.com/auth/chat.users.availability',
]

# Cache for user display names: {user_id: display_name}
_user_display_name_cache: Dict[str, str] = {}

# Cached API service objects (keyed by credentials token)
_service_cache: Dict[str, object] = {}

# Transient failures are retried with backoff. A 429 means Google did not process
# the request, so any method may repeat it; a 5xx after a write may have taken
# effect already (a second send would post twice), so only reads repeat on 5xx.
# Google's per-space limit is about one write a second, so bursts do hit 429.
RETRY_ATTEMPTS = 3
RETRY_5XX = {500, 502, 503, 504}


def _should_retry(method: str, status: int) -> bool:
    return status == 429 or (status in RETRY_5XX and method.upper() == 'GET')


def _retry_delay(attempt: int, retry_after: Optional[str] = None) -> float:
    """Seconds before retry number attempt (0-based): Retry-After when sent, else 1, 2, 4 plus jitter."""
    if retry_after and retry_after.isdigit():
        return min(float(retry_after), 30.0)
    return 2 ** attempt + random.uniform(0, 0.5)


class _RetryingHttpRequest(HttpRequest):
    """googleapiclient request whose execute() retries transient failures."""

    def execute(self, http=None, num_retries=0):
        for attempt in range(RETRY_ATTEMPTS + 1):
            try:
                return super().execute(http=http, num_retries=num_retries)
            except HttpError as e:
                if attempt == RETRY_ATTEMPTS or not _should_retry(self.method, e.resp.status):
                    raise
                delay = _retry_delay(attempt, e.resp.get('retry-after'))
                logger.info("%s %s got %s; retrying in %.1fs", self.method, self.uri.split('?')[0], e.resp.status, delay)
                time.sleep(delay)


class _ChatRetry(Retry):
    """urllib3 retry policy for the raw-HTTP calls, with the same rules."""

    def is_retry(self, method, status_code, has_retry_after=False):
        return bool(self.total) and _should_retry(method, status_code)

    def get_backoff_time(self):
        return _retry_delay(len(self.history) - 1)

    def get_retry_after(self, response):
        # Same 30s cap as the googleapiclient path; a longer wait blocks the event loop.
        seconds = super().get_retry_after(response)
        return None if seconds is None else min(seconds, 30.0)

def _get_service(api: str, version: str, creds: Credentials) -> object:
    """Get or create a cached Google API service object."""
    cache_key = f"{api}:{version}:{creds.token}"
    if cache_key not in _service_cache:
        # Clear stale entries for same api:version with old tokens
        prefix = f"{api}:{version}:"
        stale = [k for k in _service_cache if k.startswith(prefix) and k != cache_key]
        for k in stale:
            del _service_cache[k]
        _service_cache[cache_key] = build(api, version, credentials=creds, requestBuilder=_RetryingHttpRequest)
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
_pending_auth_state: Optional[str] = None

# Store credentials info
token_info = {
    'credentials': None,
    'last_refresh': None,
    'token_path': DEFAULT_TOKEN_PATH,
    # mtime of the token file the in-memory credentials came from, or were saved to
    'mtime': None,
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

def write_private(path: Path, text: str) -> None:
    """Replace path atomically with owner-only permissions from the first byte.

    Several server processes share these files, so each writes its own temp
    file; a shared name would let one process move another's half-written file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _load_token_file(token_path: Path) -> Credentials:
    """Load a token with the scopes recorded in it, which are the ones granted.

    Passing SCOPES here would make a refresh ask for exactly SCOPES: a token
    granted fewer fails with invalid_scope, and one granted more is narrowed.
    """
    return Credentials.from_authorized_user_file(str(token_path))


def save_credentials(creds: Credentials, token_path: Optional[str] = None) -> None:
    """Save credentials to file and update in-memory cache.
    
    Args:
        creds: The credentials to save
        token_path: Path to save the token file
    """
    # Use configured token path if none provided
    if token_path is None:
        token_path = token_info['token_path']
    
    # Written atomically: other server processes reload this file whenever it changes.
    token_path = Path(token_path)
    write_private(token_path, creds.to_json())

    # Update in-memory cache
    token_info['credentials'] = creds
    token_info['mtime'] = token_path.stat().st_mtime_ns
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

    # Every server process (one per Claude Code session) shares the token file.
    # Reload it whenever it changed, so a re-login or another process's refresh
    # is picked up instead of being overwritten by this process's older token.
    token_path = Path(token_path)
    if token_path.exists():
        mtime = token_path.stat().st_mtime_ns
        if not creds or mtime != token_info['mtime']:
            creds = _load_token_file(token_path)
            token_info['credentials'] = creds
            token_info['mtime'] = mtime

    # If we have credentials that need refresh
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as e:
            logger.warning("Failed to refresh credentials: %s", e)
            return None
        try:
            save_credentials(creds, token_path)
        except OSError as e:
            # The refreshed token still works in this process; only sharing it failed.
            logger.warning("Refreshed credentials could not be saved to %s: %s", token_path, e)
    
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
            creds = _load_token_file(token_path)

        if not creds.refresh_token:
            return False, "No refresh token available"
        
        creds.refresh(Request())
        save_credentials(creds, token_path)
        return True, "Token refreshed successfully"
    except Exception as e:
        return False, f"Failed to refresh token: {str(e)}"

def prefetch_space_members(space_name: str, creds: Credentials) -> List[Dict]:
    """List all memberships of a space and cache their members' display names.

    First collects user IDs from Chat API memberships, then resolves names
    via People API directory lookup. Requires chat.memberships.readonly
    and directory.readonly scopes.

    Args:
        space_name: The space to fetch members from (format: 'spaces/SPACE_ID')
        creds: Valid credentials for API calls

    Returns:
        The raw memberships, so callers need not list them a second time.
        A failed membership listing raises; a failed name lookup only leaves IDs unnamed.
    """
    memberships = []
    # Step 1: Get all member user IDs from Chat API
    chat_service = _get_service('chat', 'v1', creds)
    user_ids = []
    page_token = None
    while True:
        list_args = {'parent': space_name, 'pageSize': 100}
        if page_token:
            list_args['pageToken'] = page_token
        response = chat_service.spaces().members().list(**list_args).execute()
        memberships.extend(response.get('memberships', []))
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
    return memberships


# Names the user gave for people Google cannot name (deleted or hidden
# accounts), as {users/ID: name}. Shared by every server process, so it is
# reread whenever the file changes.
_user_names: Dict[str, str] = {}
_user_names_mtime: Optional[int] = None


def _user_names_path() -> Path:
    return Path(token_info['token_path']).parent / 'user_names.json'


def user_names() -> Dict[str, str]:
    global _user_names, _user_names_mtime
    path = _user_names_path()
    mtime = path.stat().st_mtime_ns if path.exists() else None
    if mtime != _user_names_mtime:
        new = json.loads(path.read_text()) if mtime is not None else {}
        # Forget names worked out under the old mapping: saved ones that changed
        # or went away, and IDs that stood in for a name that may now exist.
        stale = set(_user_names) | set(new) | {u for u, n in _user_display_name_cache.items() if n == u}
        for user_id in stale:
            _user_display_name_cache.pop(user_id, None)
        _user_names, _user_names_mtime = new, mtime
    return _user_names


def set_user_name(user_id: str, name: str) -> Dict:
    """Save (or, with an empty name, remove) the name shown for a user Google cannot name."""
    if not re.fullmatch(r'users/[0-9]+', user_id):
        raise ValueError(f"Expected 'users/NUMERIC_ID', got {user_id!r}")
    names = dict(user_names())
    name = name.strip()
    if name:
        names[user_id] = name
    else:
        names.pop(user_id, None)
    write_private(_user_names_path(), json.dumps(names, indent=2, ensure_ascii=False))
    _user_display_name_cache.pop(user_id, None)
    return {'user_id': user_id, 'name': name or None, 'saved_names': len(names)}


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
    saved = user_names()

    # Check if already cached (from prefetch_space_members)
    if user_id in _user_display_name_cache:
        return _user_display_name_cache[user_id]

    # If Chat API already provided displayName, use it
    if sender.get('displayName'):
        _user_display_name_cache[user_id] = sender['displayName']
        return sender['displayName']

    # A name the user gave for someone Google cannot name.
    if user_id in saved:
        _user_display_name_cache[user_id] = saved[user_id]
        return saved[user_id]

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
        except HttpError as e:
            if e.resp.status not in (403, 404):
                # Transient (retries ran out, or a 5xx): try again on the next lookup.
                logger.warning("Name lookup for %s failed with %s", user_id, e.resp.status)
                return user_id
        except Exception as e:
            logger.warning("Name lookup for %s failed: %s", user_id, e)
            return user_id

    # No name to be had: a deleted account (404), one we may not see (403), or an
    # external person whose profile has no visible name. That will not change, so
    # the ID stands in for the name for the rest of this process.
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
# The URL may hold one level of balanced parentheses, as Wikipedia links do.
_MD_LINK = re.compile(r'\[([^\]\n]+)\]\((https?://(?:[^()\s]|\([^()\s]*\))+)\)')
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


def _member_fields(membership: Dict) -> Dict:
    """A membership as get_members reports it. Names come from the cache prefetch fills."""
    member = membership.get('member', {})
    user_id = member.get('name', '')
    saved = user_names()  # rereads a mapping another session changed, dropping stale cache entries
    return {
        'user_id': user_id,
        'display_name': (_user_display_name_cache.get(user_id) or member.get('displayName')
                         or saved.get(user_id) or user_id),
        'mention': f'<{user_id}>',
        'type': member.get('type', 'HUMAN'),
        'role': membership.get('role', 'ROLE_MEMBER'),
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

        members = []
        for membership in prefetch_space_members(space_name, creds):
            member = membership.get('member', {})
            user_id = member.get('name', '')
            if not user_id:
                continue
            members.append(_member_fields(membership))
        return members
    except Exception as e:
        raise Exception(f"Failed to list space members: {str(e)}")


# MCP functions
SPACE_TYPES = {'SPACE', 'GROUP_CHAT', 'DIRECT_MESSAGE'}

# Space -> its link in the Chat web app, from spaceUri. Only Google knows
# whether a space opens under /room/ or /dm/, and messages do not say.
_space_links: Dict[str, str] = {}
_space_names: Dict[str, str] = {}


def _link(space: Dict) -> str:
    """https://chat.google.com/room/ID or /dm/ID, without the ?cls= client hint."""
    uri = space.get('spaceUri') or f"https://chat.google.com/room/{space['name'].removeprefix('spaces/')}"
    link = uri.split('?')[0]
    _space_links[space['name']] = link
    _space_names[space['name']] = space.get('displayName', '')
    return link


def space_link(space_name: str, creds: Credentials) -> str:
    if space_name not in _space_links:
        _space_links[space_name] = _link(_get_service('chat', 'v1', creds).spaces().get(name=space_name).execute())
    return _space_links[space_name]


def space_display_name(space_name: str, creds: Credentials) -> str:
    """A space's display name from the same cached spaces.get as its link; '' for a DM."""
    if space_name not in _space_names:
        _link(_get_service('chat', 'v1', creds).spaces().get(name=space_name).execute())
    return _space_names[space_name]


def _cache_space_links(space_names, creds: Credentials) -> None:
    """Fill the link cache for many spaces with one spaces.list instead of a get each."""
    if len(set(space_names) - set(_space_links)) <= 1:
        return  # space_link fetches a single missing one itself
    service = _get_service('chat', 'v1', creds)
    page_token = None
    while True:
        response = service.spaces().list(pageSize=1000, **({'pageToken': page_token} if page_token else {})).execute()
        for space in response.get('spaces', []):
            _link(space)
        page_token = response.get('nextPageToken')
        if not page_token:
            break


def message_link(message_name: str, creds: Credentials) -> str:
    """spaces/S/messages/T.M -> {space link}/T/M, as the Chat app links a message."""
    space_name, _, message_id = message_name.partition('/messages/')
    return f"{space_link(space_name, creds)}/{message_id.replace('.', '/', 1)}"


async def list_chat_spaces(query: Optional[str] = None, space_type: Optional[str] = None,
                           limit: int = 100) -> Dict:
    """The user's spaces, most recently active first, one short entry each.

    Returns:
        {'total': spaces matching, 'spaces': [{'space', 'name'?, 'type', 'last_active'?}]}.
        DMs and group chats have no name.
    """
    if space_type is not None and space_type not in SPACE_TYPES:
        raise ValueError(f"space_type must be one of {sorted(SPACE_TYPES)}")
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    creds = get_credentials()
    if not creds:
        raise Exception("No valid credentials found. Please authenticate first.")
    service = _get_service('chat', 'v1', creds)
    spaces, page_token = [], None
    while True:
        list_args = {'pageSize': 1000}
        if space_type:
            list_args['filter'] = f'spaceType = "{space_type}"'
        if page_token:
            list_args['pageToken'] = page_token
        response = service.spaces().list(**list_args).execute()
        spaces.extend(response.get('spaces', []))
        page_token = response.get('nextPageToken')
        if not page_token:
            break
    if query:
        needle = query.casefold()
        spaces = [sp for sp in spaces if needle in sp.get('displayName', '').casefold()]
    spaces.sort(key=lambda sp: _last_active(sp) or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc),
                reverse=True)
    out = []
    for sp in spaces[:limit]:
        entry = {'space': sp['name']}
        if sp.get('displayName'):
            entry['name'] = sp['displayName']
        entry['type'] = sp.get('spaceType')
        if _last_active(sp):
            entry['last_active'] = _short_time(sp['lastActiveTime'])
        entry['link'] = _link(sp)
        out.append(entry)
    return {'total': len(spaces), 'spaces': out}


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

        # Senders' names come with the messages; members.list added a request per call
        # without naming anyone the messages did not already name.
        threads: Dict[str, List[Dict]] = {}
        for msg in messages:
            key = msg.get('thread', {}).get('name', '')
            threads.setdefault(key, []).append(_compact_message(msg, creds, space_name))

        link = space_link(space_name, creds)
        result = {'space': space_name, 'link': link,
                  'threads': [{'thread': t, 'link': f"{link}/{t.rsplit('/', 1)[-1]}", 'messages': msgs}
                              for t, msgs in threads.items()]}
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
    """The authenticated user as a Chat 'users/ID' name, cached per refresh token
    so a sign-in as another account is not answered with the old ID."""
    key = creds.refresh_token or creds.token
    if key not in _self_id_cache:
        person = _get_service('people', 'v1', creds).people().get(
            resourceName='people/me', personFields='names').execute()
        _self_id_cache.clear()
        _self_id_cache[key] = person['resourceName'].replace('people/', 'users/')
    return _self_id_cache[key]


CHAT_API = 'https://chat.googleapis.com/v1'
UNREAD_WORKERS = 8
UNREAD_MAX_PAGES = 10
_thread_local = threading.local()


def _parse_time(ts: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(ts.replace('Z', '+00:00'))


def _http(creds: Credentials) -> AuthorizedSession:
    """One HTTP session per worker thread; the cached googleapiclient services
    use httplib2, which is not thread-safe. A reloaded token gets a new session."""
    session = getattr(_thread_local, 'session', None)
    if session is None or session.credentials is not creds:
        _thread_local.session = session = AuthorizedSession(creds)
        # raise_on_status=False hands the last response back, so callers still
        # see Google's error after the retries run out.
        retry = _ChatRetry(total=RETRY_ATTEMPTS, connect=0, read=0, allowed_methods=None,
                           raise_on_status=False, respect_retry_after_header=True)
        session.mount('https://', HTTPAdapter(max_retries=retry))
    return session


def _last_active(space: Dict) -> Optional[datetime.datetime]:
    """A space's lastActiveTime, or None when unknown. The API reports the Unix
    epoch after the space's newest message is deleted."""
    ts = space.get('lastActiveTime')
    if not ts or ts.startswith('1970-01-01'):
        return None
    return _parse_time(ts)


def _space_unread(creds: Credentials, space: Dict, self_id: str) -> Optional[Dict]:
    """Unread summary for one space, or None when everything is read."""
    http = _http(creds)
    resp = http.get(f"{CHAT_API}/users/me/{space['name']}/spaceReadState")
    resp.raise_for_status()
    last_read = resp.json().get('lastReadTime')
    active = _last_active(space)
    if last_read and active and active <= _parse_time(last_read):
        return None
    params = {'pageSize': 100}
    if last_read:
        params['filter'] = f'createTime > "{last_read}"'
    # Your own messages after the read marker are not unread for you, so keep
    # paging until 100 others' messages are counted or the pages run out.
    others, pages = [], 0
    while True:
        resp = http.get(f"{CHAT_API}/{space['name']}/messages", params=params)
        resp.raise_for_status()
        page = resp.json()
        others.extend(m for m in page.get('messages', []) if m.get('sender', {}).get('name') != self_id)
        pages += 1
        if len(others) >= 100 or not page.get('nextPageToken') or pages == UNREAD_MAX_PAGES:
            break
        params['pageToken'] = page['nextPageToken']
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
    return {'space': space['name'], 'name': name, 'type': space.get('spaceType'), 'link': _link(space),
            'unread': f'{min(count, 100)}+' if count > 100 or page.get('nextPageToken') else count,
            'last_read': _short_time(last_read),
            'latest': _short_time(space['lastActiveTime']) if active else _short_time(others[-1].get('createTime'))}


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
    # Spaces with an unknown last activity are checked too, or their unread messages would hide.
    active = sorted((s for s in spaces if (_last_active(s) or since) >= since),
                    key=lambda s: s.get('lastActiveTime', ''), reverse=True)

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

            response = _chat_request(creds, 'POST', f"{parent}/messages:search", json=body)
            page_entries = [entry['message'] for entry in response.get('results', []) if 'message' in entry]
            results.extend(page_entries)
            next_token = response.get('nextPageToken')
            if not next_token or not page_entries:
                break
    except ChatApiError as e:
        detail = e.detail
        if e.status in (403, 404):
            raise Exception(
                "Failed to search messages: the spaces.messages.search method is in "
                "Google Workspace Developer Preview and this account/project appears "
                f"to have lost access (HTTP {e.status}). Other tools are unaffected. "
                f"API said: {detail}"
            )
        if e.status == 400:
            raise Exception(
                f"Failed to search messages: API rejected filter {filter_str!r} "
                f"(HTTP 400). API said: {detail}"
            )
        raise Exception(f"Failed to search messages: HTTP {e.status} {detail}")
    except Exception as e:
        raise Exception(f"Failed to search messages: {str(e)}")

    if not FILTER_MESSAGES:
        return {'messages': results, 'nextPageToken': next_token}

    filtered_messages = []
    _cache_space_links([m.get('name', '').partition('/messages/')[0] for m in results], creds)
    for msg in results:
        name = msg.get('name', '')
        space = msg.get('space', {}).get('name') or '/'.join(name.split('/')[:2])
        filtered_messages.append({
            'name': name,
            'space': space,
            'link': message_link(name, creds),
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


# Custom emoji ':name:' -> resource name (customEmojis/...), filled from
# customEmojis.list on first use. Reactions accept the resource name; the uid
# the same list returns is rejected as an invalid custom emoji.
_custom_emoji_uids: Dict[str, str] = {}
_CUSTOM_EMOJI = re.compile(r':[^:\s]+:')


def _list_custom_emojis(creds: Credentials) -> List[Dict]:
    service = _get_service('chat', 'v1', creds)
    emojis, page_token = [], None
    while True:
        response = service.customEmojis().list(pageSize=200, **({'pageToken': page_token} if page_token else {})).execute()
        emojis.extend(response.get('customEmojis', []))
        page_token = response.get('nextPageToken')
        if not page_token:
            break
    _custom_emoji_uids.update({e['emojiName']: e['name'] for e in emojis if e.get('emojiName')})
    return emojis


async def list_custom_emojis(query: Optional[str] = None) -> Dict:
    """The organization's custom emoji, as the ':name:' used to react with them."""
    creds = get_credentials()
    if not creds:
        raise Exception("No valid credentials found. Please authenticate first.")
    names = sorted(e['emojiName'] for e in _list_custom_emojis(creds) if e.get('emojiName'))
    if query:
        names = [n for n in names if query.casefold() in n.casefold()]
    return {'total': len(names), 'emojis': names}


def _reaction_emoji(emoji: str, creds: Credentials) -> Dict:
    """{'unicode': ...} or {'customEmoji': {'name': 'customEmojis/...'}} for ':name:'."""
    if not _CUSTOM_EMOJI.fullmatch(emoji):
        return {'unicode': emoji}
    if emoji not in _custom_emoji_uids:
        _list_custom_emojis(creds)  # a new emoji, or the first use in this process
    if emoji not in _custom_emoji_uids:
        raise ValueError(f"No custom emoji named {emoji}; see list_custom_emojis")
    return {'customEmoji': {'name': _custom_emoji_uids[emoji]}}


async def create_reaction(message_name: str, emoji: str) -> Dict:
    """Add an emoji reaction to a message.

    Args:
        message_name: The resource name of the message to react to
                     (format: 'spaces/SPACE_ID/messages/MESSAGE_ID')
        emoji: A Unicode emoji ('👍') or an organization custom emoji as ':name:'

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
            body={'emoji': _reaction_emoji(emoji, creds)},
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


class ChatApiError(Exception):
    def __init__(self, method: str, path: str, status: int, detail: str):
        super().__init__(f"{method} {path} failed ({status}): {detail}")
        self.status = status
        self.detail = detail


def _error_detail(resp) -> str:
    """Google's error message from a failed response. Most endpoints send
    {"error": {...}}; the media endpoint wraps it in a list."""
    try:
        body = resp.json()
        if isinstance(body, list):
            body = body[0]
        return body['error']['message']
    except (ValueError, KeyError, IndexError, TypeError):
        return resp.text[:500]


def _chat_request(creds: Credentials, method: str, path: str, **kwargs) -> Dict:
    """Call a Chat API v1 method that the installed discovery client lacks
    (messagePins, findGroupChats, messages:search). Raises ChatApiError with
    Google's own error message."""
    resp = _http(creds).request(method, f"{CHAT_API}/{path}", **kwargs)
    if not resp.ok:
        detail = _error_detail(resp)
        raise ChatApiError(method, path, resp.status_code, detail)
    return resp.json() if resp.content else {}


def _message_space(message_name: str) -> str:
    """'spaces/S/messages/M' -> 'spaces/S'."""
    parts = message_name.split('/')
    if len(parts) != 4 or parts[0] != 'spaces' or parts[2] != 'messages':
        raise ValueError(f"Expected 'spaces/SPACE_ID/messages/MESSAGE_ID', got {message_name!r}")
    return '/'.join(parts[:2])


async def list_pinned_messages(space_name: str) -> Dict:
    """The pinned messages of a space, each in get_messages' compact message format."""
    creds = get_credentials()
    if not creds:
        raise Exception("No valid credentials found. Please authenticate first.")
    pins, page_token = [], None
    while True:
        params = {'pageSize': 100, **({'pageToken': page_token} if page_token else {})}
        page = _chat_request(creds, 'GET', f"{space_name}/messagePins", params=params)
        pins.extend(page.get('messagePins', []))
        page_token = page.get('nextPageToken')
        if not page_token:
            break

    def fetch(pin):
        resp = _http(creds).get(f"{CHAT_API}/{pin['message']}")
        if resp.status_code in (403, 404):
            return {'name': pin['message'], 'error': resp.status_code}
        resp.raise_for_status()  # a transient failure is not a fact about access
        return resp.json()

    with concurrent.futures.ThreadPoolExecutor(max_workers=UNREAD_WORKERS) as pool:
        messages = await asyncio.get_running_loop().run_in_executor(None, lambda: list(pool.map(fetch, pins)))
    out = []
    for msg in messages:
        if 'error' in msg:
            # Pinned, but this user can no longer read it; say so instead of dropping it.
            out.append({'id': msg['name'].removeprefix(f"{space_name}/messages/"), 'unavailable': msg['error']})
        else:
            out.append({**_compact_message(msg, creds, space_name), 'link': message_link(msg['name'], creds)})
    return {'space': space_name, 'pins': out}


async def pin_message(message_name: str) -> Dict:
    """Pin a message in its space."""
    creds = get_credentials()
    if not creds:
        raise Exception("No valid credentials found. Please authenticate first.")
    pin = _chat_request(creds, 'POST', f"{_message_space(message_name)}/messagePins",
                        json={'message': message_name})
    return {'pin': pin.get('name'), 'message': pin.get('message', message_name)}


async def unpin_message(message_name: str) -> Dict:
    """Unpin a message. A pin's ID is its message's ID, so the message name is enough."""
    creds = get_credentials()
    if not creds:
        raise Exception("No valid credentials found. Please authenticate first.")
    space = _message_space(message_name)
    pin_name = f"{space}/messagePins/{message_name.split('/')[-1]}"
    _chat_request(creds, 'DELETE', pin_name)
    return {'unpinned': message_name}


# Short names for spaceEvents.list event types: 'message.updated' is
# 'google.workspace.chat.message.v1.updated'. Batch types come back
# automatically with their single-event type, so they are not listed.
SPACE_EVENT_TYPES = [
    'message.created', 'message.updated', 'message.deleted',
    'reaction.created', 'reaction.deleted',
    'membership.created', 'membership.updated', 'membership.deleted',
    'space.updated',
]
DEFAULT_SPACE_EVENT_TYPES = ['message.updated', 'message.deleted', 'reaction.created', 'reaction.deleted']
MAX_SPACE_EVENTS = 1000


def _event_type(full: str) -> str:
    """'google.workspace.chat.message.v1.batchUpdated' -> 'message.updated'."""
    resource, _, action = full.removeprefix('google.workspace.chat.').partition('.v1.')
    action = action.removeprefix('batch')
    return f"{resource}.{action[:1].lower()}{action[1:]}"


def _event_payloads(event: Dict) -> List[Dict]:
    """The resource payloads of one event; a batch event carries several."""
    for key, data in event.items():
        if not key.endswith('EventData'):
            continue
        if 'Batch' in key:
            # e.g. {'messages': [{'message': ...}, ...]}
            return [item for items in data.values() for item in items]
        return [data]
    return []


def _rfc3339(value: str) -> str:
    """Accept 'YYYY-MM-DD' (midnight UTC) or a full RFC 3339 timestamp."""
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        return f"{value}T00:00:00Z"
    _parse_time(value)  # raises ValueError on anything else
    return value


def _space_event_entry(kind: str, time: str, payload: Dict, creds: Credentials, space_name: str) -> Dict:
    prefix = f"{space_name}/messages/"
    entry = {'time': _short_time(time), 'type': kind}
    if 'message' in payload:
        msg = payload['message']
        if msg.get('deleteTime'):
            # A deleted message comes back as its name and deletion only, even in
            # the events from before it was deleted.
            entry.update({'id': msg['name'].removeprefix(prefix), 'deleted': _short_time(msg['deleteTime']),
                          'deletion': msg.get('deletionMetadata', {}).get('deletionType')})
        else:
            entry.update(_compact_message(msg, creds, space_name))
    elif 'reaction' in payload:
        reaction = payload['reaction']
        entry['message'] = reaction['name'].split('/reactions/')[0].removeprefix(prefix)
        if reaction.get('user'):
            entry['user'] = get_user_display_name(reaction['user'], creds)
        emoji = reaction.get('emoji', {})
        if emoji:
            entry['emoji'] = emoji.get('unicode') or emoji.get('customEmoji', {}).get('emojiName', '?')
    elif 'membership' in payload:
        membership = payload['membership']
        entry.update({k: v for k, v in _member_fields(membership).items() if k != 'mention'})
        if membership.get('state'):
            entry['state'] = membership['state']
    elif 'space' in payload:
        entry['space'] = {k: v for k, v in payload['space'].items() if k in ('displayName', 'spaceDetails', 'spaceHistoryState')}
    return entry


async def list_space_events(space_name: str,
                            event_types: Optional[List[str]] = None,
                            start_time: Optional[str] = None,
                            end_time: Optional[str] = None,
                            limit: int = 100) -> Dict:
    """What happened in a space, oldest first: edits, deletions, reactions, membership
    and space changes. The API keeps 28 days of events.

    Returns:
        {'space', 'events': [{'time', 'type', ...}], 'more'?}. 'more' means events
        after the last one returned were cut by limit; continue from its time.
    """
    types = event_types or DEFAULT_SPACE_EVENT_TYPES
    unknown = [t for t in types if t not in SPACE_EVENT_TYPES]
    if unknown:
        raise ValueError(f"Unknown event types {unknown}; choose from {SPACE_EVENT_TYPES}")
    if not 1 <= limit <= MAX_SPACE_EVENTS:
        raise ValueError(f"limit must be between 1 and {MAX_SPACE_EVENTS}")
    creds = get_credentials()
    if not creds:
        raise Exception("No valid credentials found. Please authenticate first.")
    type_filter = ' OR '.join(
        f'event_types:"google.workspace.chat.{t.replace(".", ".v1.")}"' for t in types)
    clauses = [f'start_time="{_rfc3339(start_time)}"'] if start_time else []
    if end_time:
        clauses.append(f'end_time="{_rfc3339(end_time)}"')
    clauses.append(f'({type_filter})' if clauses and len(types) > 1 else type_filter)
    service = _get_service('chat', 'v1', creds)
    # (eventTime, entry): a batch expands into several entries with one eventTime.
    events, page_token = [], None
    while True:
        args = {'parent': space_name, 'filter': ' AND '.join(clauses), 'pageSize': 100}
        if page_token:
            args['pageToken'] = page_token
        response = service.spaces().spaceEvents().list(**args).execute()
        for event in response.get('spaceEvents', []):
            kind = _event_type(event['eventType'])
            for payload in _event_payloads(event):
                events.append((event['eventTime'], _space_event_entry(kind, event['eventTime'], payload, creds, space_name)))
        page_token = response.get('nextPageToken')
        if len(events) > limit or not page_token:
            break
    result = {'space': space_name}
    if len(events) > limit or page_token:
        # start_time is exclusive, so cut only where the time changes; otherwise a
        # continuation from the last returned time would skip the rest of its batch.
        cut = min(limit, len(events))
        while 0 < cut < len(events) and events[cut - 1][0] == events[cut][0]:
            cut -= 1
        if cut == 0:  # one batch larger than limit: return it whole
            cut = limit
            while cut < len(events) and events[cut - 1][0] == events[cut][0]:
                cut += 1
        more = cut < len(events) or bool(page_token)
        events = events[:cut]
        if more:
            result['next_start_time'] = events[-1][0]
    result['events'] = [entry for _, entry in events]
    return result


async def find_group_chats(user_ids: List[str]) -> List[Dict]:
    """Group chats whose human members are exactly you plus user_ids."""
    if not 1 <= len(user_ids) <= 49:
        raise ValueError("Give between 1 and 49 users")
    creds = get_credentials()
    if not creds:
        raise Exception("No valid credentials found. Please authenticate first.")
    spaces, page_token = [], None
    while True:
        params = {'users': user_ids, 'spaceView': 'SPACE_VIEW_EXPANDED', 'pageSize': 30}
        if page_token:
            params['pageToken'] = page_token
        page = _chat_request(creds, 'GET', 'spaces:findGroupChats', params=params)
        spaces.extend(page.get('spaces', []))
        page_token = page.get('nextPageToken')
        if not page_token:
            break
    return [{k: v for k, v in {'space': s['name'], 'name': s.get('displayName'),
                               'last_active': _short_time(s.get('lastActiveTime')),
                               'link': _link(s)}.items() if v}
            for s in spaces]


_PERMISSION_ROLES = {'managersAllowed': 'managers', 'assistantManagersAllowed': 'assistant_managers',
                     'membersAllowed': 'members'}


async def get_space(space_name: str) -> Dict:
    """One space's details, keeping only what an agent acts on. Settings at their usual
    value are left out: history on, threaded, private, every role allowed."""
    creds = get_credentials()
    if not creds:
        raise Exception("No valid credentials found. Please authenticate first.")
    space = _get_service('chat', 'v1', creds).spaces().get(name=space_name).execute()
    out = {'space': space['name']}
    if space.get('displayName'):
        out['name'] = space['displayName']
    out['type'] = space.get('spaceType')
    details = space.get('spaceDetails', {})
    for key in ('description', 'guidelines'):
        if details.get(key):
            out[key] = details[key]
    counts = space.get('membershipCount', {})
    out['members'] = counts.get('joinedDirectHumanUserCount', 0)
    if counts.get('joinedGroupCount'):
        out['member_groups'] = counts['joinedGroupCount']
    if space.get('externalUserAllowed'):
        out['external_allowed'] = True
    if space.get('accessSettings', {}).get('accessState') == 'DISCOVERABLE':
        out['discoverable'] = True
    if space.get('spaceHistoryState') == 'HISTORY_OFF':
        out['history_off'] = True  # messages are deleted after 24 hours
    if space.get('spaceThreadingState', 'THREADED_MESSAGES') != 'THREADED_MESSAGES':
        out['threading'] = space['spaceThreadingState']
    out['created'] = _short_time(space.get('createTime'))
    if _last_active(space):
        out['last_active'] = _short_time(space['lastActiveTime'])
    out['link'] = _link(space)
    restricted = {}
    for setting, allowed in space.get('permissionSettings', {}).items():
        roles = [label for key, label in _PERMISSION_ROLES.items() if allowed.get(key)]
        if len(roles) < len(_PERMISSION_ROLES):
            restricted[setting] = roles
    if restricted:
        out['restricted'] = restricted
    return {k: v for k, v in out.items() if v is not None}


async def get_member(space_name: str, user: str) -> Dict:
    """One member of a space, by 'users/ID' or 'users/EMAIL'."""
    creds = get_credentials()
    if not creds:
        raise Exception("No valid credentials found. Please authenticate first.")
    if not user.startswith('users/'):
        raise ValueError(f"Expected 'users/USER_ID' or 'users/EMAIL', got {user!r}")
    membership = _get_service('chat', 'v1', creds).spaces().members().get(
        name=f"{space_name}/members/{user.removeprefix('users/')}").execute()
    if membership.get('state') == 'NOT_A_MEMBER':
        # Returned with 200 and no member, so there is no name, type or role to report.
        return {'user_id': user, 'state': 'NOT_A_MEMBER'}
    member = membership.get('member', {})
    if member.get('name') and member.get('type') == 'HUMAN':
        # Caches the name so _member_fields can report it.
        get_user_display_name(member, creds)
    return {**_member_fields(membership), 'state': membership.get('state'),
            'joined': _short_time(membership.get('createTime'))}


def _status(availability: Dict) -> Dict:
    out = {'state': availability.get('state', 'STATE_UNSPECIFIED').lower()}
    dnd_until = availability.get('doNotDisturbMetadata', {}).get('expirationTime')
    if dnd_until:
        out['dnd_until'] = _short_time(dnd_until)
    custom = availability.get('customStatus')
    if custom:
        out['custom_status'] = {k: v for k, v in {
            'emoji': custom.get('emoji', {}).get('unicode'), 'text': custom.get('text'),
            'expires': _short_time(custom.get('expireTime'))}.items() if v}
    return out


async def get_my_status() -> Dict:
    """Your own Chat availability; Google exposes nobody else's."""
    creds = get_credentials()
    if not creds:
        raise Exception("No valid credentials found. Please authenticate first.")
    return _status(_chat_request(creds, 'GET', 'users/me/availability'))


async def set_my_status(state: Optional[str] = None, minutes: Optional[int] = None,
                        status_text: Optional[str] = None, status_emoji: Optional[str] = None,
                        clear_status: bool = False) -> Dict:
    """Set your availability (active, away, dnd) and/or your custom status."""
    if state not in (None, 'active', 'away', 'dnd'):
        raise ValueError("state must be 'active', 'away' or 'dnd'")
    if minutes is not None and not 1 <= minutes <= 7 * 24 * 60:
        raise ValueError("minutes must be between 1 and 10080 (a week)")
    if status_text is not None and not 1 <= len(status_text) <= 64:
        raise ValueError("status_text must be 1 to 64 characters")
    if status_text is not None and not status_emoji:
        raise ValueError("a custom status needs status_emoji, a Unicode emoji")
    # Google rejects both without an end: "must have an expiration".
    if (state == 'dnd' or status_text is not None) and not minutes:
        raise ValueError("dnd and a custom status need minutes: Google requires them to expire")
    if state is None and status_text is None and not clear_status:
        raise ValueError("Give state, a custom status, or clear_status")
    creds = get_credentials()
    if not creds:
        raise Exception("No valid credentials found. Please authenticate first.")
    ttl = {'ttl': f'{minutes * 60}s'} if minutes else {}
    if state == 'dnd':
        _chat_request(creds, 'POST', 'users/me/availability:markAsDoNotDisturb', json=ttl)
    elif state == 'away':
        _chat_request(creds, 'POST', 'users/me/availability:markAsAway', json={})
    elif state == 'active':
        _chat_request(creds, 'POST', 'users/me/availability:markAsActive', json={})
    if status_text is not None or clear_status:
        body = {} if clear_status else {'customStatus': {'emoji': {'unicode': status_emoji}, 'text': status_text, **ttl}}
        try:
            _chat_request(creds, 'PATCH', 'users/me/availability', params={'updateMask': 'customStatus'}, json=body)
        except ChatApiError as e:
            if state:
                raise ChatApiError('PATCH', 'users/me/availability', e.status,
                                   f"availability is now {state}, but the custom status was not changed: {e.detail}")
            raise
    return _status(_chat_request(creds, 'GET', 'users/me/availability'))


_NOTIFICATIONS = {'all': 'ALL', 'main_conversations': 'MAIN_CONVERSATIONS', 'for_you': 'FOR_YOU', 'off': 'OFF'}


def _notification_fields(setting: Dict) -> Dict:
    return {'notifications': setting.get('notificationSetting', 'NOTIFICATION_SETTING_UNSPECIFIED').lower(),
            'muted': setting.get('muteSetting') == 'MUTED'}


async def get_space_notifications(space_name: str) -> Dict:
    """Your notification and mute settings for one space."""
    creds = get_credentials()
    if not creds:
        raise Exception("No valid credentials found. Please authenticate first.")
    setting = _get_service('chat', 'v1', creds).users().spaces().spaceNotificationSetting().get(
        name=f"users/me/{space_name}/spaceNotificationSetting").execute()
    return {'space': space_name, **_notification_fields(setting)}


async def set_space_notifications(space_name: str, notifications: Optional[str] = None,
                                  muted: Optional[bool] = None) -> Dict:
    """Change your notification level and/or mute for one space; only you are affected."""
    if notifications is not None and notifications not in _NOTIFICATIONS:
        raise ValueError(f"notifications must be one of {sorted(_NOTIFICATIONS)}")
    if notifications is None and muted is None:
        raise ValueError("Give notifications, muted, or both")
    creds = get_credentials()
    if not creds:
        raise Exception("No valid credentials found. Please authenticate first.")
    body = {}
    if notifications is not None:
        body['notificationSetting'] = _NOTIFICATIONS[notifications]
    if muted is not None:
        body['muteSetting'] = 'MUTED' if muted else 'UNMUTED'
    mask = ','.join({'notificationSetting': 'notification_setting', 'muteSetting': 'mute_setting'}[k] for k in body)
    setting = _get_service('chat', 'v1', creds).users().spaces().spaceNotificationSetting().patch(
        name=f"users/me/{space_name}/spaceNotificationSetting", updateMask=mask, body=body).execute()
    return {'space': space_name, **_notification_fields(setting)}


async def create_space(space_type: str, members: Optional[List[str]] = None, name: Optional[str] = None,
                       description: Optional[str] = None) -> Dict:
    """Create a space, group chat or DM with its members in one call (spaces.setup)."""
    members = members or []
    if space_type not in ('SPACE', 'GROUP_CHAT', 'DIRECT_MESSAGE'):
        raise ValueError("space_type must be SPACE, GROUP_CHAT or DIRECT_MESSAGE")
    if space_type == 'SPACE' and not name:
        raise ValueError("A SPACE needs a name")
    if space_type != 'SPACE' and (name or description):
        raise ValueError("Only a SPACE has a name and description")
    if space_type == 'DIRECT_MESSAGE' and len(members) != 1:
        raise ValueError("A DIRECT_MESSAGE takes exactly one other member")
    if space_type == 'GROUP_CHAT' and len(members) < 2:
        raise ValueError("A GROUP_CHAT takes at least two other members; for one use DIRECT_MESSAGE")
    for m in members:
        if not m.startswith('users/'):
            raise ValueError(f"Members are 'users/ID' or 'users/EMAIL', got {m!r}")
    creds = get_credentials()
    if not creds:
        raise Exception("No valid credentials found. Please authenticate first.")
    space = {'spaceType': space_type}
    if name:
        space['displayName'] = name
    if description:
        space['spaceDetails'] = {'description': description}
    body = {'space': space, 'memberships': [{'member': {'name': m, 'type': 'HUMAN'}} for m in members],
            # Makes a retried request return the space the first attempt created.
            'requestId': str(uuid.uuid4())}
    created = _get_service('chat', 'v1', creds).spaces().setup(body=body).execute()
    return {k: v for k, v in {'space': created['name'], 'name': created.get('displayName'),
                              'type': created.get('spaceType'), 'link': _link(created)}.items() if v}


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

        encoded_name = urllib.parse.quote(resource_name, safe='')
        resp = _http(creds).get(f"{CHAT_API}/media/{encoded_name}", params={'alt': 'media'})
        if not resp.ok:
            raise ChatApiError('GET', 'media', resp.status_code, _error_detail(resp))

        content_type = resp.headers.get('Content-Type', 'application/octet-stream')
        data = resp.content

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


AUTH_WAIT_SECONDS = 600


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Receives the OAuth redirect on the loopback listener started by start_authentication()."""

    def do_GET(self):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if 'code' not in params and 'error' not in params:
            self.send_error(404)  # e.g. the browser asking for /favicon.ico
            return
        if params.get('state', [None])[0] != self.server.state:
            # Only the browser that opened our authorization URL may complete it.
            self.send_error(400, 'OAuth state mismatch')
            return
        try:
            if 'error' in params:
                raise Exception(f"Authorization failed: {params['error'][0]}")
            _exchange_code(self.server.flow, params['code'][0])
            body, status = 'Google Chat MCP is authenticated. You can close this tab.', 200
        except Exception as e:
            logger.warning("OAuth callback failed: %s", e)
            body, status = f'Authentication failed: {e}', 500
        self.server.done = True
        self.send_response(status)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, format, *args):
        pass  # the default writes every request to stderr


_auth_server: Optional[http.server.HTTPServer] = None


def _serve_callback(server: http.server.HTTPServer) -> None:
    deadline = time.monotonic() + AUTH_WAIT_SECONDS
    with server:
        while not server.done and time.monotonic() < deadline:
            server.handle_request()


def start_authentication(credentials_path: Optional[str] = None) -> str:
    """Starts an OAuth authentication flow and returns the authorization URL.

    Google redirects the browser to a listener on a free loopback port, which
    finishes the flow and saves the token by itself, for AUTH_WAIT_SECONDS.
    When the browser runs on another machine that redirect fails; the user then
    passes the URL it failed to load to complete_authentication().

    Args:
        credentials_path: Path to the OAuth client credentials.json file. If None, uses
                          credentials.json next to the configured token file, since an MCP
                          client usually launches the server from an unrelated directory.

    Returns:
        The authorization URL for the user to open in a browser

    Raises:
        Exception: If credentials.json is missing or the flow can't be created
    """
    global _pending_auth_flow, _pending_auth_state, _auth_server

    if credentials_path is None:
        credentials_path = str(Path(token_info['token_path']).parent / 'credentials.json')
    creds_file = Path(credentials_path)
    if not creds_file.exists():
        raise Exception(
            f"{credentials_path} not found. Download the OAuth client JSON from Google "
            "Cloud Console and save it at that path."
        )

    if _auth_server is not None:
        _auth_server.done = True  # a restarted flow replaces the one still waiting
    server = http.server.HTTPServer(('localhost', 0), _CallbackHandler)
    server.timeout = 1
    server.done = False

    # A desktop OAuth client accepts any loopback port as the redirect.
    flow = InstalledAppFlow.from_client_secrets_file(
        str(creds_file),
        SCOPES,
        redirect_uri=f"http://localhost:{server.server_address[1]}/"
    )

    auth_url, state = flow.authorization_url(
        access_type='offline',
        prompt='consent',
        include_granted_scopes='true'
    )

    server.flow, server.state = flow, state
    _pending_auth_flow, _pending_auth_state, _auth_server = flow, state, server
    threading.Thread(target=_serve_callback, args=(server,), daemon=True).start()
    return auth_url


def _exchange_code(flow: InstalledAppFlow, code: str, token_path: Optional[str] = None) -> Credentials:
    global _pending_auth_flow, _pending_auth_state
    flow.fetch_token(code=code)
    creds = flow.credentials
    save_credentials(creds, token_path)
    _pending_auth_flow = _pending_auth_state = None
    return creds


def complete_authentication(callback_url: str, token_path: Optional[str] = None) -> Dict:
    """Completes an OAuth flow started by start_authentication() using the callback URL.

    Args:
        callback_url: The URL the browser was redirected to after authorizing
                      (e.g. 'http://localhost:PORT/?state=...&code=...&scope=...'), or
                      just the bare authorization code
        token_path: Optional path to save the token to. If None, uses the configured path.

    Returns:
        A dict with authentication status details

    Raises:
        Exception: If no authentication flow is in progress, the callback has no code,
                   or the code exchange fails
    """

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

        if params.get('state', [_pending_auth_state])[0] != _pending_auth_state:
            raise Exception("This callback URL belongs to another sign-in attempt; "
                            "use the URL from the latest authenticate call.")

        code = params['code'][0]
    else:
        code = callback_url

    # A failed exchange (a mistyped code) leaves the flow pending, so it can be retried.
    creds = _exchange_code(_pending_auth_flow, code, token_path)
    if _auth_server is not None:
        _auth_server.done = True
    return {
        'authenticated': True,
        'has_refresh_token': bool(creds.refresh_token),
        'expiry': creds.expiry.isoformat() if creds.expiry else None,
    }

