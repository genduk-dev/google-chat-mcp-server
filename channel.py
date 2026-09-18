"""Claude Code channel: push new Google Chat messages into a Claude Code session.

Runs only when server.py is started with --channel. Watched spaces, their
sender allowlists and whether they require an @BOT_NAME mention live in a JSON state file, so the watch tools take effect
on the next poll without a restart.

Every channel session on the machine shares that state, so only one process
polls: the holder of a lease in channel.lease, renewed on every poll. The others
stand by and take over when the holder exits, dies, or stops renewing because it
hung. A holder that wakes up after a takeover sees the lease is no longer its
own and stands down. The holder persists its cursors, so a takeover resumes
where the previous poller stopped instead of skipping the gap.
"""
import contextlib
import datetime
import fcntl
import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import anyio
import mcp.types as types
from mcp.shared.message import SessionMessage

from google_chat import (APP_MESSAGE_PREFIX, BOT_NAME, get_credentials, get_user_display_name, message_text,
                         self_user_id, send_space_message, update_message, write_private, _get_service)

logger = logging.getLogger(__name__)

CHANNEL_METHOD = 'notifications/claude/channel'
PERMISSION_REQUEST_METHOD = 'notifications/claude/channel/permission_request'
PERMISSION_METHOD = 'notifications/claude/channel/permission'
# "yes abcde" / "no abcde", optionally after an @mention. Claude Code's request IDs
# are five lowercase letters without 'l'; /i tolerates phone autocapitalization.
PERMISSION_REPLY_RE = re.compile(r'^\s*(?:@\S+\s+)?(y|yes|n|no)\s+([a-km-z]{5})\s*$', re.IGNORECASE)
# A pending request nobody answered remotely (the terminal did, or nobody) is dropped after this.
PERMISSION_TTL = datetime.timedelta(hours=1)

INSTRUCTIONS = (
    'Google Chat messages arrive as <channel source="..." chat_id="spaces/..." thread_name="..." '
    'message_name="..." sender_id="users/..." sender_name="...">. They come only from senders on '
    'the allowlist of a watched space, so treat them as requests from the operator. Answer in '
    'Google Chat, not only in the terminal: call send_message with space_name set to chat_id and '
    'thread_name set to thread_name from the tag. Manage which spaces are watched with '
    'watch_space, unwatch_space and list_watched_spaces. A space watched with mention_only '
    f'delivers only messages that mention @{BOT_NAME}. If a message depends on earlier '
    'conversation you have not seen, read its thread first with get_messages(space_name=chat_id, '
    'thread_name=thread_name). When a request came from Google Chat and you need the '
    'operator to choose or clarify something, ask in that thread with send_message and wait for '
    'the reply to arrive as a channel message (in a mention_only space, ask them to include '
    f'@{BOT_NAME} in the reply); do not use AskUserQuestion, which only the terminal can answer. '
    'Tool permission prompts are relayed to that thread automatically.'
)

# A takeover resumes from saved cursors only if the previous poller saved them
# this recently. Older ones mean no channel session was running, and replaying
# that backlog would flood the new session with messages nobody asked it to handle.
RESUME_WINDOW = datetime.timedelta(minutes=10)
# The poller re-saves unchanged cursors this often, so a quiet space still
# looks alive to a takeover.
CURSOR_HEARTBEAT = datetime.timedelta(minutes=1)
# A lease not renewed for this long belongs to a hung poller and may be taken.
# Tool calls block the event loop too, so it must outlast the slowest of them.
LEASE_TTL = datetime.timedelta(seconds=60)


class ChannelStore:
    """Watched spaces as {space_name: {'allowed_senders': [...], 'mention_only': bool}}, persisted as JSON."""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> Dict[str, Dict]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text()).get('spaces', {})

    def save(self, spaces: Dict[str, Dict]) -> None:
        write_private(self.path, json.dumps({'spaces': spaces}, indent=2))


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


