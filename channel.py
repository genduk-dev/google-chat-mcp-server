"""Claude Code channel: push new Google Chat messages into a Claude Code session.

Runs only when server.py is started with --channel. Watched spaces, their
sender allowlists and whether they require an @BOT_NAME mention live in a JSON
state file, so the watch tools take effect on the next poll without a restart.

Every channel session on the machine shares that state, so only one process
polls: the holder of a lease in channel.lease, renewed on every poll. The others
stand by and take over when the holder exits, dies, or stops renewing because it
hung. A holder that wakes up after a takeover sees the lease is no longer its
own and stands down. The holder persists its cursors, so a takeover resumes
where the previous poller stopped instead of skipping the gap.

In a mention_only space the bot behaves like a person who was pinged: idle
until someone addresses it, then present in the space, reading everything,
until the conversation moves on (see SpaceState). What it knows about each
space for that lives in the poller's memory only, so a restart starts idle.
"""
import contextlib
import copy
import dataclasses
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

from google_chat import (APP_MESSAGE_PREFIX, BOT_NAME, AttachmentTooLarge, get_credentials,
                         get_user_display_name, message_text, save_attachment, space_display_name,
                         self_user_id, send_space_message, update_message, write_private, _get_service,
                         _event_payloads, _parse_time)

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
    'Google Chat messages arrive as <channel source="..." chat_id="spaces/..." '
    'space_display_name="..." thread_name="..." message_name="..." sender_id="users/..." '
    'sender_name="...">; space_display_name names the space for you to read and is absent for a '
    'direct message, while chat_id is the value tools take. sender_is_operator="true" marks the '
    'operator, the signed-in user you work for; every other sender is another member of the space. '
    'A message is not an order to reply. Reply when it is addressed to you: mentioned="true" (an '
    f'@{BOT_NAME} mention), replying_to_bot="true" (a quote reply to one of your messages), or any '
    'message in a space watched without mention_only. Even then leave out a reply that adds '
    'nothing, such as one to thanks or an ok. Anything else you only read: stay silent unless '
    'someone clearly asks you something or you can clearly help. To stay silent, end your turn '
    'without calling send_message. To reply, call send_message with space_name set to chat_id and '
    'thread_name set to the thread of the message you answer. '
    'In a space watched with mention_only you are idle until someone addresses you. Then you are '
    'in the conversation (presence="active"): every message in the space reaches you, most of '
    'them not for you, until nobody has addressed you for 10 minutes, or for an hour at most. '
    'Messages that arrive close together come as one delivery. When it holds more than one, its '
    'content lists each under "New" with its sender, time and thread, and the meta describes the '
    'last one. A delivery also carries, under "Earlier", the messages you have not seen that it '
    'follows from, so call get_messages only when those are not enough. Attachments are saved on '
    'this machine, and the content gives each path; read an image or a file there. '
    'When someone ends the conversation with you or asks you to stop, call leave_conversation. '
    'When the operator asks you to keep quiet in a space for a while, call mute_space. '
    'Manage which spaces are watched with watch_space, unwatch_space and list_watched_spaces. '
    'When you need someone to choose or clarify something, ask in that thread with send_message '
    'and wait for the reply to arrive as a channel message; do not use AskUserQuestion, which only '
    'the terminal can answer. Tool permission prompts are relayed to that thread automatically, and '
    'only the operator can answer them. A message that was edited arrives again with edited="true" '
    'and the same message_name; treat it as a correction of the earlier version.'
)

# A takeover resumes from saved cursors only if the previous poller saved them
# this recently. Older ones mean no channel session was running, and replaying
# that backlog would flood the new session with messages nobody asked it to handle.
RESUME_WINDOW = datetime.timedelta(minutes=10)
# The poller re-saves unchanged cursors this often, so a quiet space still
# looks alive to a takeover.
CURSOR_HEARTBEAT = datetime.timedelta(minutes=1)
# A permission prompt goes to the Chat thread this session last heard from, if
# it heard from it this recently.
RELAY_WINDOW = datetime.timedelta(minutes=30)
# A lease not renewed for this long belongs to a hung poller and may be taken.
# Tool calls block the event loop too, so it must outlast the slowest of them.
LEASE_TTL = datetime.timedelta(seconds=60)

