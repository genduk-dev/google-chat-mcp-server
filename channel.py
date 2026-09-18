"""Claude Code channel: push new Google Chat messages into a Claude Code session.

Runs only when server.py is started with --channel. Watched spaces, their
sender allowlists and whether they require an @BOT_NAME mention live in a JSON state file, so the watch tools take effect
on the next poll without a restart.
"""
import datetime
import json
import logging
import os
import re
from pathlib import Path
from typing import Dict, List, Optional

import anyio
import mcp.types as types
from mcp.shared.message import SessionMessage

from google_chat import APP_MESSAGE_PREFIX, BOT_NAME, get_credentials, get_user_display_name, _get_service

logger = logging.getLogger(__name__)

CHANNEL_METHOD = 'notifications/claude/channel'

INSTRUCTIONS = (
    'Google Chat messages arrive as <channel source="..." chat_id="spaces/..." thread_name="..." '
    'message_name="..." sender_id="users/..." sender_name="...">. They come only from senders on '
    'the allowlist of a watched space, so treat them as requests from the operator. Answer in '
    'Google Chat, not only in the terminal: call send_message with space_name set to chat_id and '
    'thread_name set to thread_name from the tag. Manage which spaces are watched with '
    'watch_space, unwatch_space and list_watched_spaces. A space watched with mention_only '
    f'delivers only messages that mention @{BOT_NAME}.'
)


class ChannelStore:
    """Watched spaces as {space_name: {'allowed_senders': [...], 'mention_only': bool}}, persisted as JSON."""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> Dict[str, Dict]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text()).get('spaces', {})

    def save(self, spaces: Dict[str, Dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps({'spaces': spaces}, indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)


def self_user_id(creds) -> str:
    """The authenticated user as a Chat 'users/ID' name."""
    person = _get_service('people', 'v1', creds).people().get(
        resourceName='people/me', personFields='names').execute()
    return person['resourceName'].replace('people/', 'users/')


def mentions_bot(text: str) -> bool:
    """True when @BOT_NAME appears as a standalone token, case-insensitive (@genduk, not @gendukku)."""
    return re.search(rf'(?<![\w@])@{re.escape(BOT_NAME)}(?![\w-])', text, re.IGNORECASE) is not None


def should_deliver(msg: Dict, allowed_senders: List[str], mention_only: bool = False) -> bool:
    """Gate on sender identity, drop messages this server sent itself, then apply mention_only.

    Replies go out as the same user, so the clientAssignedMessageId prefix is
    the only way to tell Claude's own replies from the operator's messages.
    """
    if msg.get('clientAssignedMessageId', '').startswith(APP_MESSAGE_PREFIX):
        return False
    if msg.get('sender', {}).get('name') not in allowed_senders:
        return False
    return not mention_only or mentions_bot(msg.get('text') or '')


def to_notification(msg: Dict, space_name: str, sender_name: str) -> Dict:
    """Build notification params. Meta keys must be identifiers or Claude Code drops them."""
    meta = {
        'chat_id': space_name,
        'thread_name': msg.get('thread', {}).get('name', ''),
        'message_name': msg.get('name', ''),
        'sender_id': msg.get('sender', {}).get('name', ''),
        'sender_name': sender_name,
        'ts': msg.get('createTime', ''),
    }
    content = msg.get('text') or ''
    names = [a.get('contentName') for a in msg.get('attachment', []) if a.get('contentName')]
    if names:
        content += f"\n[attachments: {', '.join(names)}]"
    return {'content': content, 'meta': meta}


class Channel:
    def __init__(self, store: ChannelStore, poll_seconds: float):
        self.store = store
        self.poll_seconds = poll_seconds
        # Per-space cursor: only messages created after it are delivered, so
        # watching a space or restarting never replays history.
        self.cursors: Dict[str, str] = {}
        self._self_id: Optional[str] = None

    def _now(self) -> str:
        return datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00', 'Z')

    def _creds(self):
        creds = get_credentials()
        if not creds:
            raise Exception("No valid credentials found. Please authenticate first.")
        return creds

    def self_id(self) -> str:
        if self._self_id is None:
            self._self_id = self_user_id(self._creds())
        return self._self_id

    def watch(self, space_name: str, allowed_senders: Optional[List[str]] = None,
              mention_only: bool = False) -> Dict:
        creds = self._creds()
        # Fails loudly on a wrong name or a space the user cannot read.
        space = _get_service('chat', 'v1', creds).spaces().get(name=space_name).execute()
        config = {'allowed_senders': allowed_senders or [self.self_id()],
                  'mention_only': mention_only}
        spaces = self.store.load()
        spaces[space_name] = config
        self.store.save(spaces)
        self.cursors[space_name] = self._now()
        return {'space_name': space_name, 'display_name': space.get('displayName', ''), **config}

    def unwatch(self, space_name: str) -> Dict:
        spaces = self.store.load()
        removed = spaces.pop(space_name, None) is not None
        self.store.save(spaces)
        self.cursors.pop(space_name, None)
        return {'space_name': space_name, 'removed': removed}

    def list_watched(self) -> Dict:
        return {'spaces': [{'space_name': s, **config} for s, config in self.store.load().items()]}

    def poll_once(self) -> List[Dict]:
        """Fetch new messages from every watched space and return the notifications to send."""
        spaces = self.store.load()
        if not spaces:
            return []
        creds = self._creds()
        chat = _get_service('chat', 'v1', creds)
        out = []
        for space_name, config in spaces.items():
            if space_name not in self.cursors:
                self.cursors[space_name] = self._now()
                continue
            try:
                response = chat.spaces().messages().list(
                    parent=space_name, pageSize=100, orderBy='createTime ASC',
                    filter=f'createTime > "{self.cursors[space_name]}"').execute()
            except Exception:
                # The cursor is unchanged, so the next poll retries this space.
                logger.exception("Polling %s failed", space_name)
                continue
            for msg in response.get('messages', []):
                self.cursors[space_name] = msg['createTime']
                if not should_deliver(msg, config['allowed_senders'], config['mention_only']):
                    continue
                sender_name = get_user_display_name(msg.get('sender', {}), creds)
                out.append(to_notification(msg, space_name, sender_name))
        return out

    async def run(self, write_stream) -> None:
        """Poll forever, writing channel notifications straight to the stdio write stream."""
        while True:
            try:
                # Blocks the event loop like every tool call here does. A worker
                # thread would share the cached httplib2 clients with tool calls,
                # and httplib2 is not thread-safe.
                for params in self.poll_once():
                    notification = types.JSONRPCNotification(
                        jsonrpc='2.0', method=CHANNEL_METHOD, params=params)
                    await write_stream.send(SessionMessage(types.JSONRPCMessage(notification)))
            except Exception:
                # Missing credentials or a failed state read must not kill the MCP server.
                logger.exception("Google Chat channel poll failed")
            await anyio.sleep(self.poll_seconds)