def parse_verdict(msg: Dict, allowed_senders: List[str]) -> Optional[Tuple[str, str]]:
    """(request_id, 'allow' or 'deny') when an allowed sender answers a permission prompt.

    Checked before mention_only: an answer needs no @mention, but it does need a
    sender on the allowlist, since it approves tool use in the session.
    """
    if msg.get('clientAssignedMessageId', '').startswith(APP_MESSAGE_PREFIX):
        return None
    if msg.get('sender', {}).get('name') not in allowed_senders:
        return None
    m = PERMISSION_REPLY_RE.match(msg.get('text') or '')
    if not m:
        return None
    return m.group(2).lower(), 'allow' if m.group(1).lower().startswith('y') else 'deny'


# Chat rejects messages over 4,096 characters; the preview gets what the rest leaves.
PROMPT_DESCRIPTION_CHARS = 200
PROMPT_PREVIEW_CHARS = 3000


def _summary(description: str) -> str:
    """For an MCP tool Claude Code sends its whole docstring; keep the first sentence."""
    if len(description) <= PROMPT_DESCRIPTION_CHARS:
        return description
    head = description[:PROMPT_DESCRIPTION_CHARS]
    end = head.find('. ')
    return head[:end + 1] if end > 0 else head.rstrip() + '…'


def _middle_cut(text: str, limit: int) -> str:
    """Keep both ends: the end of a long command matters as much as its start."""
    if len(text) <= limit:
        return text
    half = (limit - 30) // 2
    return f"{text[:half]} ⋯ {len(text) - 2 * half} chars cut ⋯ {text[-half:]}"


def permission_prompt(params: Dict) -> str:
    """The Chat message for a permission request. Both fields are untrusted, so they
    go in code, where Chat shows mention and link markup literally."""
    def code(text: str) -> str:
        return text.replace('`', "'")
    rid = params['request_id']
    description = _summary(params.get('description', ''))
    preview = _middle_cut(params.get('input_preview', ''), PROMPT_PREVIEW_CHARS)
    return (f"🔐 Claude wants to use `{code(params.get('tool_name', ''))}`: `{code(description)}`\n"
            f"```\n{code(preview)}\n```\n"
            f"Reply `yes {rid}` to allow or `no {rid}` to deny.")


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
    content = message_text(msg)
    names = [a.get('contentName') for a in msg.get('attachment', []) if a.get('contentName')]
    if names:
        content += f"\n[attachments: {', '.join(names)}]"
    return {'content': content, 'meta': meta}