# Presence in a mention_only space: it ends when nobody has addressed the bot
# for PRESENCE_IDLE, and PRESENCE_MAX after it began however lively the space
# is, so a chatty space cannot keep the session reading forever.
PRESENCE_IDLE = datetime.timedelta(minutes=10)
PRESENCE_MAX = datetime.timedelta(hours=1)
# How often the poller asks while a space is active or has messages waiting.
ACTIVE_POLL_SECONDS = 2.0
# Unaddressed messages in an active space wait for the chat to pause for
# BATCH_QUIET, or for BATCH_MAX at most, and go as one delivery: one turn for
# a burst instead of one per message.
BATCH_QUIET = datetime.timedelta(seconds=4)
BATCH_MAX = datetime.timedelta(seconds=20)
# A mention_only space delivers at most this many times an hour. Past it the
# presence ends, and only messages that address the bot get through.
HOURLY_DELIVERIES = 30
# Messages the poller saw, kept to give a delivery the context it follows from.
BUFFER_WINDOW = datetime.timedelta(minutes=30)
BUFFER_MAX = 200
# What the session saw, and which messages are the bot's, is remembered this long.
MEMORY_WINDOW = datetime.timedelta(days=1)
# A delivery's "Earlier" part: at most this many messages and characters, newest kept.
CONTEXT_MESSAGES = 20
CONTEXT_CHARS = 4000
# Attachments of delivered messages are saved beside the state, up to this size,
# and deleted after ATTACHMENT_TTL.
ATTACHMENT_MAX_BYTES = 20 * 1024 * 1024
ATTACHMENT_TTL = datetime.timedelta(days=7)
ACK_EMOJI = '👀'


class ChannelStore:
    """Watched spaces as {space_name: config}, persisted as JSON.

    A config holds 'allowed_senders' (a list of users/ID, or None for everyone in
    the space) and 'mention_only', plus 'left_at' and 'muted_until' once the
    session has left the conversation or been muted there.
    """

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


def is_own(msg: Dict) -> bool:
    """Replies go out as the same user, so the clientAssignedMessageId prefix is
    the only way to tell the bot's messages from the operator's."""
    return msg.get('clientAssignedMessageId', '').startswith(APP_MESSAGE_PREFIX)


def sender_allowed(msg: Dict, allowed_senders: Optional[List[str]]) -> bool:
    return allowed_senders is None or msg.get('sender', {}).get('name') in allowed_senders


def parse_verdict(msg: Dict, operator: str) -> Optional[Tuple[str, str]]:
    """(request_id, 'allow' or 'deny') when the operator answers a permission prompt.

    Only the operator: an answer approves tool use in the session, and a space
    may let anyone else talk to the bot.
    """
    if is_own(msg) or msg.get('sender', {}).get('name') != operator:
        return None
    m = PERMISSION_REPLY_RE.match(msg.get('text') or '')
    if not m:
        return None
    return m.group(2).lower(), 'allow' if m.group(1).lower().startswith('y') else 'deny'


def _utc(ts: str) -> str:
    """'2026-09-18T09:24:19.953311Z' -> '09:24Z'."""
    return _parse_time(ts).strftime('%H:%MZ')


def _thread(msg: Dict) -> str:
    return msg.get('thread', {}).get('name', '')


def _body(msg: Dict) -> str:
    text = message_text(msg)
    if not text and (msg.get('cardsV2') or msg.get('cards')):
        return '[a card]'
    return text


@dataclasses.dataclass
class Flags:
    """How a message relates to the bot, from the message alone."""
    mentioned: bool = False
    replying_to_bot: bool = False
    operator: bool = False
    bot_sender: bool = False  # another bot or app, never counted as addressing ours

    @property
    def addressed(self) -> bool:
        return (self.mentioned or self.replying_to_bot) and not self.bot_sender

    def meta(self) -> Dict[str, str]:
        out = {}
        if self.mentioned:
            out['mentioned'] = 'true'
        if self.replying_to_bot:
            out['replying_to_bot'] = 'true'
        if self.operator:
            out['sender_is_operator'] = 'true'
        return out


