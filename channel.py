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

With CHANNEL_GATE=jev no space has presence, and mention_only means nothing.
In every watched space a mention or a quote reply still arrives at once, and
every other batch goes to the gate
(gate.py), which asks Jev whether to deliver it, react to it with an emoji,
join in (to help, or just to talk), or hold it back. What it held back reaches the session later as context.
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
import urllib.parse
import uuid
import zoneinfo
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import anyio
import mcp.types as types
from mcp.shared.message import SessionMessage

from gate import HOLD, INTERJECT, REACT, REPLY, TEXT_CHARS, Decision, Gate, JevError, Message as GateMessage
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
    'and the same message_name, with the version you saw under "Before the edit" when you saw one. '
    'Treat it as a correction of that version, and reply only when the edit changes what was asked '
    'or what it means, or adds a request; a fixed typo needs nothing.'
)

def gate_instructions(gate: Gate) -> str:
    """Appended to INSTRUCTIONS when CHANNEL_GATE=jev.

    Built from the gate that runs, so what the session says about how it works,
    when someone in a chat asks, cannot drift from what it is.
    """
    host = urllib.parse.urlparse(gate.jev.url).hostname
    return (
        ' This channel runs a classifier gate in every watched space, in place of mention_only and presence: a '
        'mention or a quote reply to you still arrives at once, and any other message arrives only when the gate '
        'lets it through. Such a delivery carries gate="reply" when the gate judged that the chat expects your '
        'answer, or gate="interject" when nobody asked you but joining in would be natural: gate_reason="help" when '
        'there is an open question you may be able to help with, gate_reason="join" when the talk itself invites a '
        'remark or a joke. Either way say one short thing that fits, and stay silent when nothing does. The gate '
        'also reacts with an emoji for you where that is all a person would do. What the gate held back reaches you '
        'later under "Earlier". leave_conversation holds back everything but mentions and quote replies there for '
        f'{int(PRESENCE_IDLE.total_seconds() // 60)} minutes.'
        ' What follows is how the gate works. You may explain it when someone asks, and should not embellish it. '
        f'The gate is the channel server, not you. When a chat pauses for {int(BATCH_QUIET.total_seconds())} seconds, it sends the new '
        f'messages, up to {GATE_HISTORY} before them and your own last message in that conversation (each cut to {TEXT_CHARS} characters, with sender names, how '
        'long ago each was sent, and whether it mentions you or someone else), your one-line description, and the '
        f"space's own description if the operator gave one, to TypeSafe's Jev model ({gate.jev.model}) through {host}. "
        'Jev is a decision model: it writes no text, and answers typed questions with probabilities (is this addressed '
        'to you, does the chat expect your answer, is there an open problem you could help with, how invited would '
        'you feel to join, is it personal, which emoji fits). The server turns those into reply, react, join or '
        f'hold with fixed thresholds. It stops you joining in unasked while you wrote more than '
        f'{int(GATE_MAX_SHARE * 100)}% of the last {GATE_SHARE_WINDOW} messages, unless the space sets its own share. '
        'A message that mentions or quotes you reaches you without Jev judging it, though it can be part of what Jev reads later. You yourself are a Claude model in a Claude Code session, '
        'and your replies are written by you, not by Jev. If you are asked about something this does not cover, such '
        'as the exact thresholds, say you do not know.'
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
# With the gate: the recent messages Jev reads before the batch it judges, and
# how long it waits after Jev failed before asking again.
GATE_HISTORY = 12
GATE_RETRY = datetime.timedelta(seconds=30)
# Joining in unasked is held back by the bot's share of the conversation, like a
# person who keeps from dominating a group, instead of by a clock: a fixed
# cooldown left it silent through a lively chat it had joined once. A space's
# config may set 'max_share' of the last GATE_SHARE_WINDOW messages. It may also
# set 'reactions' to false, where an emoji on every "ok" would be noise, and
# 'norms', the operator's description of how the space works, which Jev reads.
GATE_SHARE_WINDOW = 10
GATE_MAX_SHARE = 0.3
# An unasked reaction skips a message when one of this many before it got one.
GATE_REACTION_SPACING = 3
GATE_CONFIG_KEYS = ('norms', 'max_share', 'reactions')


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


def mentions_someone(msg: Dict) -> bool:
    """An @-mention of a person, not @all. The bot is not a Chat user, so it is never one of them."""
    return any(a.get('type') == 'USER_MENTION' and a.get('userMention', {}).get('user', {}).get('name')
               for a in msg.get('annotations', []))


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


def _local(ts: str, zone: datetime.tzinfo, now: datetime.datetime) -> str:
    """A message's time as people in the space read it, with the day when it is not today.

    '2026-09-18T09:24:19Z' -> '09:24Z' in UTC, '16:24 WIB' in Asia/Jakarta, and
    'Sep 17 16:24 WIB' when read on the 18th.
    """
    at = _parse_time(ts).astimezone(zone)
    label = 'Z' if zone == datetime.timezone.utc else f" {at.strftime('%Z')}"
    day = '' if at.date() == now.astimezone(zone).date() else at.strftime('%b %d ')
    return f"{day}{at.strftime('%H:%M')}{label}"


def _thread(msg: Dict) -> str:
    return msg.get('thread', {}).get('name', '')


def _scope(msg: Dict) -> str:
    """The conversation a message belongs to: the thread it replies in, or the main flow."""
    return _thread(msg) if msg.get('threadReply') else 'main'


def _same_words(a: str, b: str) -> bool:
    return a.casefold().split() == b.casefold().split()


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
    # Messages fetched to read a quote, by name.
    fetched: Dict[str, Dict] = dataclasses.field(default_factory=dict)
    # Unaddressed messages waiting for the chat to pause: (message, flags, when queued).
    pending: List[Tuple[Dict, Flags, datetime.datetime]] = dataclasses.field(default_factory=list)
    deliveries: List[datetime.datetime] = dataclasses.field(default_factory=list)
    # With the gate: the decision on the pending batch and the newest message it
    # covered, so a batch is judged once until it grows; when to ask again after
    # Jev failed; and what it did about messages it did not deliver (message
    # name -> 'stayed_silent' or 'reacted').
    judged: Optional[Tuple[str, Decision]] = None
    gate_retry_at: Optional[datetime.datetime] = None
    silent: Dict[str, str] = dataclasses.field(default_factory=dict)
    # Per conversation (a thread replied in, or the main flow): the createTime of
    # the newest message the session has seen there, so a delivery after a long
    # gap can fetch what it missed beyond the buffer; and the bot's last message
    # there with the first one after it, so the gate knows the bot took part.
    seen_upto: Dict[str, str] = dataclasses.field(default_factory=dict)
    last_spoke: Dict[str, List[Dict]] = dataclasses.field(default_factory=dict)

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
        self.judged = None

    def remember(self, msg: Dict) -> None:
        self.buffer = [m for m in self.buffer if m['name'] != msg['name']] + [msg]

    def saw(self, msg: Dict) -> None:
        scope = _scope(msg)
        self.seen_upto[scope] = max(self.seen_upto.get(scope, ''), msg['createTime'])

    def prune(self, now: datetime.datetime) -> None:
        self.buffer = [m for m in self.buffer if now - _parse_time(m['createTime']) < BUFFER_WINDOW][-BUFFER_MAX:]
        for table in (self.seen, self.bot_messages):
            for key in [k for k, t in table.items() if now - t >= MEMORY_WINDOW]:
                del table[key]
        self.fetched = dict(list(self.fetched.items())[-BUFFER_MAX:])
        kept = {m['name'] for m in self.buffer}
        self.silent = {name: action for name, action in self.silent.items() if name in kept}
        self.last_spoke = {scope: msgs for scope, msgs in self.last_spoke.items()
                           if now - _parse_time(msgs[0]['createTime']) < MEMORY_WINDOW}
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
                    content: Optional[str] = None, gate: str = '', gate_reason: str = '') -> Dict:
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
    if gate:
        meta['gate'] = gate
    if gate_reason:
        meta['gate_reason'] = gate_reason
    if content is None:
        content = _body(msg)
        names = [a.get('contentName') for a in msg.get('attachment', []) if a.get('contentName')]
        if names:
            content += f"\n[attachments: {', '.join(names)}]"
    return {'content': content, 'meta': meta}