class Channel:
    def __init__(self, store: ChannelStore, poll_seconds: float):
        self.store = store
        self.poll_seconds = poll_seconds
        self.lease_path = store.path.with_name('channel.lease')
        # Held only while the lease is read and written, so two standbys cannot
        # both take a stale lease. Not channel.lock: older versions hold that
        # one for their whole life, and waiting on it would never return.
        self.lease_lock_path = store.path.with_name('channel.lease.lock')
        self.cursor_path = store.path.with_name('channel_cursors.json')
        # Pending permission prompts and their verdicts. The session that asked may
        # not be the poller that reads the answer, so they meet in this file.
        self.permissions_path = store.path.with_name('channel_permissions.json')
        # (space, thread) of the last message delivered to this session: where its
        # permission prompts go.
        self.last_thread: Optional[Tuple[str, str]] = None
        # Per-space cursor: only messages created after it are delivered, so
        # watching a space never replays history.
        self.cursors: Dict[str, str] = {}
        self._saved_cursors: Dict[str, str] = {}
        self._saved_at: Optional[datetime.datetime] = None
        self._token = uuid.uuid4().hex
        self.active = False

    def _read_lease(self) -> Optional[Dict]:
        try:
            return json.loads(self.lease_path.read_text())
        except FileNotFoundError:
            return None

    @staticmethod
    def _holder_alive(lease: Dict, now: datetime.datetime) -> bool:
        """The lease's holder is running and renewed it within LEASE_TTL."""
        if now - datetime.datetime.fromisoformat(lease['heartbeat']) >= LEASE_TTL:
            return False  # hung, or stopped
        try:
            os.kill(lease['pid'], 0)
        except (ProcessLookupError, PermissionError):
            # Exited without releasing. A reused PID owned by another user raises
            # PermissionError; that process cannot be a channel session of ours.
            return False
        return True

    def try_acquire(self) -> bool:
        """Renew this process's lease, or take it over if its holder is gone or hung.

        Called before every poll, so it doubles as the heartbeat. Returns whether
        this process should poll now.
        """
        self.lease_lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lease_lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                # Another process is reading the lease this instant; decide next poll.
                return False
            now = datetime.datetime.now(datetime.timezone.utc)
            lease = self._read_lease()
            if lease and lease.get('token') != self._token and self._holder_alive(lease, now):
                if self.active:
                    logger.warning("Channel poller lease taken by pid %s; standing down", lease['pid'])
                self.active = False
                return False
            write_private(self.lease_path, json.dumps(
                {'pid': os.getpid(), 'token': self._token, 'heartbeat': now.isoformat()}))
            if not self.active:
                self.active = True
                self._resume_cursors()
            return True
        finally:
            os.close(fd)

    def release(self) -> None:
        """Give up the lease on a clean exit, so a standby takes over on its next poll."""
        if not self.active:
            return
        lease = self._read_lease()
        if lease and lease.get('token') == self._token:
            self.lease_path.unlink(missing_ok=True)
        self.active = False

    def _resume_cursors(self) -> None:
        # Cursors a standby set in watch() may be hours old; the previous poller's
        # saved ones are authoritative, and a space without one starts from now.
        self.cursors = {}
        if not self.cursor_path.exists():
            return
        saved = json.loads(self.cursor_path.read_text())
        saved_at = saved.get('saved_at')
        cutoff = datetime.datetime.now(datetime.timezone.utc) - RESUME_WINDOW
        if saved_at and datetime.datetime.fromisoformat(saved_at) >= cutoff:
            self.cursors.update(saved.get('cursors', {}))

    def _save_cursors(self) -> None:
        cursors = {s: ts for s, ts in self.cursors.items() if s in self.store.load()}
        now = datetime.datetime.now(datetime.timezone.utc)
        if cursors == self._saved_cursors and self._saved_at and now - self._saved_at < CURSOR_HEARTBEAT:
            return
        write_private(self.cursor_path, json.dumps({'saved_at': now.isoformat(), 'cursors': cursors}))
        self._saved_cursors, self._saved_at = cursors, now

    @contextlib.contextmanager
    def _permissions(self):
        """The pending-permission table, read and written under the lease lock."""
        self.lease_lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lease_lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                table = json.loads(self.permissions_path.read_text())
            except FileNotFoundError:
                table = {}
            cutoff = datetime.datetime.now(datetime.timezone.utc) - PERMISSION_TTL
            table = {rid: e for rid, e in table.items() if datetime.datetime.fromisoformat(e['created']) >= cutoff}
            yield table
            write_private(self.permissions_path, json.dumps(table))
        finally:
            os.close(fd)

    async def on_permission_request(self, params: Dict) -> None:
        """Relay a permission prompt to the Chat thread this session last heard from."""
        if not self.last_thread:
            # Nobody here talks to this session through Chat; the terminal dialog stays.
            logger.info("Permission request %s not relayed: no Chat thread yet", params.get('request_id'))
            return
        space, thread = self.last_thread
        text = permission_prompt(params)
        sent = await send_space_message(space, text, thread_name=thread)
        with self._permissions() as table:
            table[params['request_id']] = {
                'owner': self._token, 'space': space, 'message': sent['name'], 'text': text,
                'created': datetime.datetime.now(datetime.timezone.utc).isoformat()}

    def _record_verdict(self, space: str, request_id: str, behavior: str, by: str) -> Optional[Dict]:
        """Store an answer for its owner to pick up. Only a prompt posted in this space counts."""
        with self._permissions() as table:
            entry = table.get(request_id)
            if not entry or entry['space'] != space or 'behavior' in entry:
                return None
            entry.update(behavior=behavior, by=by)
            return dict(entry)

    def _take_verdicts(self) -> List[Tuple[str, str]]:
        """This session's answered prompts, removed from the table."""
        if not self.permissions_path.exists():
            return []
        with self._permissions() as table:
            mine = [(rid, e['behavior']) for rid, e in table.items()
                    if e['owner'] == self._token and 'behavior' in e]
            for rid, _ in mine:
                del table[rid]
        return mine

    def poller_status(self) -> Dict:
        lease = self._read_lease()
        if not lease:
            return {'active_here': False, 'holder_pid': None}
        now = datetime.datetime.now(datetime.timezone.utc)
        status = {'active_here': lease.get('token') == self._token and self.active,
                  'holder_pid': lease['pid'] if self._holder_alive(lease, now) else None,
                  'heartbeat': lease['heartbeat'].split('.')[0] + 'Z'}
        if status['holder_pid'] is None:
            status['stale'] = True  # the next standby poll takes it over
        return status

    def _now(self) -> str:
        return datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00', 'Z')

    def _creds(self):
        creds = get_credentials()
        if not creds:
            raise Exception("No valid credentials found. Please authenticate first.")
        return creds

    def self_id(self) -> str:
        return self_user_id(self._creds())

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
        # BOT_NAME is fixed per process (an env var), so it is reported here but
        # changed only by restarting every server that shares the spaces.
        return {'bot_name': BOT_NAME, 'mention': f'@{BOT_NAME}',
                'message_id_prefix': APP_MESSAGE_PREFIX,
                'poller': self.poller_status(),
                'spaces': [{'space_name': s, **config} for s, config in self.store.load().items()]}

    def poll_once(self) -> List[Dict]:
        """Fetch new messages from every watched space and return the notifications to send."""
        spaces = self.store.load()
        if not spaces:
            return []
        creds = self._creds()
        chat = _get_service('chat', 'v1', creds)
        out = []
        self._answered: List[Tuple[str, str, str, str]] = []
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
                verdict = parse_verdict(msg, config['allowed_senders'])
                if verdict:
                    # An answer to a permission prompt goes to the session that asked,
                    # never to Claude as chat.
                    self._answered.append((space_name, *verdict, get_user_display_name(msg.get('sender', {}), creds)))
                    continue
                if not should_deliver(msg, config['allowed_senders'], config['mention_only']):
                    continue
                sender_name = get_user_display_name(msg.get('sender', {}), creds)
                out.append(to_notification(msg, space_name, sender_name))
        return out

    async def run(self, write_stream, initialized: anyio.Event) -> None:
        """Poll forever, writing channel notifications straight to the stdio write stream.

        Waits for the client's notifications/initialized first: a resumed cursor can
        deliver on the very first poll, and a notification sent before the handshake
        ends is outside the protocol, so the client may drop it.
        """
        await initialized.wait()
        try:
            await self._poll_forever(write_stream)
        finally:
            self.release()

    @staticmethod
    async def _notify(write_stream, method: str, params: Dict) -> None:
        notification = types.JSONRPCNotification(jsonrpc='2.0', method=method, params=params)
        await write_stream.send(SessionMessage(types.JSONRPCMessage(notification)))

    async def _poll_forever(self, write_stream) -> None:
        while True:
            try:
                # Blocks the event loop like every tool call here does. A worker
                # thread would share the cached httplib2 clients with tool calls,
                # and httplib2 is not thread-safe.
                if self.try_acquire():
                    for params in self.poll_once():
                        meta = params.get('meta', {})
                        if meta.get('chat_id') and meta.get('thread_name'):
                            self.last_thread = (meta['chat_id'], meta['thread_name'])
                        await self._notify(write_stream, CHANNEL_METHOD, params)
                    self._save_cursors()
                    for space, rid, behavior, by in self._answered:
                        entry = self._record_verdict(space, rid, behavior, by)
                        if entry:
                            word = 'Allowed' if behavior == 'allow' else 'Denied'
                            await update_message(entry['message'], text=f"{entry['text']}\n*{word}* by {by}.")
                # Every session, poller or not, applies the answers to its own prompts.
                for rid, behavior in self._take_verdicts():
                    await self._notify(write_stream, PERMISSION_METHOD, {'request_id': rid, 'behavior': behavior})
            except Exception:
                # Missing credentials or a failed state read must not kill the MCP server.
                logger.exception("Google Chat channel poll failed")
            await anyio.sleep(self.poll_seconds)