@dataclasses.dataclass
class SpaceState:
    """What the poller knows about one space, in memory only.

    Presence: a message that addresses the bot starts it (or keeps it going),
    and so does the bot speaking while present. It ends PRESENCE_IDLE after
    the last of those, PRESENCE_MAX after it began, or when the session leaves.
    """
    active_since: Optional[datetime.datetime] = None
    last_addressed: Optional[datetime.datetime] = None
    # Recent messages from allowed senders and the bot, oldest first.
    buffer: List[Dict] = dataclasses.field(default_factory=list)
    # message name -> when it was noted: delivered to the session, or sent by it.
    seen: Dict[str, datetime.datetime] = dataclasses.field(default_factory=dict)
    bot_messages: Dict[str, datetime.datetime] = dataclasses.field(default_factory=dict)
    # Messages fetched for context or to read a quote, by name, and threads fetched.
    fetched: Dict[str, Dict] = dataclasses.field(default_factory=dict)
    fetched_threads: Dict[str, datetime.datetime] = dataclasses.field(default_factory=dict)
    # Unaddressed messages waiting for the chat to pause: (message, flags, when queued).
    pending: List[Tuple[Dict, Flags, datetime.datetime]] = dataclasses.field(default_factory=list)
    deliveries: List[datetime.datetime] = dataclasses.field(default_factory=list)

    def is_active(self, at: datetime.datetime) -> bool:
        return (self.active_since is not None and at - self.last_addressed < PRESENCE_IDLE
                and at - self.active_since < PRESENCE_MAX)

    def address(self, at: datetime.datetime) -> None:
        if not self.is_active(at):
            self.active_since = at
        self.last_addressed = at

    def go_idle(self) -> None:
        self.active_since = self.last_addressed = None
        self.pending.clear()

    def remember(self, msg: Dict) -> None:
        self.buffer = [m for m in self.buffer if m['name'] != msg['name']] + [msg]

    def prune(self, now: datetime.datetime) -> None:
        self.buffer = [m for m in self.buffer if now - _parse_time(m['createTime']) < BUFFER_WINDOW][-BUFFER_MAX:]
        for table in (self.seen, self.bot_messages, self.fetched_threads):
            for key in [k for k, t in table.items() if now - t >= MEMORY_WINDOW]:
                del table[key]
        self.fetched = dict(list(self.fetched.items())[-BUFFER_MAX:])
        self.deliveries = [t for t in self.deliveries if now - t < datetime.timedelta(hours=1)]


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

    def inline(text: str) -> str:
        # An inline code span ends at a newline, and text after it would be live
        # Chat markup; Claude Code folds whitespace already, but do not rely on it.
        return code(' '.join(text.split()))
    rid = params['request_id']
    description = _summary(params.get('description', ''))
    preview = _middle_cut(params.get('input_preview', ''), PROMPT_PREVIEW_CHARS)
    return (f"🔐 Claude wants to use `{inline(params.get('tool_name', ''))}`: `{inline(description)}`\n"
            f"```\n{code(preview)}\n```\n"
            f"Reply `yes {rid}` to allow or `no {rid}` to deny.")