class Channel:
    def __init__(self, store: ChannelStore, poll_seconds: float, gate: Optional[Gate] = None):
        self.store = store
        self.poll_seconds = poll_seconds
        # None: mention_only spaces use presence. A Gate: they use the gate.
        self.gate = gate
        # The zone a delivery's times are written in, for the people in the space.
        # Meta keeps ISO UTC. A name zoneinfo does not know fails the start.
        zone = os.environ.get('CHANNEL_TIMEZONE', '')
        self.zone = zoneinfo.ZoneInfo(zone) if zone else datetime.timezone.utc
        # Every gate decision and error, one JSON object a line, to tune the gate by.
        # It holds chat text, so entries older than CHANNEL_GATE_LOG_DAYS go.
        self.gate_log_path = store.path.with_name('gate_log.jsonl')
        self.gate_log_days = int(os.environ.get('CHANNEL_GATE_LOG_DAYS', '14'))
        self._gate_log_pruned: Optional[datetime.datetime] = None
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
        spaces = self.store.load()
        # The operator's gate settings are kept; watching again is not how they change.
        kept = {k: v for k, v in spaces.get(space_name, {}).items() if k in GATE_CONFIG_KEYS}
        config = {'allowed_senders': allowed_senders or None, 'mention_only': mention_only, **kept}
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
            if config.get('mention_only') and self.active and not self.gate:
                entry['presence'] = 'active' if state and state.is_active(now) else 'idle'
            spaces.append(entry)
        return {'bot_name': BOT_NAME, 'mention': f'@{BOT_NAME}', 'gate': 'jev' if self.gate else 'rules',
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
        self._prune_gate_log(now)
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
    def _left_recently(config: Dict, now: datetime.datetime) -> bool:
        """With the gate, leaving holds back what does not address the bot for PRESENCE_IDLE."""
        left_at = config.get('left_at')
        return bool(left_at) and now - _parse_time(left_at) < PRESENCE_IDLE

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
            state.saw(msg)
            state.last_spoke[_scope(msg)] = [msg]
            if state.is_active(at):
                state.last_addressed = at  # the bot speaking keeps its presence going
            if self.gate:
                self._gate_log({'event': 'bot_message', 'space': msg['name'].split('/messages/')[0],
                                'message': msg['name']})
            return
        if not sender_allowed(msg, config.get('allowed_senders')):
            return
        state.remember(msg)
        spoke = state.last_spoke.get(_scope(msg))
        if spoke and len(spoke) == 1:
            spoke.append(msg)
        flags = self._flags(chat, state, msg, operator)
        if self._muted(config, now) and not (flags.operator and flags.addressed):
            return
        if self.gate:
            if flags.addressed:
                state.address(at)
                direct.append((msg, flags))
                return
            if flags.bot_sender or self._left_recently(config, now):
                return
            if len(state.deliveries) >= HOURLY_DELIVERIES:
                logger.warning("%d deliveries from %s in the last hour; the gate holds back the rest",
                               len(state.deliveries), msg['name'].split('/messages/')[0])
                return
            state.pending.append((msg, flags, now))
        elif not config.get('mention_only'):
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
        if self.gate and not direct:
            return self._gated(chat, creds, space_name, config, state, operator, now)
        state.judged = None  # a mention takes the batch the gate was judging with it
        batch = []
        if state.pending and (direct or now - state.pending[-1][2] >= BATCH_QUIET
                              or now - state.pending[0][2] >= BATCH_MAX):
            batch = [(m, f) for m, f, _ in state.pending]
            state.pending = []
        batch = sorted(batch + direct, key=lambda e: e[0]['createTime'])
        if not batch:
            return []
        mention_only = bool(config.get('mention_only'))
        everything = not mention_only and not self.gate
        acknowledged = [m for m, f in batch if not f.bot_sender and (f.addressed or everything)]
        return [self._delivery(chat, creds, space_name, config, state, operator, now, batch, acknowledged,
                               presence=mention_only and not self.gate)]

    def _delivery(self, chat, creds, space_name: str, config: Dict, state: SpaceState, operator: str,
                  now: datetime.datetime, batch: List[Tuple[Dict, Flags]], acknowledged: List[Dict],
                  presence: bool = False, gate: str = '', gate_reason: str = '') -> Dict:
        """The notification for a batch, with the context it follows from. Marks all of it seen."""
        if config.get('mention_only') or self.gate:
            state.deliveries.append(now)
        earlier, left_out = self._context(chat, config, state, batch)
        for msg in acknowledged:
            self._acknowledge(chat, msg)
        last, last_flags = batch[-1]
        notification = to_notification(
            last, space_name, get_user_display_name(last.get('sender', {}), creds),
            space_title=self._space_title(space_name, creds), flags=last_flags, presence=presence,
            content=self._content(creds, operator, batch, earlier, left_out), gate=gate, gate_reason=gate_reason)
        for msg in [m for m, _ in batch] + earlier:
            state.seen[msg['name']] = now
            state.saw(msg)
        return notification

    def _regate_edit(self, config: Dict, state: SpaceState, msg: Dict, flags: Flags,
                     now: datetime.datetime) -> bool:
        """With the gate, an edit of a message the session never saw is judged again.

        The edited version joins the pending batch, in place of the original if
        it is still waiting, so "lunch?" edited to "Nduk, lunch?" can now reach
        the session. An edit that only changes spacing or case is not worth a
        judgment. An edit that addresses the bot, and one of a message the
        session saw, go the ordinary way. Returns whether the edit was handled.
        """
        if not self.gate or flags.addressed or flags.bot_sender or msg['name'] in state.seen:
            return False
        before = next((m for m in state.buffer if m['name'] == msg['name']), None)
        if not before:
            return True   # too old to have context worth judging it in
        state.remember(msg)
        if _same_words(_body(before), _body(msg)) or self._left_recently(config, now):
            return True
        state.pending = [p for p in state.pending if p[0]['name'] != msg['name']] + [(msg, flags, now)]
        state.silent.pop(msg['name'], None)
        return True

    def _gated(self, chat, creds, space_name: str, config: Dict, state: SpaceState, operator: str,
               now: datetime.datetime) -> List[Dict]:
        """The pending batch of a gated space, once the chat paused: judged once until it
        grows, then delivered, reacted to, chimed in on, or held back.

        Chiming in waits longer than a reply, so a person can answer first: a new
        message grows the batch and it is judged again. Joining in unasked is held
        back where the bot already has its share of the conversation (see
        GATE_MAX_SHARE), and so is an emoji where the space turned reactions off or
        the bot just reacted. A failed call leaves the batch waiting and is retried
        after GATE_RETRY.
        """
        if not state.pending:
            return []
        quiet = now - state.pending[-1][2]
        if quiet < BATCH_QUIET and now - state.pending[0][2] < BATCH_MAX:
            return []
        newest = state.pending[-1][0]['name']
        if not state.judged or state.judged[0] != newest:
            if state.gate_retry_at and now < state.gate_retry_at:
                return []
            decision = self._judge(creds, space_name, config, state, now)
            if not decision:
                state.gate_retry_at = now + GATE_RETRY
                return []
            state.gate_retry_at = None
            state.judged = (newest, decision)
        decision = state.judged[1]
        action = decision.action
        policy = self.gate.policy
        if action == INTERJECT:
            if self._has_its_share(config, state):
                action = HOLD
            elif quiet.total_seconds() < policy.interject_quiet:
                return []
        elif action == REACT and decision.scores['addressed'] < policy.reply:
            recent = [m['name'] for m in state.buffer if m['name'] not in {p[0]['name'] for p in state.pending}]
            if config.get('reactions') is False or any(
                    state.silent.get(name) == 'reacted' for name in recent[-GATE_REACTION_SPACING:]):
                action = HOLD
        batch = [(m, f) for m, f, _ in state.pending]
        state.pending, state.judged = [], None
        if action in (REPLY, INTERJECT):
            return [self._delivery(chat, creds, space_name, config, state, operator, now, batch,
                                   acknowledged=[batch[-1][0]] if action == REPLY else [], gate=action,
                                   gate_reason=decision.reason if action == INTERJECT else '')]
        if action == REACT:
            self._acknowledge(chat, batch[-1][0], decision.emoji)
        for msg, _ in batch:
            state.silent[msg['name']] = 'reacted' if action == REACT else 'stayed_silent'
        return []

    @staticmethod
    def _has_its_share(config: Dict, state: SpaceState) -> bool:
        """The bot wrote more than its share of the recent messages in the space."""
        recent = state.buffer[-GATE_SHARE_WINDOW:]
        own = sum(1 for m in recent if is_own(m))
        return own > config.get('max_share', GATE_MAX_SHARE) * GATE_SHARE_WINDOW

    def _judge(self, creds, space_name: str, config: Dict, state: SpaceState,
               now: datetime.datetime) -> Optional[Decision]:
        """Ask the gate about the pending batch, logging the decision or the failure."""
        messages = self._gate_messages(creds, state, now)
        started = self._clock()
        try:
            decision = self.gate.judge('group', messages, config.get('norms', ''))
        except JevError as e:
            logger.error("The gate could not judge %s: %s", space_name, e)
            self._gate_log({'event': 'error', 'space': space_name, 'error': str(e)})
            return None
        self._gate_log({'event': 'decision', 'space': space_name, 'action': decision.action,
                        'reason': decision.reason, 'emoji': decision.emoji,
                        'scores': decision.scores, 'ms': int((self._clock() - started).total_seconds() * 1000),
                        'messages': [m.state() for m in messages],
                        'names': [m['name'] for m, _, _ in state.pending]})
        return decision

    def _gate_messages(self, creds, state: SpaceState, now: datetime.datetime) -> List[GateMessage]:
        """The pending batch as the gate reads it, after the recent messages it follows:
        its threads', and the main flow's when it has main-flow messages."""
        pending = {m['name']: f for m, f, _ in state.pending}
        newest = max(m['createTime'] for m, _, _ in state.pending)
        threads = {_thread(m) for m, _, _ in state.pending if m.get('threadReply')}
        main_flow = any(not m.get('threadReply') for m, _, _ in state.pending)
        history = [m for m in state.buffer
                   if m['name'] not in pending and m['createTime'] < newest
                   and (_thread(m) in threads or (main_flow and not m.get('threadReply')))][-GATE_HISTORY:]

        def gate_message(msg: Dict, flags: Optional[Flags]) -> GateMessage:
            own = is_own(msg)
            quoted = msg.get('quotedMessageMetadata', {}).get('name', '')
            return GateMessage(
                sender=self.gate.policy.name if own else get_user_display_name(msg.get('sender', {}), creds),
                text=_body(msg), ago_seconds=int((now - _parse_time(msg['createTime'])).total_seconds()),
                thread=_thread(msg).split('/threads/')[-1], from_agent=own,
                from_bot=msg.get('sender', {}).get('type') == 'BOT',
                mentions_agent=flags.mentioned if flags else (not own and mentions_bot(msg.get('text') or '')),
                replies_to_agent=flags.replying_to_bot if flags else quoted in state.bot_messages,
                mentions_others=mentions_someone(msg), new=flags is not None, agent_action=state.silent.get(msg['name'], ''),
                edited=bool(msg.get('lastUpdateTime')))
        shown = {m['name'] for m in history} | set(pending)
        scopes = {_scope(m) for m, _, _ in state.pending}
        spoke = sorted((m for scope in scopes for m in state.last_spoke.get(scope, []) if m['name'] not in shown),
                       key=lambda m: m['createTime'])
        return ([gate_message(m, None) for m in spoke + history]
                + [gate_message(m, f) for m, f, _ in state.pending])

    def _prune_gate_log(self, now: datetime.datetime) -> None:
        """Drop entries older than gate_log_days, at most once an hour."""
        if not self.gate or not self.gate_log_path.exists():
            return
        if self._gate_log_pruned and now - self._gate_log_pruned < datetime.timedelta(hours=1):
            return
        self._gate_log_pruned = now
        cutoff = (now - datetime.timedelta(days=self.gate_log_days)).isoformat().replace('+00:00', 'Z')
        lines = self.gate_log_path.read_text().splitlines(keepends=True)
        kept = [line for line in lines if json.loads(line).get('at', '') >= cutoff]
        if len(kept) < len(lines):
            write_private(self.gate_log_path, ''.join(kept))

    def _gate_log(self, entry: Dict) -> None:
        entry = {'at': self._now(), **entry}
        fd = os.open(self.gate_log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, 'a') as f:
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')

    def _context(self, chat, config: Dict, state: SpaceState,
                 batch: List[Tuple[Dict, Flags]]) -> Tuple[List[Dict], int]:
        """The messages the session has not seen that the batch follows from, oldest
        first, and how many more were left out for size.

        That is the quoted messages, the rest of each thread a message replies in,
        and for a message in the main flow the recent main-flow messages before it.
        Where the buffer does not reach back far enough, Chat is asked: for a
        conversation the session last saw before the buffer begins (it held back
        or missed messages for longer than BUFFER_WINDOW), the gap since then, so
        a delivery hours later still carries what was said in between. For one
        this session has not seen at all, as after a restart, whatever the last
        MEMORY_WINDOW holds, since a fresh session knows none of it.
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
        scopes = threads | ({'main'} if main_flow else set())
        for scope in scopes:
            since = state.seen_upto.get(scope)
            if since and any(m['createTime'] <= since for m in state.buffer):
                continue  # the buffer covers the gap
            if not since:
                since = (self._clock() - MEMORY_WINDOW).isoformat().replace('+00:00', 'Z')
            first = min(m['createTime'] for m, _ in batch if _scope(m) == scope)
            query = f'createTime > "{since}" AND createTime < "{first}"'
            if scope != 'main':
                query = f'thread.name = {scope} AND {query}'
            try:
                # One page, newest first. A longer gap counts short in "left out",
                # and get_messages has the rest.
                listed = chat.spaces().messages().list(
                    parent=batch[0][0]['name'].split('/messages/')[0], pageSize=100, orderBy='createTime DESC',
                    filter=query).execute()
            except Exception:
                logger.exception("Reading what the session has not seen in %s failed", scope)
                continue
            for msg in listed.get('messages', []):
                # A thread's query holds only the thread, its first message included,
                # which is not a reply. The main flow's holds replies too, to drop.
                if scope != 'main' or not msg.get('threadReply'):
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
            when = _local(msg['createTime'], self.zone, self._clock())
            return f"[{who}, {when}, {_thread(msg)}]\n{_body(msg)}{attachments(msg, save)}"

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
    def _acknowledge(chat, msg: Dict, emoji: str = ACK_EMOJI) -> None:
        """React to a message addressed to the bot, so the sender knows it arrived."""
        try:
            chat.spaces().messages().reactions().create(
                parent=msg['name'], body={'emoji': {'unicode': emoji}}).execute()
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
                        filtered = mention_only or self.gate is not None
                        if self._regate_edit(config, state, msg, flags, now):
                            continue
                        if filtered and not (flags.addressed or state.is_active(at) or msg['name'] in state.seen):
                            continue
                        # The version the session saw, so it can tell a typo fix from a
                        # changed request. Only the buffer has it; a fetch would return the edit.
                        before = (next((m for m in state.buffer if m['name'] == msg['name']), None)
                                  if msg['name'] in state.seen else None)
                        if before and _same_words(_body(before), _body(msg)):
                            state.remember(msg)
                            continue  # spacing or case only: nothing to reconsider
                        if filtered and flags.addressed:
                            state.address(at)
                        state.remember(msg)
                        state.seen[msg['name']] = now
                        sender_name = get_user_display_name(msg.get('sender', {}), creds)
                        content = None
                        if before:
                            content = f"Before the edit:\n{_body(before)}\n\nAfter:\n{_body(msg)}"
                        out.append(to_notification(msg, space_name, sender_name, edited=True,
                                                   space_title=self._space_title(space_name, creds),
                                                   flags=flags, presence=mention_only and not self.gate,
                                                   content=content))
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