def to_notification(msg: Dict, space_name: str, sender_name: str, edited: bool = False,
                    space_title: str = '', flags: Optional[Flags] = None, presence: bool = False,
                    content: Optional[str] = None) -> Dict:
    """Build notification params. Meta keys must be identifiers or Claude Code drops them.

    content defaults to the message's own text with its attachments' names.
    """
    meta = {
        'chat_id': space_name,
        'thread_name': _thread(msg),
        'message_name': msg.get('name', ''),
        'sender_id': msg.get('sender', {}).get('name', ''),
        'sender_name': sender_name,
        'ts': msg.get('createTime', ''),
    }
    if space_title:
        # Beside chat_id, never in it: chat_id goes back verbatim as send_message's space_name.
        meta['space_display_name'] = space_title
    if edited:
        meta['edited'] = 'true'
        meta['edited_at'] = msg.get('lastUpdateTime', '')
    if flags:
        meta.update(flags.meta())
    if presence:
        meta['presence'] = 'active'
    if content is None:
        content = _body(msg)
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
        self.attachments_dir = store.path.with_name('attachments')
        # (space, thread, when) of the last message delivered to this session: where
        # its permission prompts go while that conversation is recent.
        self.last_thread: Optional[Tuple[str, str, datetime.datetime]] = None
        # Per-space cursor: only messages created after it are delivered, so
        # watching a space never replays history.
        self.cursors: Dict[str, str] = {}
        self._saved_cursors: Optional[Tuple[Dict, Dict]] = None  # last written (cursors, edits)
        self.states: Dict[str, SpaceState] = {}
        # Per-space time of the last message.updated event seen, for edit delivery.
        self.edit_cursors: Dict[str, str] = {}
        self._saved_at: Optional[datetime.datetime] = None
        self._token = uuid.uuid4().hex
        self.active = False
        # Permission answers read by the last poll: (space, request_id, behavior, by)
        self._answered: List[Tuple[str, str, str, str]] = []

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
        # Presence is not resumed: a session that starts again starts idle.
        self.cursors, self.edit_cursors, self.states = {}, {}, {}
        if not self.cursor_path.exists():
            return
        saved = json.loads(self.cursor_path.read_text())
        saved_at = saved.get('saved_at')
        cutoff = datetime.datetime.now(datetime.timezone.utc) - RESUME_WINDOW
        if saved_at and datetime.datetime.fromisoformat(saved_at) >= cutoff:
            self.cursors.update(saved.get('cursors', {}))
            self.edit_cursors.update(saved.get('edits', {}))

    def _save_cursors(self) -> None:
        cursors = {s: ts for s, ts in self.cursors.items() if s in self.store.load()}
        now = datetime.datetime.now(datetime.timezone.utc)
        edits = {s: ts for s, ts in self.edit_cursors.items() if s in cursors}
        state = (cursors, edits)
        if state == self._saved_cursors and self._saved_at and now - self._saved_at < CURSOR_HEARTBEAT:
            return
        write_private(self.cursor_path, json.dumps(
            {'saved_at': now.isoformat(), 'cursors': cursors, 'edits': edits}))
        self._saved_cursors, self._saved_at = state, now

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
        now = datetime.datetime.now(datetime.timezone.utc)
        if not self.last_thread or now - self.last_thread[2] > RELAY_WINDOW:
            # No recent Chat conversation with this session: a prompt there would show
            # the command or file contents to whoever is in that space, unasked.
            logger.info("Permission request %s not relayed: no recent Chat thread", params.get('request_id'))
            return
        space, thread, _ = self.last_thread
        text = permission_prompt(params)
        rid = params['request_id']
        # Record the request before posting it, so an answer read by another poller
        # right after the post finds it.
        with self._permissions() as table:
            table[rid] = {'owner': self._token, 'space': space, 'message': None, 'text': text,
                          'created': now.isoformat()}
        sent = await send_space_message(space, text, thread_name=thread)
        with self._permissions() as table:
            if rid in table:
                table[rid]['message'] = sent['name']

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

    @staticmethod
    def _clock() -> datetime.datetime:
        return datetime.datetime.now(datetime.timezone.utc)

    def _now(self) -> str:
        return self._clock().isoformat().replace('+00:00', 'Z')

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
        config = {'allowed_senders': allowed_senders or None, 'mention_only': mention_only}
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

    def _update_config(self, space_name: str, **changes) -> Dict:
        spaces = self.store.load()
        if space_name not in spaces:
            raise ValueError(f"{space_name} is not watched; see list_watched_spaces")
        config = spaces[space_name]
        for key, value in changes.items():
            if value is None:
                config.pop(key, None)
            else:
                config[key] = value
        self.store.save(spaces)
        return config

    def leave(self, space_name: str) -> Dict:
        """End the session's presence in a space. The poller reads it from the store,
        since the tool may run in a session that is not the one polling."""
        self._update_config(space_name, left_at=self._now())
        return {'space_name': space_name, 'presence': 'idle'}

    def mute(self, space_name: str, minutes: int) -> Dict:
        """Leave, and for minutes let through only the operator addressing the bot. 0 unmutes."""
        if minutes < 0:
            raise ValueError("minutes must be 0 or more")
        until = (self._clock() + datetime.timedelta(minutes=minutes)).isoformat().replace('+00:00', 'Z')
        self._update_config(space_name, left_at=self._now(), muted_until=until if minutes else None)
        return {'space_name': space_name, 'muted_until': until if minutes else None}

    def list_watched(self) -> Dict:
        # BOT_NAME is fixed per process (an env var), so it is reported here but
        # changed only by restarting every server that shares the spaces.
        now = self._clock()
        spaces = []
        for s, config in self.store.load().items():
            entry = {'space_name': s, **config}
            state = self.states.get(s)
            if config.get('mention_only') and self.active:
                entry['presence'] = 'active' if state and state.is_active(now) else 'idle'
            spaces.append(entry)
        return {'bot_name': BOT_NAME, 'mention': f'@{BOT_NAME}',
                'message_id_prefix': APP_MESSAGE_PREFIX,
                'poller': self.poller_status(),
                'spaces': spaces}

    def busy(self) -> bool:
        """A space is active or has messages waiting, so the poller should ask sooner."""
        now = self._clock()
        return any(s.pending or s.is_active(now) for s in self.states.values())

    def poll_once(self) -> List[Dict]:
        """Fetch new messages from every watched space and return the notifications to send."""
        self._answered = []
        spaces = self.store.load()
        if not spaces:
            return []
        creds = self._creds()
        chat = _get_service('chat', 'v1', creds)
        operator = self_user_id(creds)
        now = self._clock()
        out = []
        for space_name, config in spaces.items():
            if space_name not in self.cursors:
                self.cursors[space_name] = self.edit_cursors[space_name] = self._now()
                continue
            state = self.states.setdefault(space_name, SpaceState())
            self._apply_leave(state, config)
            listed_after = self.cursors[space_name]
            try:
                response = chat.spaces().messages().list(
                    parent=space_name, pageSize=100, orderBy='createTime ASC',
                    filter=f'createTime > "{self.cursors[space_name]}"').execute()
            except Exception:
                # The cursor is unchanged, so the next poll retries this space.
                logger.exception("Polling %s failed", space_name)
                continue
            direct: List[Tuple[Dict, Flags]] = []
            for msg in response.get('messages', []):
                self.cursors[space_name] = msg['createTime']
                verdict = parse_verdict(msg, operator)
                if verdict:
                    # An answer to a permission prompt goes to the session that asked,
                    # never to Claude as chat.
                    self._answered.append((space_name, *verdict, get_user_display_name(msg.get('sender', {}), creds)))
                    continue
                self._take(chat, state, config, msg, operator, now, direct)
            out.extend(self._flush(chat, creds, space_name, config, state, operator, now, direct))
            out.extend(self._poll_edits(chat, creds, space_name, config, state, operator, listed_after))
            state.prune(now)
        return out

    @staticmethod
    def _apply_leave(state: SpaceState, config: Dict) -> None:
        left_at = config.get('left_at')
        if left_at and state.last_addressed and _parse_time(left_at) >= state.last_addressed:
            state.go_idle()

    @staticmethod
    def _muted(config: Dict, now: datetime.datetime) -> bool:
        until = config.get('muted_until')
        return bool(until) and _parse_time(until) > now

    def _flags(self, chat, state: SpaceState, msg: Dict, operator: str) -> Flags:
        sender = msg.get('sender', {})
        quoted = msg.get('quotedMessageMetadata', {}).get('name', '')
        replying = False
        if quoted:
            original = self._message(chat, state, quoted)
            replying = quoted in state.bot_messages or bool(original and is_own(original))
        return Flags(mentioned=mentions_bot(msg.get('text') or ''), replying_to_bot=replying,
                     operator=sender.get('name') == operator, bot_sender=sender.get('type') == 'BOT')

    def _message(self, chat, state: SpaceState, name: str) -> Optional[Dict]:
        """A message by name: from the buffer, from an earlier fetch, or fetched once now."""
        for msg in state.buffer:
            if msg['name'] == name:
                return msg
        if name not in state.fetched:
            try:
                state.fetched[name] = chat.spaces().messages().get(name=name).execute()
            except Exception:
                logger.exception("Reading quoted message %s failed", name)
                return None
        return state.fetched[name]

    def _take(self, chat, state: SpaceState, config: Dict, msg: Dict, operator: str,
              now: datetime.datetime, direct: List[Tuple[Dict, Flags]]) -> None:
        """Decide what one new message is: the bot's own, dropped, kept for context,
        queued for the next batch, or delivered with this poll."""
        at = _parse_time(msg['createTime'])
        if is_own(msg):
            state.remember(msg)
            state.seen[msg['name']] = state.bot_messages[msg['name']] = now
            if state.is_active(at):
                state.last_addressed = at  # the bot speaking keeps its presence going
            return
        if not sender_allowed(msg, config.get('allowed_senders')):
            return
        state.remember(msg)
        flags = self._flags(chat, state, msg, operator)
        if self._muted(config, now) and not (flags.operator and flags.addressed):
            return
        if not config.get('mention_only'):
            direct.append((msg, flags))
        elif flags.addressed:
            state.address(at)
            direct.append((msg, flags))
        elif state.is_active(at):
            if len(state.deliveries) >= HOURLY_DELIVERIES:
                logger.warning("%d deliveries from %s in the last hour; leaving the conversation",
                               len(state.deliveries), msg['name'].split('/messages/')[0])
                state.go_idle()
                return
            state.pending.append((msg, flags, now))

    def _flush(self, chat, creds, space_name: str, config: Dict, state: SpaceState, operator: str,
               now: datetime.datetime, direct: List[Tuple[Dict, Flags]]) -> List[Dict]:
        """One delivery for this space: the messages delivered now, plus the queued
        ones once the chat paused, or with them."""
        batch = []
        if state.pending and (direct or now - state.pending[-1][2] >= BATCH_QUIET
                              or now - state.pending[0][2] >= BATCH_MAX):
            batch = [(m, f) for m, f, _ in state.pending]
            state.pending = []
        batch = sorted(batch + direct, key=lambda e: e[0]['createTime'])
        if not batch:
            return []
        mention_only = bool(config.get('mention_only'))
        if mention_only:
            state.deliveries.append(now)
        earlier, left_out = self._context(chat, config, state, batch)
        for msg, flags in batch:
            if not flags.bot_sender and (flags.addressed or not mention_only):
                self._acknowledge(chat, msg)
        last, last_flags = batch[-1]
        notification = to_notification(
            last, space_name, get_user_display_name(last.get('sender', {}), creds),
            space_title=self._space_title(space_name, creds), flags=last_flags, presence=mention_only,
            content=self._content(creds, operator, batch, earlier, left_out))
        for msg in [m for m, _ in batch] + earlier:
            state.seen[msg['name']] = now
        return [notification]

    def _context(self, chat, config: Dict, state: SpaceState,
                 batch: List[Tuple[Dict, Flags]]) -> Tuple[List[Dict], int]:
        """The messages the session has not seen that the batch follows from, oldest
        first, and how many more were left out for size.

        That is the quoted messages, the rest of each thread a message replies in,
        and for a message in the main flow the recent main-flow messages before it.
        A thread the poller never saw begin is fetched once.
        """
        new = {m['name'] for m, _ in batch}
        newest = max(m['createTime'] for m, _ in batch)
        threads = {_thread(m) for m, _ in batch if m.get('threadReply')}
        main_flow = any(not m.get('threadReply') for m, _ in batch)
        candidates = {}
        for msg in state.buffer:
            if msg['createTime'] < newest and (_thread(msg) in threads or (main_flow and not msg.get('threadReply'))):
                candidates[msg['name']] = msg
        for msg, _ in batch:
            quoted = msg.get('quotedMessageMetadata', {}).get('name', '')
            if quoted:
                original = self._message(chat, state, quoted)
                if original:
                    candidates[quoted] = original
        for thread in threads - set(state.fetched_threads):
            state.fetched_threads[thread] = self._clock()
            first = min(m['createTime'] for m, _ in batch if _thread(m) == thread)
            if any(_thread(m) == thread and m['createTime'] < first for m in state.buffer):
                continue  # the buffer holds its start already
            try:
                listed = chat.spaces().messages().list(
                    parent=thread.split('/threads/')[0], pageSize=CONTEXT_MESSAGES, orderBy='createTime DESC',
                    filter=f'thread.name = {thread} AND createTime < "{first}"').execute()
            except Exception:
                logger.exception("Reading thread %s for context failed", thread)
                continue
            for msg in listed.get('messages', []):
                candidates[msg['name']] = msg
        earlier = sorted((m for name, m in candidates.items()
                          if name not in new and name not in state.seen
                          and (is_own(m) or sender_allowed(m, config.get('allowed_senders')))),
                         key=lambda m: m['createTime'])
        total = len(earlier)
        earlier = earlier[-CONTEXT_MESSAGES:]
        while len(earlier) > 1 and sum(len(_body(m)) for m in earlier) > CONTEXT_CHARS:
            earlier = earlier[1:]
        return earlier, total - len(earlier)

    def _content(self, creds, operator: str, batch: List[Tuple[Dict, Flags]],
                 earlier: List[Dict], left_out: int) -> str:
        """The delivery's text. One message with nothing before it is just its text."""
        def attachments(msg: Dict, save: bool) -> str:
            lines = [self._attachment_line(creds, a, save) for a in msg.get('attachment', [])]
            return ''.join(f"\n{line}" for line in lines)

        if len(batch) == 1 and not earlier:
            msg = batch[0][0]
            return _body(msg) + attachments(msg, True)

        def line(msg: Dict, flags: Optional[Flags], save: bool) -> str:
            if is_own(msg):
                who = 'you'
            else:
                who = get_user_display_name(msg.get('sender', {}), creds)
                tags = ['operator'] if msg.get('sender', {}).get('name') == operator else []
                if flags and flags.mentioned:
                    tags.append('mentions you')
                if flags and flags.replying_to_bot:
                    tags.append('replies to you')
                if tags:
                    who += f" ({', '.join(tags)})"
            return f"[{who}, {_utc(msg['createTime'])}, {_thread(msg)}]\n{_body(msg)}{attachments(msg, save)}"

        parts = []
        if earlier:
            head = 'Earlier, not sent to you before'
            if left_out:
                head += f' ({left_out} older left out; get_messages has them)'
            parts.append(head + ':\n' + '\n'.join(line(m, None, False) for m in earlier))
        parts.append('New:\n' + '\n'.join(line(m, f, True) for m, f in batch))
        return '\n\n'.join(parts)

    def _attachment_line(self, creds, attachment: Dict, save: bool) -> str:
        """An attachment as the session reads it: a local path for a Chat upload that
        was saved, a link for a Drive file, otherwise its name and why."""
        name = attachment.get('contentName') or 'attachment'
        drive_id = attachment.get('driveDataRef', {}).get('driveFileId')
        if drive_id:
            return f"[attachment {name}: Drive file https://drive.google.com/open?id={drive_id}]"
        resource = attachment.get('attachmentDataRef', {}).get('resourceName')
        if not save or not resource:
            return f"[attachment {name}]"
        self._prune_attachments()
        try:
            saved = save_attachment(creds, resource, str(self.attachments_dir), name, ATTACHMENT_MAX_BYTES)
        except AttachmentTooLarge:
            return f"[attachment {name}: over {ATTACHMENT_MAX_BYTES // (1024 * 1024)} MB, not saved]"
        except Exception:
            logger.exception("Saving attachment %s failed", name)
            return f"[attachment {name}: could not be saved]"
        return f"[attachment {name} ({saved['contentType']}): {saved['path']}]"

    def _prune_attachments(self) -> None:
        if not self.attachments_dir.exists():
            self.attachments_dir.mkdir(mode=0o700, parents=True)
            return
        cutoff = self._clock().timestamp() - ATTACHMENT_TTL.total_seconds()
        for path in self.attachments_dir.iterdir():
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)

    @staticmethod
    def _acknowledge(chat, msg: Dict) -> None:
        """React to a message addressed to the bot, so the sender knows it arrived."""
        try:
            chat.spaces().messages().reactions().create(
                parent=msg['name'], body={'emoji': {'unicode': ACK_EMOJI}}).execute()
        except Exception:
            logger.exception("Reacting to %s failed", msg['name'])

    @staticmethod
    def _space_title(space_name: str, creds) -> str:
        """The space's display name for the notification; '' when it cannot be read.

        A delivery never waits on this lookup failing: the name is a hint, and the
        message is what the session needs.
        """
        try:
            return space_display_name(space_name, creds)
        except Exception:
            logger.exception("Reading the name of %s failed", space_name)
            return ''

    def _poll_edits(self, chat, creds, space_name: str, config: Dict, state: SpaceState, operator: str,
                    listed_after: str) -> List[Dict]:
        """Edited messages that concern the session, delivered one by one and marked edited.

        An edit gets through where the message itself would have: it addresses the
        bot now, the space is not mention_only, the bot is present, or the session
        saw the message before the edit.

        messages.list cannot filter on edit time, so edits come from spaceEvents.
        """
        since = self.edit_cursors.setdefault(space_name, self._now())
        out, page_token = [], None
        now = self._clock()
        try:
            while True:
                args = {'parent': space_name, 'pageSize': 100,
                        'filter': f'start_time="{since}" AND event_types:"google.workspace.chat.message.v1.updated"'}
                if page_token:
                    args['pageToken'] = page_token
                response = chat.spaces().spaceEvents().list(**args).execute()
                for event in response.get('spaceEvents', []):
                    self.edit_cursors[space_name] = event['eventTime']
                    for payload in _event_payloads(event):
                        msg = payload.get('message', {})
                        if msg.get('deleteTime') or not msg.get('lastUpdateTime'):
                            continue
                        if _parse_time(msg['createTime']) > _parse_time(listed_after):
                            continue  # listed in this poll already, in its edited form
                        if is_own(msg):
                            continue  # the bot's own edits, including answered permission prompts
                        if not sender_allowed(msg, config.get('allowed_senders')):
                            continue
                        flags = self._flags(chat, state, msg, operator)
                        at = _parse_time(msg['lastUpdateTime'])
                        if self._muted(config, now) and not (flags.operator and flags.addressed):
                            continue
                        mention_only = bool(config.get('mention_only'))
                        if mention_only and not (flags.addressed or state.is_active(at) or msg['name'] in state.seen):
                            continue
                        if mention_only and flags.addressed:
                            state.address(at)
                        state.remember(msg)
                        state.seen[msg['name']] = now
                        sender_name = get_user_display_name(msg.get('sender', {}), creds)
                        out.append(to_notification(msg, space_name, sender_name, edited=True,
                                                   space_title=self._space_title(space_name, creds),
                                                   flags=flags, presence=mention_only))
                page_token = response.get('nextPageToken')
                if not page_token:
                    break
        except Exception:
            # edit_cursors keeps the last event handled, so the next poll resumes after it.
            logger.exception("Polling edits in %s failed", space_name)
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

    async def _deliver(self, write_stream, notifications: List[Dict]) -> None:
        for params in notifications:
            meta = params.get('meta', {})
            if meta.get('chat_id') and meta.get('thread_name'):
                self.last_thread = (meta['chat_id'], meta['thread_name'], datetime.datetime.now(datetime.timezone.utc))
            await self._notify(write_stream, CHANNEL_METHOD, params)
        self._save_cursors()
        for space, rid, behavior, by in self._answered:
            entry = self._record_verdict(space, rid, behavior, by)
            if entry and entry.get('message'):
                word = 'Allowed' if behavior == 'allow' else 'Denied'
                await update_message(entry['message'], text=f"{entry['text']}\n*{word}* by {by}.")

    @staticmethod
    async def _notify(write_stream, method: str, params: Dict) -> None:
        notification = types.JSONRPCNotification(jsonrpc='2.0', method=method, params=params)
        await write_stream.send(SessionMessage(notification))

    async def _poll_forever(self, write_stream) -> None:
        while True:
            try:
                # Blocks the event loop like every tool call here does. A worker
                # thread would share the cached httplib2 clients with tool calls,
                # and httplib2 is not thread-safe.
                if self.try_acquire():
                    before = copy.deepcopy((self.cursors, self.edit_cursors, self.states))
                    notifications = self.poll_once()
                    # A long poll (retries, a slow API) can outlast the lease. If another
                    # session took over meanwhile, it delivers from the saved cursors, so
                    # delivering or saving here would duplicate or clobber its work. Roll
                    # the cursors back instead; if the lease is still ours (the check only
                    # hit a busy lock), the next poll fetches the same messages again.
                    if self.try_acquire():
                        await self._deliver(write_stream, notifications)
                    else:
                        self.cursors, self.edit_cursors, self.states = before
                        logger.warning("Channel lease changed during a poll; its results were not delivered")
                # Every session, poller or not, applies the answers to its own prompts.
                for rid, behavior in self._take_verdicts():
                    await self._notify(write_stream, PERMISSION_METHOD, {'request_id': rid, 'behavior': behavior})
            except Exception:
                # Missing credentials or a failed state read must not kill the MCP server.
                logger.exception("Google Chat channel poll failed")
            await anyio.sleep(min(self.poll_seconds, ACTIVE_POLL_SECONDS) if self.busy() else self.poll_seconds)
